"""Repeated Skyvern evaluation on three public, read-only navigation tasks.

The runner talks to a self-hosted Skyvern API. It reads the generated local
Skyvern API token from the private credentials file, never prints it, and only
writes sanitized results to ``--output-dir``. Detailed browser artifacts stay
in the private state directory ``$CUA_PRIVATE_ROOT/pilots/skyvern`` (mounted by
``docker_start.ps1``), which must lie outside the repository.

``--audit-existing`` recomputes the aggregate from a sanitized runs file and
compares it with the ``aggregate.json`` beside it.  It is read-only unless
``--write`` is given, and it never writes into ``research/evidence``.

Run from the repository root: ``python -m research.pilots.skyvern.runner``.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_ROOT = REPO_ROOT / "research" / "evidence"
PRIVATE_ROOT_ENV = "CUA_PRIVATE_ROOT"
# Host- and time-dependent aggregate fields that an offline re-audit cannot reproduce.
AUDIT_VOLATILE_KEYS = frozenset({"completed_at", "audit_completed_at", "python_version", "platform"})
FRAMEWORK = "skyvern"
FRAMEWORK_VERSION = "1.0.47"
FRAMEWORK_REPOSITORY = "https://github.com/Skyvern-AI/skyvern"
FRAMEWORK_COMMIT = "8e3b40ae072c8d954879f26460b710367db723cb"
RUNTIME_IMAGE_SOURCE_COMMIT = "Not reported in image metadata"
SKYVERN_IMAGE = (
    "public.ecr.aws/skyvern/skyvern@"
    "sha256:ff9ae1aa20f27d90115614f98fc0a796dcb86ee218aedde9f035cd9c2a89b95b"
)
POSTGRES_IMAGE = (
    "postgres@"
    "sha256:f1341c01408dc7278e9d365ed4f860cd3f87dd16b4464ac326fc0f422083a579"
)
DEFAULT_MODEL = "OPENAI_GPT5_5"
DEFAULT_ENGINE = "skyvern-2.0"
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "terminated",
    "timed_out",
    "canceled",
}
CLICK_ACTIONS = {
    "click",
    "click_action",
    "clickelement",
    "click_element",
    "ai_click",
}


@dataclass(frozen=True)
class Task:
    id: str
    public_description: str
    start_url: str
    prompt: str
    target_url: str
    output_key: str
    output_prefix: str


TASKS = (
    Task(
        id="example_follow_more_information",
        public_description=(
            "Open Example Domain, follow its visible information link, and "
            "extract the destination heading."
        ),
        start_url="https://example.com",
        prompt=(
            "Click the visible link that offers more information. Do not type, "
            "infer, or directly navigate to the destination URL. On the "
            "destination page, wait for navigation to finish and read the main "
            "heading. The task is NOT complete merely because the click "
            "succeeded. Do not issue COMPLETE in the same plan as the click. "
            "COMPLETE only after the destination page is visibly loaded, and "
            "return the heading in the requested structured field."
        ),
        target_url="https://www.iana.org/help/example-domains",
        output_key="heading",
        output_prefix="Heading:",
    ),
    Task(
        id="iana_follow_root_zone",
        public_description=(
            "Open the IANA homepage, follow the visible Root Zone Management "
            "link, and extract the destination heading."
        ),
        start_url="https://www.iana.org/",
        prompt=(
            "Click the visible Root Zone Management link. Do not type, infer, "
            "or directly navigate to the destination URL. On the destination "
            "page, wait for navigation to finish and read the main heading. "
            "The task is NOT complete merely because the click succeeded. Do "
            "not issue COMPLETE in the same plan as the click. COMPLETE only "
            "after the destination page is visibly loaded, and return the "
            "heading in the requested structured field."
        ),
        target_url="https://www.iana.org/domains/root",
        output_key="heading",
        output_prefix="Heading:",
    ),
    Task(
        id="hn_follow_newest",
        public_description=(
            "Open Hacker News, follow the visible new/newest navigation link, "
            "and extract the first story title."
        ),
        start_url="https://news.ycombinator.com/news",
        prompt=(
            "Click the visible 'new' navigation link near the top of the page. "
            "Do not type, infer, or directly navigate to its destination URL. "
            "On the resulting page, wait for navigation to finish and read the "
            "first story title. The task is NOT complete merely because the "
            "click succeeded. Do not issue COMPLETE in the same plan as the "
            "click. COMPLETE only after the destination page is visibly loaded, "
            "and return the title in the requested structured field."
        ),
        target_url="https://news.ycombinator.com/newest",
        output_key="first_story",
        output_prefix="First story:",
    ),
)


def _inside(path: Path, root: Path) -> bool:
    resolved, parent = path.resolve(), root.resolve()
    return resolved == parent or parent in resolved.parents


def private_state_dir() -> Path:
    """Return ``$CUA_PRIVATE_ROOT/pilots/skyvern``; it must lie outside the repository."""

    value = os.environ.get(PRIVATE_ROOT_ENV)
    if not value:
        raise RuntimeError(f"{PRIVATE_ROOT_ENV} is not set")
    state = Path(value).expanduser().resolve() / "pilots" / "skyvern"
    if _inside(state, REPO_ROOT):
        raise RuntimeError(f"{PRIVATE_ROOT_ENV} must resolve outside the repository")
    return state


def require_writable_output(path: Path) -> Path:
    """Refuse output inside the archived evidence tree."""

    if _inside(path, EVIDENCE_ROOT):
        raise SystemExit(
            f"refusing to write {path}: research/evidence is archival and read-only"
        )
    return path.resolve()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def localhost_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_local_api_key(credentials_file: Path) -> str:
    with credentials_file.open("rb") as handle:
        document = tomllib.load(handle)
    configs = document.get("skyvern", {}).get("configs", [])
    for config in configs:
        for organization in config.get("orgs", []):
            credential = organization.get("cred")
            if isinstance(credential, str) and credential.strip():
                return credential
    raise RuntimeError("No usable local Skyvern API token was found")


def request_json(
    *,
    method: str,
    url: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 60,
) -> Any:
    body = None
    headers = {
        "Accept": "application/json",
        "x-api-key": api_key,
        "x-user-agent": "computer-use-agent-skyvern-pilot/1.0",
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url=url,
        data=body,
        headers=headers,
        method=method,
    )
    with localhost_opener().open(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else None


def fetch_public_html(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "computer-use-agent-pilot-validator/1.0"},
    )
    # Public validators honor the host's standard proxy configuration. Local
    # Skyvern API calls use ``localhost_opener`` so credentials never leave
    # the loopback endpoint through a proxy.
    with urllib.request.urlopen(request, timeout=30) as response:
        encoding = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(encoding, errors="replace")


def strip_markup(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", value))).strip()


def expected_live_value(task: Task) -> str:
    page = fetch_public_html(task.target_url)
    if task.output_key == "heading":
        match = re.search(r"<h1(?:\s[^>]*)?>(.*?)</h1>", page, re.IGNORECASE | re.DOTALL)
    else:
        match = re.search(
            r"<span[^>]*class=[\"'][^\"']*\btitleline\b[^\"']*[\"'][^>]*>"
            r"\s*<a[^>]*>(.*?)</a>",
            page,
            re.IGNORECASE | re.DOTALL,
        )
    if not match:
        raise RuntimeError(f"Independent validator could not parse {task.target_url}")
    return strip_markup(match.group(1))


def timestamped_dom_evidence(
    task: Task,
    workflow_run_id: str | None,
) -> dict[str, Any]:
    if workflow_run_id is None:
        raise RuntimeError("Timestamped DOM validation requires a workflow run id")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", workflow_run_id):
        raise ValueError("Unexpected workflow run id")
    evidence_task_ids = psql_rows(
        "SELECT task_id FROM workflow_run_blocks "
        f"WHERE workflow_run_id = '{workflow_run_id}' "
        "AND block_type = 'extraction' AND task_id IS NOT NULL "
        "ORDER BY created_at;"
    )
    artifact_kind = "pre_extraction"
    if not evidence_task_ids:
        evidence_task_ids = psql_rows(
            "SELECT task_id FROM workflow_run_blocks "
            f"WHERE workflow_run_id = '{workflow_run_id}' "
            "AND block_type = 'navigation' AND task_id IS NOT NULL "
            "ORDER BY created_at;"
        )
        artifact_kind = "completion_action"
    artifacts_root = private_state_dir() / "artifacts"
    candidates: list[Path] = []
    for task_id in evidence_task_ids:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            continue
        candidates.extend(
            artifacts_root.glob(f"*/{task_id}/**/*_html_scrape.html")
        )
    if not candidates:
        for task_id in evidence_task_ids:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
                continue
            candidates.extend(
                artifacts_root.glob(f"*/{task_id}/**/*_html_action.html")
            )
    if not candidates:
        raise RuntimeError("No timestamped extraction DOM artifact was found")
    artifact = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    if artifact.name.endswith("_html_scrape.html"):
        artifact_kind = (
            "pre_extraction"
            if artifact_kind == "pre_extraction"
            else "navigation_snapshot_before_completion"
        )
    page = artifact.read_text(encoding="utf-8", errors="replace")
    if task.output_key == "heading":
        match = re.search(
            r"<h1(?:\s[^>]*)?>(.*?)</h1>",
            page,
            re.IGNORECASE | re.DOTALL,
        )
    else:
        match = re.search(
            r"<span[^>]*class=[\"'][^\"']*\btitleline\b[^\"']*[\"'][^>]*>"
            r"\s*<a[^>]*>(.*?)</a>",
            page,
            re.IGNORECASE | re.DOTALL,
        )
    if not match:
        raise RuntimeError("Timestamped DOM validator could not parse the target")
    timestamp_text = artifact.name.split("_a_", maxsplit=1)[0]
    timestamp_text = re.sub(r"[^0-9TZ+.\-]", ":", timestamp_text)
    return {
        "value": strip_markup(match.group(1)),
        "source": f"timestamped_browser_dom_{artifact_kind}",
        "captured_at": timestamp_text,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }


def dom_validator_evidence(
    task: Task,
    workflow_run_id: str | None,
) -> dict[str, Any]:
    if task.id == "hn_follow_newest":
        return timestamped_dom_evidence(task, workflow_run_id)
    return {
        "value": expected_live_value(task),
        "source": "independently_fetched_live_dom_after_completion",
        "captured_at": utc_now(),
        "sha256": None,
    }


def find_output_value(value: Any, key: str) -> str:
    if isinstance(value, dict):
        for candidate_key, candidate_value in value.items():
            if normalize(candidate_key).replace(" ", "_") == key:
                if isinstance(candidate_value, (str, int, float)):
                    return str(candidate_value)
            nested = find_output_value(candidate_value, key)
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = find_output_value(item, key)
            if nested:
                return nested
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return find_output_value(json.loads(text), key)
            except json.JSONDecodeError:
                pass
        prefix = "Heading:" if key == "heading" else "First story:"
        match = re.search(rf"{re.escape(prefix)}\s*(.+)", value, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def psql_rows(sql: str) -> list[str]:
    process = subprocess.run(
        [
            "docker",
            "exec",
            "week1-skyvern-postgres",
            "psql",
            "-U",
            "skyvern",
            "-d",
            "skyvern",
            "-At",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    )
    return [line for line in process.stdout.splitlines() if line]


def database_evidence(
    provider_run_id: str,
    workflow_run_id: str | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", provider_run_id):
        raise ValueError("Unexpected provider run id")
    if workflow_run_id is not None and not re.fullmatch(
        r"[A-Za-z0-9_-]+", workflow_run_id
    ):
        raise ValueError("Unexpected workflow run id")
    run_rows = psql_rows(
        "SELECT json_build_object("
        "'llm_cost', llm_cost, "
        "'proxy_cost', proxy_cost, "
        "'captcha_cost', captcha_cost, "
        "'compute_cost', compute_cost, "
        "'duration_ms', duration_ms, "
        "'status', status"
        ")::text FROM task_runs "
        f"WHERE run_id = '{provider_run_id}' "
        "ORDER BY created_at DESC LIMIT 1;"
    )
    task_ids = [provider_run_id]
    block_cost = 0.0
    if workflow_run_id is not None:
        task_ids = psql_rows(
            "SELECT task_id FROM workflow_run_blocks "
            f"WHERE workflow_run_id = '{workflow_run_id}' "
            "AND task_id IS NOT NULL ORDER BY created_at;"
        )
        block_cost_rows = psql_rows(
            "SELECT COALESCE(sum(llm_cost), 0)::text "
            "FROM workflow_run_blocks "
            f"WHERE workflow_run_id = '{workflow_run_id}';"
        )
        block_cost = float(block_cost_rows[0]) if block_cost_rows else 0.0
    safe_task_ids = [
        task_id
        for task_id in task_ids
        if re.fullmatch(r"[A-Za-z0-9_-]+", task_id)
    ]
    quoted_task_ids = ", ".join(f"'{task_id}'" for task_id in safe_task_ids)
    task_id_clause = quoted_task_ids or "''"
    step_rows = psql_rows(
        "SELECT json_build_object("
        "'order', \"order\", "
        "'status', status, "
        "'step_cost', step_cost, "
        "'input_tokens', input_token_count, "
        "'output_tokens', output_token_count, "
        "'reasoning_tokens', reasoning_token_count, "
        "'cached_tokens', cached_token_count, "
        "'model', last_llm_model, "
        "'output', output"
        ")::text FROM steps "
        f"WHERE task_id IN ({task_id_clause}) "
        "ORDER BY \"order\", retry_index;"
    )
    run_cost = json.loads(run_rows[0]) if run_rows else {}
    steps = [json.loads(row) for row in step_rows]
    action_names: set[str] = set()
    urls: set[str] = set()

    def visit(value: Any, parent_key: str = "") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_norm = normalize(key).replace(" ", "_")
                if key_norm in {"action_type", "action_name"} and isinstance(nested, str):
                    action_names.add(normalize(nested).replace(" ", "_"))
                elif key_norm == "action" and isinstance(nested, str):
                    action_names.add(normalize(nested).replace(" ", "_"))
                visit(nested, key_norm)
        elif isinstance(value, list):
            for nested in value:
                visit(nested, parent_key)
        elif isinstance(value, str):
            if parent_key in {
                "url",
                "current_url",
                "final_url",
                "starting_url",
                "page_url",
            } and re.match(r"^https?://", value):
                urls.add(value)
            if parent_key in {"action", "action_type", "action_name"}:
                action_names.add(normalize(value).replace(" ", "_"))

    for step in steps:
        visit(step.get("output"))

    step_costs = [
        float(step["step_cost"])
        for step in steps
        if step.get("step_cost") is not None
    ]
    if workflow_run_id is not None:
        llm_cost = block_cost + sum(step_costs)
    else:
        llm_cost = run_cost.get("llm_cost")
        if llm_cost is None:
            llm_cost = sum(step_costs) if step_costs else None
    reported_cost = (
        round(
            sum(
                float(value)
                for value in (
                    llm_cost,
                    run_cost.get("proxy_cost"),
                    run_cost.get("captcha_cost"),
                )
                if value is not None
            ),
            8,
        )
        if any(
            value is not None
            for value in (
                llm_cost,
                run_cost.get("proxy_cost"),
                run_cost.get("captcha_cost"),
            )
        )
        else None
    )
    return {
        "step_count": len(steps),
        "reported_cost_usd": reported_cost,
        "llm_cost_usd": float(llm_cost) if llm_cost is not None else None,
        "input_tokens": sum(int(step.get("input_tokens") or 0) for step in steps),
        "output_tokens": sum(int(step.get("output_tokens") or 0) for step in steps),
        "reasoning_tokens": sum(int(step.get("reasoning_tokens") or 0) for step in steps),
        "cached_tokens": sum(int(step.get("cached_tokens") or 0) for step in steps),
        "models": sorted(
            {
                str(step["model"])
                for step in steps
                if step.get("model") is not None
            }
        ),
        "action_names": sorted(action_names),
        "observed_urls": sorted(urls),
        "database_status": run_cost.get("status"),
    }


def url_seen(urls: list[str], expected: str) -> bool:
    expected_norm = expected.rstrip("/")
    return any(url.rstrip("/") == expected_norm for url in urls)


def click_seen(actions: list[str]) -> bool:
    return any(
        action in CLICK_ACTIONS or "click" in action
        for action in (normalize(item).replace(" ", "_") for item in actions)
    )


def build_validator_checks(
    *,
    task: Task,
    provider_status: str,
    evidence: dict[str, Any],
    observed: str,
    expected: str,
) -> list[dict[str, Any]]:
    return [
        {
            "name": "Skyvern reports completed",
            "passed": provider_status == "completed",
        },
        {
            "name": "start page observed in step evidence",
            "passed": url_seen(evidence["observed_urls"], task.start_url),
        },
        {
            "name": "exact target page observed in step evidence",
            "passed": url_seen(evidence["observed_urls"], task.target_url),
        },
        {
            "name": "visible-link click action recorded",
            "passed": click_seen(evidence["action_names"]),
        },
        {
            "name": "structured output field present",
            "passed": bool(observed),
        },
        {
            "name": "output matches deterministic DOM validator",
            "passed": normalize(observed).rstrip(" .!?")
            == normalize(expected).rstrip(" .!?"),
        },
    ]


def poll_run(
    *,
    api_base: str,
    api_key: str,
    provider_run_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_response: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_response = request_json(
            method="GET",
            url=f"{api_base}/v1/runs/{provider_run_id}",
            api_key=api_key,
            timeout=30,
        )
        status = normalize(last_response.get("status"))
        if status in TERMINAL_STATUSES:
            return last_response
        time.sleep(5)
    try:
        request_json(
            method="POST",
            url=f"{api_base}/v1/runs/{provider_run_id}/cancel",
            api_key=api_key,
            payload=None,
            timeout=30,
        )
    except Exception:
        pass
    raise TimeoutError(f"Skyvern run exceeded {timeout_seconds:.0f} seconds")


def sanitized_error(exc: BaseException) -> dict[str, str]:
    if isinstance(exc, urllib.error.HTTPError):
        category = f"HTTP_{exc.code}"
    else:
        category = type(exc).__name__
    return {
        "error_type": category,
        "error_summary": "Detailed failure retained only in local service logs.",
    }


def run_once(
    *,
    task: Task,
    repetition: int,
    experiment_id: str,
    api_base: str,
    api_key: str,
    max_steps: int,
    timeout_seconds: float,
    engine: str,
) -> dict[str, Any]:
    local_run_id = f"{experiment_id}-{task.id}-r{repetition:02d}"
    started_at = utc_now()
    started_clock = time.perf_counter()
    provider_run_id: str | None = None
    try:
        payload = {
            "prompt": task.prompt,
            "url": task.start_url,
            "engine": engine,
            "title": local_run_id,
            "proxy_location": "NONE",
            "data_extraction_schema": {
                "type": "object",
                "properties": {task.output_key: {"type": "string"}},
                "required": [task.output_key],
                "additionalProperties": False,
            },
            "max_steps": max_steps,
            "include_action_history_in_verification": True,
            "run_with": "agent",
        }
        created = request_json(
            method="POST",
            url=f"{api_base}/v1/run/tasks",
            api_key=api_key,
            payload=payload,
            timeout=90,
        )
        provider_run_id = str(created["run_id"])
        final = (
            created
            if normalize(created.get("status")) in TERMINAL_STATUSES
            else poll_run(
                api_base=api_base,
                api_key=api_key,
                provider_run_id=provider_run_id,
                timeout_seconds=timeout_seconds,
            )
        )
        app_url = str(final.get("app_url") or created.get("app_url") or "")
        workflow_match = re.search(r"/runs/(wr_[A-Za-z0-9_-]+)", app_url)
        workflow_run_id = workflow_match.group(1) if workflow_match else None
        evidence = database_evidence(provider_run_id, workflow_run_id)
        dom_evidence = dom_validator_evidence(task, workflow_run_id)
        expected = str(dom_evidence["value"])
        observed = find_output_value(final.get("output"), task.output_key)
        final_status = normalize(final.get("status"))
        checks = build_validator_checks(
            task=task,
            provider_status=final_status,
            evidence=evidence,
            observed=observed,
            expected=expected,
        )
        validator_passed = all(check["passed"] for check in checks)
        success = final_status == "completed" and validator_passed
        return {
            "run_id": local_run_id,
            "provider_run_id": provider_run_id,
            "workflow_run_id": workflow_run_id,
            "framework": FRAMEWORK,
            "framework_version": FRAMEWORK_VERSION,
            "framework_repository": FRAMEWORK_REPOSITORY,
            "framework_commit": FRAMEWORK_COMMIT,
            "framework_commit_scope": (
                "official source checkout used for API and deployment review"
            ),
            "runtime_image_source_commit": RUNTIME_IMAGE_SOURCE_COMMIT,
            "image": SKYVERN_IMAGE,
            "engine": engine,
            "model_configuration": DEFAULT_MODEL,
            "task_id": task.id,
            "task": task.public_description,
            "repetition": repetition,
            "started_at": started_at,
            "duration_seconds": round(time.perf_counter() - started_clock, 2),
            "steps": int(final.get("step_count") or evidence["step_count"]),
            "reported_cost_usd": evidence["reported_cost_usd"],
            "llm_cost_usd": evidence["llm_cost_usd"],
            "input_tokens": evidence["input_tokens"],
            "output_tokens": evidence["output_tokens"],
            "reasoning_tokens": evidence["reasoning_tokens"],
            "cached_tokens": evidence["cached_tokens"],
            "models": evidence["models"],
            "status": "success" if success else "failed_validation",
            "success": success,
            "provider_status": final_status,
            "validator_passed": validator_passed,
            "validator_checks": checks,
            "action_names": evidence["action_names"],
            "observed_urls": evidence["observed_urls"],
            "observed_value": observed,
            "expected_dom_value": expected,
            "dom_validator_source": dom_evidence["source"],
            "dom_evidence_captured_at": dom_evidence["captured_at"],
            "dom_evidence_sha256": dom_evidence["sha256"],
            "error_type": None,
        }
    except Exception as exc:
        return {
            "run_id": local_run_id,
            "provider_run_id": provider_run_id,
            "workflow_run_id": None,
            "framework": FRAMEWORK,
            "framework_version": FRAMEWORK_VERSION,
            "framework_repository": FRAMEWORK_REPOSITORY,
            "framework_commit": FRAMEWORK_COMMIT,
            "framework_commit_scope": (
                "official source checkout used for API and deployment review"
            ),
            "runtime_image_source_commit": RUNTIME_IMAGE_SOURCE_COMMIT,
            "image": SKYVERN_IMAGE,
            "engine": engine,
            "model_configuration": DEFAULT_MODEL,
            "task_id": task.id,
            "task": task.public_description,
            "repetition": repetition,
            "started_at": started_at,
            "duration_seconds": round(time.perf_counter() - started_clock, 2),
            "steps": None,
            "reported_cost_usd": None,
            "status": "error",
            "success": False,
            "provider_status": None,
            "validator_passed": False,
            "validator_checks": [],
            "action_names": [],
            "observed_urls": [],
            **sanitized_error(exc),
        }


def recover_existing_run(
    *,
    prior: dict[str, Any],
    task: Task,
    api_base: str,
    api_key: str,
) -> dict[str, Any]:
    provider_run_id = str(prior.get("provider_run_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", provider_run_id):
        raise ValueError("Existing result has no safe provider run id")
    final = request_json(
        method="GET",
        url=f"{api_base}/v1/runs/{provider_run_id}",
        api_key=api_key,
        timeout=30,
    )
    app_url = str(final.get("app_url") or "")
    workflow_match = re.search(r"/runs/(wr_[A-Za-z0-9_-]+)", app_url)
    workflow_run_id = workflow_match.group(1) if workflow_match else None
    evidence = database_evidence(provider_run_id, workflow_run_id)
    dom_evidence = dom_validator_evidence(task, workflow_run_id)
    expected = str(dom_evidence["value"])
    observed = find_output_value(final.get("output"), task.output_key)
    final_status = normalize(final.get("status"))
    checks = build_validator_checks(
        task=task,
        provider_status=final_status,
        evidence=evidence,
        observed=observed,
        expected=expected,
    )
    validator_passed = all(check["passed"] for check in checks)
    success = final_status == "completed" and validator_passed
    recovered = dict(prior)
    recovered.pop("error_summary", None)
    recovered.pop("expected_live_value", None)
    recovered.update(
        {
            "workflow_run_id": workflow_run_id,
            "framework_commit_scope": (
                "official source checkout used for API and deployment review"
            ),
            "runtime_image_source_commit": RUNTIME_IMAGE_SOURCE_COMMIT,
            "steps": int(final.get("step_count") or evidence["step_count"]),
            "reported_cost_usd": evidence["reported_cost_usd"],
            "llm_cost_usd": evidence["llm_cost_usd"],
            "input_tokens": evidence["input_tokens"],
            "output_tokens": evidence["output_tokens"],
            "reasoning_tokens": evidence["reasoning_tokens"],
            "cached_tokens": evidence["cached_tokens"],
            "models": evidence["models"],
            "status": "success" if success else "failed_validation",
            "success": success,
            "provider_status": final_status,
            "validator_passed": validator_passed,
            "validator_checks": checks,
            "action_names": evidence["action_names"],
            "observed_urls": evidence["observed_urls"],
            "observed_value": observed,
            "expected_dom_value": expected,
            "dom_validator_source": dom_evidence["source"],
            "dom_evidence_captured_at": dom_evidence["captured_at"],
            "dom_evidence_sha256": dom_evidence["sha256"],
            "error_type": None,
            "recovered_from_provider_evidence": True,
            "recovery_reason": (
                "Original publication pass could not reach the public DOM "
                "validator because it bypassed the host proxy."
            ),
        }
    )
    return recovered


def median_or_none(values: list[float]) -> float | None:
    return round(float(statistics.median(values)), 4) if values else None


def build_aggregate(
    *,
    experiment_id: str,
    repetitions: int,
    max_steps: int,
    cost_cap: float,
    runs: list[dict[str, Any]],
    started_at: str,
    completed_at: str,
    engine: str,
) -> dict[str, Any]:
    completed = [
        run for run in runs if not str(run["status"]).startswith("skipped_")
    ]
    skipped_cost_cap = [
        run for run in runs if run["status"] == "skipped_cost_cap"
    ]
    skipped_unreported_cost = [
        run for run in runs if run["status"] == "skipped_unreported_cost"
    ]
    task_summaries = []
    for task in TASKS:
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
                    [
                        float(run["steps"])
                        for run in task_runs
                        if run.get("steps") is not None
                    ]
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
    reported_costs = [
        float(run["reported_cost_usd"])
        for run in completed
        if run.get("reported_cost_usd") is not None
    ]
    expected_run_matrix = {
        (task.id, repetition)
        for task in TASKS
        for repetition in range(1, repetitions + 1)
    }
    actual_run_matrix = [
        (str(run["task_id"]), int(run["repetition"])) for run in completed
    ]
    provider_run_ids = [
        str(run["provider_run_id"])
        for run in completed
        if run.get("provider_run_id")
    ]
    return {
        "schema_version": "1.0",
        "experiment_id": experiment_id,
        "framework": FRAMEWORK,
        "framework_version": FRAMEWORK_VERSION,
        "framework_repository": FRAMEWORK_REPOSITORY,
        "framework_commit": FRAMEWORK_COMMIT,
        "framework_commit_scope": (
            "official source checkout used for API and deployment review"
        ),
        "runtime_image_source_commit": RUNTIME_IMAGE_SOURCE_COMMIT,
        "skyvern_image": SKYVERN_IMAGE,
        "postgres_image": POSTGRES_IMAGE,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "engine": engine,
        "model_configuration": DEFAULT_MODEL,
        "headful_in_virtual_display": True,
        "profile_policy": "fresh self-hosted browser run for every task repetition",
        "navigation_policy": (
            "request provides only the start URL; target must be reached by a "
            "recorded visible-link click"
        ),
        "validation": (
            "provider status, exact observed URL, click action, structured "
            "output, and deterministic DOM evidence (independent live fetch "
            "for stable pages; timestamped run DOM captured before completion "
            "or structured output for Hacker News)"
        ),
        "planned_tasks": len(TASKS),
        "planned_repetitions_per_task": repetitions,
        "planned_runs": len(TASKS) * repetitions,
        "max_steps_per_run": max_steps,
        "completed_runs": len(completed),
        "successful_runs": sum(1 for run in completed if run["success"]),
        "skipped_cost_cap_runs": len(skipped_cost_cap),
        "skipped_unreported_cost_runs": len(skipped_unreported_cost),
        "all_planned_runs_completed": len(completed) == len(TASKS) * repetitions,
        "run_matrix_complete": (
            len(actual_run_matrix) == len(set(actual_run_matrix))
            and set(actual_run_matrix) == expected_run_matrix
        ),
        "provider_run_ids_unique": (
            len(provider_run_ids) == len(set(provider_run_ids))
            and len(provider_run_ids) == len(completed)
        ),
        "all_validator_checks_passed": all(
            bool(run.get("validator_passed"))
            and bool(run.get("validator_checks"))
            and all(
                bool(check.get("passed"))
                for check in run.get("validator_checks", [])
            )
            for run in completed
        ),
        "overall_success_rate": (
            round(sum(1 for run in completed if run["success"]) / len(completed), 4)
            if completed
            else None
        ),
        "median_duration_seconds": median_or_none(
            [float(run["duration_seconds"]) for run in completed]
        ),
        "median_steps": median_or_none(
            [
                float(run["steps"])
                for run in completed
                if run.get("steps") is not None
            ]
        ),
        "reported_cost_usd": round(sum(reported_costs), 8),
        "reported_cost_scope": (
            "Skyvern PostgreSQL workflow_run_blocks.llm_cost plus child "
            "steps.step_cost; not independently reconciled to a provider invoice"
        ),
        "runs_with_unreported_cost": sum(
            1 for run in completed if run.get("reported_cost_usd") is None
        ),
        "cost_cap_usd": cost_cap,
        "cost_cap_policy": (
            "No new run starts after cumulative reported LLM/proxy/CAPTCHA "
            "cost reaches the cap; a final in-flight run may cross it. If a "
            "completed run has no auditable cost, no further paid run starts."
        ),
        "started_at": started_at,
        "completed_at": completed_at,
        "task_summaries": task_summaries,
        "claim_boundary": (
            "Public, read-only micro-tasks only; results are not an official "
            "Skyvern benchmark score and do not measure general web-agent capability."
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=7)
    parser.add_argument(
        "--engine",
        choices=["skyvern-1.0", "skyvern-2.0"],
        default=DEFAULT_ENGINE,
    )
    parser.add_argument("--cost-cap-usd", type=float, default=3.0)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--credentials-file",
        type=Path,
        help="Local Skyvern credentials (default: $CUA_PRIVATE_ROOT/pilots/skyvern/.skyvern/credentials.toml)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for sanitized_runs.json and aggregate.json (required for a live run)",
    )
    parser.add_argument(
        "--audit-existing",
        type=Path,
        help=(
            "Recompute the aggregate from an existing sanitized runs file and "
            "compare it with the aggregate.json beside it, without starting "
            "Skyvern or making paid API calls. Read-only unless --write."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="With --audit-existing: rewrite the runs file and aggregate.json in place",
    )
    parser.add_argument(
        "--resume-existing",
        type=Path,
        help=(
            "Resume a partial sanitized runs file. Existing provider-completed "
            "errors are re-audited in place before any missing paid run starts."
        ),
    )
    parser.add_argument(
        "--task",
        action="append",
        choices=[task.id for task in TASKS],
    )
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("--repetitions must be at least 1")
    if args.max_steps < 2:
        parser.error("--max-steps must be at least 2")
    if args.cost_cap_usd <= 0 or args.cost_cap_usd > 3:
        parser.error("--cost-cap-usd must be positive and no greater than 3")
    if args.timeout_seconds < 30:
        parser.error("--timeout-seconds must be at least 30")
    if args.audit_existing is not None and args.resume_existing is not None:
        parser.error("--audit-existing and --resume-existing are mutually exclusive")
    if args.write and args.audit_existing is None:
        parser.error("--write is valid only with --audit-existing")
    if args.audit_existing is None and args.resume_existing is None and args.output_dir is None:
        parser.error("a live run requires --output-dir (outside research/evidence)")
    return args


def audit_existing(args: argparse.Namespace) -> None:
    runs_path = args.audit_existing.resolve()
    if args.write:
        require_writable_output(runs_path)
    document = json.loads(runs_path.read_text(encoding="utf-8"))
    runs = document.get("runs")
    if not isinstance(runs, list):
        raise ValueError("audit input must contain a runs array")
    for run in runs:
        run["framework_commit_scope"] = (
            "official source checkout used for API and deployment review"
        )
        run["runtime_image_source_commit"] = RUNTIME_IMAGE_SOURCE_COMMIT
    started_values = [
        str(run["started_at"]) for run in runs if run.get("started_at")
    ]
    started_at = min(started_values) if started_values else utc_now()
    completed_at = utc_now()
    aggregate = build_aggregate(
        experiment_id=str(document.get("experiment_id") or "Not reported"),
        repetitions=args.repetitions,
        max_steps=args.max_steps,
        cost_cap=args.cost_cap_usd,
        runs=runs,
        started_at=started_at,
        completed_at=completed_at,
        engine=args.engine,
    )
    aggregate["audit_mode"] = "offline sanitized-result audit; no API call"
    aggregate["audit_completed_at"] = completed_at
    aggregate["audited_runs_canonical_json_sha256"] = hashlib.sha256(
        json.dumps(runs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    existing_path = runs_path.parent / "aggregate.json"
    mismatched: list[str] = []
    if existing_path.is_file():
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
        mismatched = sorted(
            key
            for key in set(existing) | set(aggregate)
            if key not in AUDIT_VOLATILE_KEYS and existing.get(key) != aggregate.get(key)
        )
    if args.write:
        atomic_write_json(
            runs_path,
            {
                "schema_version": document.get("schema_version", "1.0"),
                "experiment_id": document.get("experiment_id"),
                "runs": runs,
            },
        )
        atomic_write_json(existing_path, aggregate)
    all_planned_runs_passed = (
        aggregate["all_planned_runs_completed"]
        and aggregate["run_matrix_complete"]
        and aggregate["provider_run_ids_unique"]
        and aggregate["all_validator_checks_passed"]
        and aggregate["successful_runs"] == aggregate["planned_runs"]
        and aggregate["runs_with_unreported_cost"] == 0
    )
    comparison = (
        "no aggregate.json to compare"
        if not existing_path.is_file() or args.write
        else "matches aggregate.json"
        if not mismatched
        else "differs from aggregate.json in: " + ", ".join(mismatched)
    )
    print(
        "offline audit: "
        f"{aggregate['completed_runs']}/{aggregate['planned_runs']} completed, "
        f"{aggregate['successful_runs']} passed, "
        f"{aggregate['runs_with_unreported_cost']} unreported costs; "
        f"{comparison}; writes_performed={bool(args.write)}",
        flush=True,
    )
    consistent = args.write or not mismatched
    raise SystemExit(0 if all_planned_runs_passed and consistent else 1)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.audit_existing is not None:
        audit_existing(args)
    resume_path = (
        require_writable_output(args.resume_existing)
        if args.resume_existing is not None
        else None
    )
    output_dir = (
        resume_path.parent if resume_path is not None else require_writable_output(args.output_dir)
    )
    credentials_file = args.credentials_file or (
        private_state_dir() / ".skyvern" / "credentials.toml"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    api_key = load_local_api_key(credentials_file.resolve())
    selected_tasks = [
        task for task in TASKS if not args.task or task.id in set(args.task)
    ]
    if resume_path is not None:
        document = json.loads(resume_path.read_text(encoding="utf-8"))
        experiment_id = str(document.get("experiment_id") or "")
        runs = document.get("runs")
        if not experiment_id or not isinstance(runs, list):
            raise ValueError("Resume input needs an experiment_id and runs array")
        runs = [
            run
            for run in runs
            if not str(run.get("status", "")).startswith("skipped_")
        ]
        task_by_id = {task.id: task for task in TASKS}
        for index, prior in enumerate(runs):
            if (
                prior.get("status") == "error"
                and prior.get("provider_run_id")
                and prior.get("task_id") in task_by_id
            ):
                print(
                    f"[{utc_now()}] re-auditing completed provider evidence "
                    f"for {prior['run_id']}",
                    flush=True,
                )
                runs[index] = recover_existing_run(
                    prior=prior,
                    task=task_by_id[str(prior["task_id"])],
                    api_base=args.api_base.rstrip("/"),
                    api_key=api_key,
                )
                atomic_write_json(
                    resume_path,
                    {
                        "schema_version": document.get("schema_version", "1.0"),
                        "experiment_id": experiment_id,
                        "runs": runs,
                    },
                )
        started_values = [
            str(run["started_at"]) for run in runs if run.get("started_at")
        ]
        started_at = min(started_values) if started_values else utc_now()
    else:
        experiment_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        started_at = utc_now()
        runs = []
    existing_pairs = [
        (str(run["task_id"]), int(run["repetition"])) for run in runs
    ]
    if len(existing_pairs) != len(set(existing_pairs)):
        raise ValueError("Resume input contains duplicate task/repetition pairs")
    results_path = resume_path or (output_dir / "sanitized_runs.json")
    cumulative_cost = sum(
        float(run["reported_cost_usd"])
        for run in runs
        if run.get("reported_cost_usd") is not None
    )
    cost_estimate_unknown = any(
        not str(run.get("status", "")).startswith("skipped_")
        and run.get("reported_cost_usd") is None
        for run in runs
    )
    for task in selected_tasks:
        for repetition in range(1, args.repetitions + 1):
            if (task.id, repetition) in set(existing_pairs):
                continue
            local_run_id = f"{experiment_id}-{task.id}-r{repetition:02d}"
            skip_status = None
            if cost_estimate_unknown:
                skip_status = "skipped_unreported_cost"
            elif cumulative_cost >= args.cost_cap_usd:
                skip_status = "skipped_cost_cap"
            if skip_status is not None:
                runs.append(
                    {
                        "run_id": local_run_id,
                        "provider_run_id": None,
                        "framework": FRAMEWORK,
                        "framework_version": FRAMEWORK_VERSION,
                        "model_configuration": DEFAULT_MODEL,
                        "task_id": task.id,
                        "task": task.public_description,
                        "repetition": repetition,
                        "status": skip_status,
                        "success": False,
                        "reported_cost_usd": None,
                        "error_type": None,
                    }
                )
                existing_pairs.append((task.id, repetition))
                continue
            print(
                f"[{utc_now()}] running {task.id} repetition "
                f"{repetition}/{args.repetitions}",
                flush=True,
            )
            result = run_once(
                task=task,
                repetition=repetition,
                experiment_id=experiment_id,
                api_base=args.api_base.rstrip("/"),
                api_key=api_key,
                max_steps=args.max_steps,
                timeout_seconds=args.timeout_seconds,
                engine=args.engine,
            )
            runs.append(result)
            existing_pairs.append((task.id, repetition))
            if result.get("reported_cost_usd") is not None:
                cumulative_cost += float(result["reported_cost_usd"])
            else:
                cost_estimate_unknown = True
            atomic_write_json(
                results_path,
                {
                    "schema_version": "1.0",
                    "experiment_id": experiment_id,
                    "runs": runs,
                },
            )
            print(
                f"[{utc_now()}] {local_run_id}: {result['status']}; "
                f"cost=${result.get('reported_cost_usd')}",
                flush=True,
            )
    completed_at = utc_now()
    aggregate = build_aggregate(
        experiment_id=experiment_id,
        repetitions=args.repetitions,
        max_steps=args.max_steps,
        cost_cap=args.cost_cap_usd,
        runs=runs,
        started_at=started_at,
        completed_at=completed_at,
        engine=args.engine,
    )
    atomic_write_json(
        results_path,
        {
            "schema_version": "1.0",
            "experiment_id": experiment_id,
            "runs": runs,
        },
    )
    atomic_write_json(output_dir / "aggregate.json", aggregate)
    print(
        f"completed {aggregate['completed_runs']} runs; "
        f"{aggregate['successful_runs']} passed; "
        f"reported cost=${aggregate['reported_cost_usd']:.8f}",
        flush=True,
    )
    all_planned_runs_passed = (
        aggregate["all_planned_runs_completed"]
        and aggregate["run_matrix_complete"]
        and aggregate["provider_run_ids_unique"]
        and aggregate["all_validator_checks_passed"]
        and aggregate["successful_runs"] == aggregate["planned_runs"]
        and aggregate["runs_with_unreported_cost"] == 0
    )
    raise SystemExit(0 if all_planned_runs_passed else 1)


if __name__ == "__main__":
    main()
