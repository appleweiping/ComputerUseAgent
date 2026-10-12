"""Repeated Browser-Use evaluation on three public, read-only navigation tasks.

The public artifacts written by this runner are intentionally sanitized. Browser
profiles and detailed per-run evidence stay under ``<output-dir>/_private/``.
``--output-dir`` is required and must not be inside ``research/evidence``
(archived evidence is read-only); prefer a directory outside the repository,
for example under ``$CUA_PRIVATE_ROOT``.

Run from the repository root: ``python -m research.pilots.browser_use.runner``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
import platform
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Browser-Use queries Chrome's loopback CDP endpoint with httpx. On Windows,
# httpx can inherit the system proxy and receive an empty 502 for localhost.
# Explicitly bypassing the proxy for loopback is required for a reliable launch.
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

from browser_use import Agent, Browser
from browser_use.llm import ChatOpenAI


FRAMEWORK = "browser-use"
FRAMEWORK_VERSION = "0.11.5"
FRAMEWORK_REPOSITORY = "https://github.com/browser-use/browser-use"
FRAMEWORK_COMMIT = "eed197835cb6e5023716ddbad5bb11975c856232"
DEFAULT_MODEL = "gpt-5.5"
CLICK_ACTION_NAMES = {
    "click",
    "click_element",
    "click_element_by_index",
}


@dataclass(frozen=True)
class Task:
    id: str
    public_description: str
    prompt: str
    allowed_domains: tuple[str, ...]
    dom_expression: str


TASKS = (
    Task(
        id="example_follow_more_information",
        public_description=(
            "Open Example Domain, follow its visible information link, and "
            "extract the destination heading."
        ),
        prompt=(
            "Open https://example.com. From that page, click the visible link "
            "that offers more information. Do not type, infer, or directly "
            "navigate to the destination URL. On the destination page, read the "
            "main heading and finish with exactly: Heading: <main heading>"
        ),
        allowed_domains=("example.com", "iana.org", "www.iana.org"),
        dom_expression=(
            "() => ({url: location.href, "
            "heading: document.querySelector('h1')?.textContent?.trim() || ''})"
        ),
    ),
    Task(
        id="iana_follow_root_zone",
        public_description=(
            "Open the IANA homepage, follow the visible Root Zone Management "
            "link, and extract the destination heading."
        ),
        prompt=(
            "Open https://www.iana.org/. From the homepage, click the visible "
            "Root Zone Management link. Do not type, infer, or directly navigate "
            "to the destination URL. On the destination page, read the main "
            "heading and finish with exactly: Heading: <main heading>"
        ),
        allowed_domains=("iana.org", "www.iana.org"),
        dom_expression=(
            "() => ({url: location.href, "
            "heading: document.querySelector('h1')?.textContent?.trim() || ''})"
        ),
    ),
    Task(
        id="hn_follow_newest",
        public_description=(
            "Open Hacker News, follow the visible new/newest navigation link, "
            "and extract the first story title."
        ),
        prompt=(
            "Open https://news.ycombinator.com/news. Click the visible 'new' "
            "navigation link near the top of the page; do not type, infer, or "
            "directly navigate to its destination URL. On the resulting page, "
            "read the first story title and finish with exactly: "
            "First story: <title>"
        ),
        allowed_domains=("news.ycombinator.com",),
        dom_expression=(
            "() => ({url: location.href, "
            "first_story: "
            "document.querySelector('.titleline > a')?.textContent?.trim() || ''})"
        ),
    ),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def hostname_allowed(url: str, allowed_domains: tuple[str, ...]) -> bool:
    hostname = (urlparse(url).hostname or "").casefold()
    return any(
        hostname == allowed.casefold()
        or hostname.endswith(f".{allowed.casefold()}")
        for allowed in allowed_domains
    )


def is_target_url(task_id: str, url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/")
    if task_id == "example_follow_more_information":
        return (
            host in {"iana.org", "www.iana.org"}
            and path in {"/domains/example", "/help/example-domains"}
        )
    if task_id == "iana_follow_root_zone":
        return host in {"iana.org", "www.iana.org"} and path == "/domains/root"
    if task_id == "hn_follow_newest":
        return host == "news.ycombinator.com" and path == "/newest"
    return False


def start_url_seen(task_id: str, urls: list[str]) -> bool:
    parsed = [urlparse(url) for url in urls]
    if task_id == "example_follow_more_information":
        return any((item.hostname or "").casefold() == "example.com" for item in parsed)
    if task_id == "iana_follow_root_zone":
        return any(
            (item.hostname or "").casefold() in {"iana.org", "www.iana.org"}
            and item.path in {"", "/"}
            for item in parsed
        )
    if task_id == "hn_follow_newest":
        return any(
            (item.hostname or "").casefold() == "news.ycombinator.com"
            and item.path.rstrip("/") == "/news"
            for item in parsed
        )
    return False


async def capture_target_page(browser: Browser, task: Task) -> tuple[dict[str, Any], list[str]]:
    matching_snapshot: dict[str, Any] = {}
    page_urls: list[str] = []
    for page in await browser.get_pages():
        try:
            snapshot = json.loads(await page.evaluate(task.dom_expression))
            url = str(snapshot.get("url", ""))
            if url:
                page_urls.append(url)
            if hostname_allowed(url, task.allowed_domains) and is_target_url(task.id, url):
                matching_snapshot = snapshot
        except Exception:
            # A closed/background page must not invalidate a correctly completed task.
            continue
    return matching_snapshot, page_urls


def validate(
    task: Task,
    final_result: str | None,
    dom: dict[str, Any],
    visited_urls: list[str],
    action_names: list[str],
) -> tuple[bool, list[dict[str, Any]]]:
    final = normalize(final_result)
    current_url = str(dom.get("url", ""))
    normalized_actions = {normalize(name) for name in action_names}
    common = [
        ("start page observed in agent history", start_url_seen(task.id, visited_urls)),
        ("target page reached", is_target_url(task.id, current_url)),
        (
            "visible link click recorded",
            bool(normalized_actions.intersection(CLICK_ACTION_NAMES)),
        ),
    ]
    if task.id == "example_follow_more_information":
        heading = normalize(dom.get("heading"))
        specific = [
            ("destination heading is Example Domains", heading == "example domains"),
            ("answer uses requested prefix", final.startswith("heading:")),
            ("answer matches live DOM", final == f"heading: {heading}"),
        ]
    elif task.id == "iana_follow_root_zone":
        heading = normalize(dom.get("heading"))
        specific = [
            (
                "destination heading is Root Zone Management",
                heading == "root zone management",
            ),
            ("answer uses requested prefix", final.startswith("heading:")),
            ("answer matches live DOM", final == f"heading: {heading}"),
        ]
    elif task.id == "hn_follow_newest":
        expected = normalize(dom.get("first_story")).rstrip(" .!?")
        actual = normalize(
            re.sub(r"^First story:\s*", "", final_result or "", flags=re.IGNORECASE)
        ).rstrip(" .!?")
        specific = [
            ("live page contains a first story", bool(expected)),
            ("answer uses requested prefix", final.startswith("first story:")),
            ("answer matches live DOM", bool(expected) and actual == expected),
        ]
    else:
        specific = [("known task", False)]
    checks = [
        {"name": name, "passed": bool(passed)} for name, passed in common + specific
    ]
    return all(check["passed"] for check in checks), checks


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def sanitized_error(exc: BaseException) -> dict[str, str]:
    # Exception messages remain private because provider errors may contain
    # request details. The public result exposes only a stable failure class.
    return {
        "error_type": type(exc).__name__,
        "error_summary": "Detailed error retained in private run evidence.",
    }


async def run_once(
    *,
    task: Task,
    repetition: int,
    experiment_id: str,
    output_dir: Path,
    model: str,
    max_steps: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = f"{experiment_id}-{task.id}-r{repetition:02d}"
    started_at = utc_now()
    started_clock = time.perf_counter()
    private_dir = output_dir / "_private" / experiment_id
    # Keep the profile path short enough for Chromium's nested files on Windows.
    task_number = next(
        index for index, candidate in enumerate(TASKS, start=1) if candidate.id == task.id
    )
    profile_dir = private_dir / "p" / f"t{task_number}r{repetition}"
    browser = Browser(
        headless=True,
        keep_alive=True,
        allowed_domains=list(task.allowed_domains),
        user_data_dir=profile_dir,
        highlight_elements=False,
    )
    public_result: dict[str, Any]
    private_result: dict[str, Any]
    try:
        agent = Agent(
            task=task.prompt,
            llm=ChatOpenAI(model=model),
            browser=browser,
            use_judge=False,
            use_vision=False,
            calculate_cost=True,
            max_failures=2,
            max_actions_per_step=1,
            step_timeout=90,
            llm_timeout=90,
            directly_open_url=True,
        )
        history = await asyncio.wait_for(
            agent.run(max_steps=max_steps),
            timeout=480,
        )
        final_result = history.final_result()
        action_names = [str(name) for name in history.action_names()]
        visited_urls = [str(url) for url in history.urls() if url]
        dom, open_page_urls = await capture_target_page(browser, task)
        validator_passed, validator_checks = validate(
            task,
            final_result,
            dom,
            visited_urls,
            action_names,
        )
        usage = getattr(history, "usage", None)
        cost = (
            round(float(usage.total_cost), 8)
            if usage is not None and usage.total_cost is not None
            else None
        )
        agent_done = bool(history.is_done())
        agent_reported_success = bool(history.is_successful())
        success = agent_done and agent_reported_success and validator_passed
        duration = round(time.perf_counter() - started_clock, 2)
        public_result = {
            "run_id": run_id,
            "framework": FRAMEWORK,
            "framework_version": FRAMEWORK_VERSION,
            "framework_repository": FRAMEWORK_REPOSITORY,
            "framework_commit": FRAMEWORK_COMMIT,
            "model": model,
            "task_id": task.id,
            "task": task.public_description,
            "repetition": repetition,
            "started_at": started_at,
            "duration_seconds": duration,
            "steps": int(history.number_of_steps()),
            "reported_cost_usd": cost,
            "status": "success" if success else "failed_validation",
            "success": success,
            "agent_done": agent_done,
            "agent_reported_success": agent_reported_success,
            "validator_passed": validator_passed,
            "validator_checks": validator_checks,
            "action_names": action_names,
            "error_type": None,
        }
        private_result = {
            **public_result,
            "prompt": task.prompt,
            "final_result": final_result,
            "observed_target_dom": dom,
            "visited_urls": visited_urls,
            "open_page_urls": open_page_urls,
            "history_errors": [str(error) for error in history.errors() if error],
            "profile_directory": str(profile_dir),
        }
    except Exception as exc:
        duration = round(time.perf_counter() - started_clock, 2)
        public_result = {
            "run_id": run_id,
            "framework": FRAMEWORK,
            "framework_version": FRAMEWORK_VERSION,
            "framework_repository": FRAMEWORK_REPOSITORY,
            "framework_commit": FRAMEWORK_COMMIT,
            "model": model,
            "task_id": task.id,
            "task": task.public_description,
            "repetition": repetition,
            "started_at": started_at,
            "duration_seconds": duration,
            "steps": None,
            "reported_cost_usd": None,
            "status": "error",
            "success": False,
            "agent_done": False,
            "agent_reported_success": False,
            "validator_passed": False,
            "validator_checks": [],
            "action_names": [],
            **sanitized_error(exc),
        }
        private_result = {
            **public_result,
            "prompt": task.prompt,
            "error_message": str(exc),
            "profile_directory": str(profile_dir),
        }
    finally:
        try:
            await browser.kill()
        except Exception:
            pass
    atomic_write_json(private_dir / "runs" / f"{run_id}.json", private_result)
    return public_result, private_result


def median_or_none(values: list[float]) -> float | None:
    return round(float(statistics.median(values)), 4) if values else None


def build_aggregate(
    *,
    experiment_id: str,
    model: str,
    tasks: list[Task],
    repetitions: int,
    cost_cap: float,
    runs: list[dict[str, Any]],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    completed = [
        run for run in runs if not str(run["status"]).startswith("skipped_")
    ]
    reported_costs = [
        float(run["reported_cost_usd"])
        for run in completed
        if run.get("reported_cost_usd") is not None
    ]
    task_summaries = []
    for task in tasks:
        task_runs = [run for run in completed if run["task_id"] == task.id]
        successful = [run for run in task_runs if run["success"]]
        task_summaries.append(
            {
                "task_id": task.id,
                "task": task.public_description,
                "completed_runs": len(task_runs),
                "successful_runs": len(successful),
                "success_rate": (
                    round(len(successful) / len(task_runs), 4) if task_runs else None
                ),
                "median_duration_seconds": median_or_none(
                    [float(run["duration_seconds"]) for run in task_runs]
                ),
                "median_steps": median_or_none(
                    [float(run["steps"]) for run in task_runs if run["steps"] is not None]
                ),
                "reported_cost_usd": round(
                    sum(
                        float(run["reported_cost_usd"])
                        for run in task_runs
                        if run.get("reported_cost_usd") is not None
                    ),
                    8,
                ),
            }
        )
    return {
        "schema_version": "1.0",
        "experiment_id": experiment_id,
        "framework": FRAMEWORK,
        "framework_version": FRAMEWORK_VERSION,
        "framework_repository": FRAMEWORK_REPOSITORY,
        "framework_commit": FRAMEWORK_COMMIT,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "model": model,
        "headless": True,
        "use_vision": False,
        "use_judge": False,
        "profile_policy": "new empty user-data directory for every run",
        "local_cdp_proxy_bypass": "NO_PROXY=127.0.0.1,localhost",
        "navigation_policy": (
            "prompt provides only the start URL; target must be reached by a "
            "recorded visible-link click"
        ),
        "validation": "deterministic URL, live DOM, answer, and action-history checks",
        "planned_tasks": len(tasks),
        "planned_repetitions_per_task": repetitions,
        "planned_runs": len(tasks) * repetitions,
        "completed_runs": len(completed),
        "successful_runs": sum(1 for run in completed if run["success"]),
        "overall_success_rate": (
            round(sum(1 for run in completed if run["success"]) / len(completed), 4)
            if completed
            else None
        ),
        "median_duration_seconds": median_or_none(
            [float(run["duration_seconds"]) for run in completed]
        ),
        "median_steps": median_or_none(
            [float(run["steps"]) for run in completed if run["steps"] is not None]
        ),
        "reported_cost_usd": round(sum(reported_costs), 8),
        "runs_with_unreported_cost": sum(
            1 for run in completed if run.get("reported_cost_usd") is None
        ),
        "cost_cap_usd": cost_cap,
        "cost_cap_policy": (
            "No new run starts after cumulative reported cost reaches the cap; "
            "a final in-flight run may cross it. No further paid work starts "
            "after a run with unreported cost."
        ),
        "started_at": started_at,
        "completed_at": completed_at,
        "task_summaries": task_summaries,
        "claim_boundary": (
            "Public, read-only micro-tasks only; results are not an official "
            "Browser-Use Benchmark score and do not measure general web-agent capability."
        ),
    }


EVIDENCE_ROOT = Path(__file__).resolve().parents[2] / "evidence"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--model", default=os.getenv("WEEK1_BROWSER_USE_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Results directory (not inside research/evidence); private details go to its _private/",
    )
    parser.add_argument(
        "--cost-cap-usd",
        type=float,
        default=2.4,
        help=(
            "Formal-run ceiling; USD 0.10 is reserved for a validation smoke "
            "run within the Browser-Use USD 2.50 allocation."
        ),
    )
    parser.add_argument("--max-steps", type=int, default=7)
    parser.add_argument(
        "--task",
        action="append",
        choices=[task.id for task in TASKS],
        help="Optional task filter; repeat for multiple tasks.",
    )
    args = parser.parse_args(argv)
    output, evidence = args.output_dir.resolve(), EVIDENCE_ROOT.resolve()
    if output == evidence or evidence in output.parents:
        parser.error("--output-dir must not be inside research/evidence; archived evidence is read-only")
    if args.repetitions < 1:
        parser.error("--repetitions must be at least 1")
    if args.cost_cap_usd <= 0:
        parser.error("--cost-cap-usd must be positive")
    if args.max_steps < 2:
        parser.error("--max-steps must be at least 2")
    return args


async def async_main(args: argparse.Namespace) -> int:
    installed_version = importlib.metadata.version(FRAMEWORK)
    if installed_version != FRAMEWORK_VERSION:
        raise RuntimeError(
            f"Expected {FRAMEWORK}=={FRAMEWORK_VERSION}, found {installed_version}"
        )
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required but is not set")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    experiment_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started_at = utc_now()
    selected_tasks = [
        task for task in TASKS if not args.task or task.id in set(args.task)
    ]
    public_runs: list[dict[str, Any]] = []
    cumulative_reported_cost = 0.0
    cost_accounting_blocked = False

    for task in selected_tasks:
        for repetition in range(1, args.repetitions + 1):
            if cost_accounting_blocked:
                public_runs.append(
                    {
                        "run_id": f"{experiment_id}-{task.id}-r{repetition:02d}",
                        "framework": FRAMEWORK,
                        "framework_version": FRAMEWORK_VERSION,
                        "model": args.model,
                        "task_id": task.id,
                        "task": task.public_description,
                        "repetition": repetition,
                        "status": "skipped_unreported_cost",
                        "success": False,
                        "reported_cost_usd": None,
                        "error_type": None,
                    }
                )
                continue
            if cumulative_reported_cost >= args.cost_cap_usd:
                public_runs.append(
                    {
                        "run_id": f"{experiment_id}-{task.id}-r{repetition:02d}",
                        "framework": FRAMEWORK,
                        "framework_version": FRAMEWORK_VERSION,
                        "model": args.model,
                        "task_id": task.id,
                        "task": task.public_description,
                        "repetition": repetition,
                        "status": "skipped_cost_cap",
                        "success": False,
                        "reported_cost_usd": None,
                        "error_type": None,
                    }
                )
                continue
            print(
                f"[{utc_now()}] running {task.id} repetition "
                f"{repetition}/{args.repetitions}",
                flush=True,
            )
            public_result, _ = await run_once(
                task=task,
                repetition=repetition,
                experiment_id=experiment_id,
                output_dir=output_dir,
                model=args.model,
                max_steps=args.max_steps,
            )
            public_runs.append(public_result)
            if public_result.get("reported_cost_usd") is not None:
                cumulative_reported_cost += float(
                    public_result["reported_cost_usd"]
                )
            else:
                # If a run reached the provider but usage cannot be accounted
                # for, stop starting paid work rather than risk the hard budget.
                cost_accounting_blocked = True
            atomic_write_json(
                output_dir / "sanitized_runs.json",
                {
                    "schema_version": "1.0",
                    "experiment_id": experiment_id,
                    "runs": public_runs,
                },
            )
            print(
                f"[{utc_now()}] {public_result['run_id']}: "
                f"{public_result['status']}; "
                f"cost=${public_result.get('reported_cost_usd')}",
                flush=True,
            )

    completed_at = utc_now()
    aggregate = build_aggregate(
        experiment_id=experiment_id,
        model=args.model,
        tasks=selected_tasks,
        repetitions=args.repetitions,
        cost_cap=args.cost_cap_usd,
        runs=public_runs,
        started_at=started_at,
        completed_at=completed_at,
    )
    atomic_write_json(
        output_dir / "sanitized_runs.json",
        {
            "schema_version": "1.0",
            "experiment_id": experiment_id,
            "runs": public_runs,
        },
    )
    atomic_write_json(output_dir / "aggregate.json", aggregate)
    print(
        f"completed {aggregate['completed_runs']} runs; "
        f"{aggregate['successful_runs']} passed; "
        f"reported cost=${aggregate['reported_cost_usd']:.8f}",
        flush=True,
    )
    complete_and_valid = (
        aggregate["completed_runs"] == aggregate["planned_runs"]
        and aggregate["successful_runs"] == aggregate["completed_runs"]
    )
    return 0 if complete_and_valid else 1


def main() -> None:
    os.environ.setdefault("BROWSER_USE_SETUP_LOGGING", "false")
    logging.getLogger("browser_use").setLevel(logging.WARNING)
    logging.getLogger("Agent").setLevel(logging.WARNING)
    try:
        exit_code = asyncio.run(async_main(parse_args()))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
