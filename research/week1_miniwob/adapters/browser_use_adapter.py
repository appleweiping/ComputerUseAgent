"""Browser-Use 0.11.5 bridge for one official MiniWoB attempt.

Launched by ``dispatch.py`` as ``python -m research.week1_miniwob.adapters.browser_use_adapter``
from the repository root inside the isolated ``browser-use`` environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import os
import sys
from pathlib import Path
from typing import Any

from .browser_use_compat import browser_use_external_cdp_compatibility
from .common import (
    FrameworkRun,
    adapter_cli,
    configure_browser_use_private_runtime,
    post_step_alignment,
    run_coroutine_in_worker,
    validate_step_alignment_descriptor,
)


BROWSER_USE_STEP_HOOK_GUARDS = {
    "run": {
        "signature": "(self, max_steps: int = 100, on_step_start: collections.abc.Callable[['Agent'], collections.abc.Awaitable[None]] | None = None, on_step_end: collections.abc.Callable[['Agent'], collections.abc.Awaitable[None]] | None = None) -> browser_use.agent.views.AgentHistoryList",
        "sha256": "c745cd2a4984c6b6231cecd23fb24a99134e121d739e6237db15d97462514d47",
    },
    "_execute_step": {
        "signature": "(self, step: int, max_steps: int, step_info: browser_use.agent.views.AgentStepInfo, on_step_start: collections.abc.Callable[['Agent'], collections.abc.Awaitable[None]] | None = None, on_step_end: collections.abc.Callable[['Agent'], collections.abc.Awaitable[None]] | None = None) -> bool",
        "sha256": "d6414c90c203b06c9de327407c17ff9ef6037244b6bd5660a6823dbda059ae94",
    },
}


def _verify_step_hook_contract(agent_class: type[Any]) -> dict[str, Any]:
    source_hashes: dict[str, str] = {}
    for name, expected in BROWSER_USE_STEP_HOOK_GUARDS.items():
        value = getattr(agent_class, name)
        implementation = inspect.unwrap(value)
        source_file = inspect.getsourcefile(implementation)
        signature = str(inspect.signature(value))
        source_hash = hashlib.sha256(inspect.getsource(implementation).encode("utf-8")).hexdigest()
        if (
            not source_file
            or not Path(source_file).as_posix().lower().endswith("browser_use/agent/service.py")
            or signature != expected["signature"]
            or source_hash != expected["sha256"]
        ):
            raise RuntimeError(f"Browser-Use 0.11.5 {name} pre-action hook contract drifted")
        source_hashes[name] = source_hash
    execute_source = inspect.getsource(inspect.unwrap(agent_class._execute_step))
    if execute_source.index("await on_step_start(self)") > execute_source.index("await asyncio.wait_for("):
        raise RuntimeError("Browser-Use on_step_start no longer precedes step execution")
    return {
        "verified": True,
        "hook_type": "browser_use_on_step_start",
        "source_hash_count": len(source_hashes),
    }


class _BrowserUseStepHook:
    def __init__(self, attempt: dict[str, Any]):
        runtime = attempt.get("_runtime")
        descriptor = runtime.get("step_alignment") if isinstance(runtime, dict) else None
        self.descriptor = validate_step_alignment_descriptor(descriptor, "browser-use")
        self.calls = 0

    async def __call__(self, _agent: Any) -> None:
        next_step = self.calls + 1
        await asyncio.to_thread(
            post_step_alignment,
            self.descriptor,
            "browser-use",
            next_step,
        )
        self.calls = next_step


def _official_target(attempt: dict[str, Any]) -> dict[str, str]:
    runtime = attempt.get("_runtime")
    target = runtime.get("official_target") if isinstance(runtime, dict) else None
    if not isinstance(target, dict):
        raise RuntimeError("official BrowserGym target identity is missing")
    target_id = target.get("target_id")
    url = target.get("url")
    if not isinstance(target_id, str) or not target_id or not isinstance(url, str) or not url:
        raise RuntimeError("official BrowserGym target identity is malformed")
    return {"target_id": target_id, "url": url}


async def _assert_official_target(browser_session: Any, expected: dict[str, str]) -> None:
    tabs = await browser_session.get_tabs()
    if len(tabs) != 1:
        raise RuntimeError(f"official target guard expected one page, observed {len(tabs)}")
    tab = tabs[0]
    if str(tab.target_id) != expected["target_id"] or str(tab.url) != expected["url"]:
        raise RuntimeError("Browser-Use page target drifted away from the official BrowserGym episode")
    current = await browser_session.get_current_target_info()
    if not isinstance(current, dict):
        raise RuntimeError("Browser-Use has no focused page target")
    if str(current.get("targetId") or "") != expected["target_id"] or str(current.get("url") or "") != expected["url"]:
        raise RuntimeError("Browser-Use focus drifted away from the official BrowserGym episode")


async def _target_guard(browser_session: Any, expected: dict[str, str], stop: asyncio.Event) -> None:
    while not stop.is_set():
        await _assert_official_target(browser_session, expected)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except TimeoutError:
            continue


async def _run_with_target_guard(
    agent: Any,
    browser_session: Any,
    expected: dict[str, str],
    *,
    max_steps: int,
    timeout: float,
    on_step_start: Any,
) -> Any:
    stop = asyncio.Event()
    agent_task = asyncio.create_task(
        agent.run(max_steps=max_steps, on_step_start=on_step_start)
    )
    guard_task = asyncio.create_task(_target_guard(browser_session, expected, stop))
    try:
        done, _pending = await asyncio.wait(
            {agent_task, guard_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await agent_task
            raise TimeoutError
        if guard_task in done:
            agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await agent_task
            await guard_task
            raise RuntimeError("official target guard stopped unexpectedly")
        history = await agent_task
        await _assert_official_target(browser_session, expected)
        return history
    finally:
        stop.set()
        guard_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await guard_task


async def _run(attempt: dict[str, Any], instruction: str, cdp_url: str) -> FrameworkRun:
    configure_browser_use_private_runtime()
    from browser_use import Agent, BrowserSession, ChatOpenAI

    expected_target = _official_target(attempt)
    step_hook_contract = _verify_step_hook_contract(Agent)
    step_hook = _BrowserUseStepHook(attempt)
    with browser_use_external_cdp_compatibility(expected_target) as compatibility:
        browser_session = BrowserSession(
            cdp_url=cdp_url,
            is_local=False,
            keep_alive=True,
            enable_default_extensions=False,
        )
        llm = ChatOpenAI(
            model=attempt["model"],
            api_key=os.environ["OPENAI_API_KEY"],
            seed=attempt["seed"],
            temperature=0,
            timeout=120,
            max_retries=2,
        )
        agent = Agent(
            task=(
                f"Complete the current MiniWoB task exactly as instructed: {instruction} "
                "Stay on the already-open task page and finish as soon as the task is complete."
            ),
            llm=llm,
            browser_session=browser_session,
            max_actions_per_step=1,
            use_judge=False,
            generate_gif=False,
            llm_timeout=120,
            step_timeout=90,
        )
        timeout = attempt["limits"]["timeout_seconds"] - 10
        outcome: FrameworkRun | None = None
        stop_error: Exception | None = None
        try:
            await browser_session.start()
            await browser_session.get_or_create_cdp_session(expected_target["target_id"], focus=True)
            await _assert_official_target(browser_session, expected_target)
            try:
                history = await _run_with_target_guard(
                    agent,
                    browser_session,
                    expected_target,
                    max_steps=attempt["limits"]["max_actions"],
                    timeout=timeout,
                    on_step_start=step_hook,
                )
            except TimeoutError:
                partial_history = getattr(agent, "history", None)
                partial_actions = partial_history.model_actions() if partial_history is not None else []
                outcome = FrameworkRun(
                    action_history=partial_actions,
                    action_count=len(partial_actions),
                    completed=False,
                    timed_out=True,
                    error=f"Browser-Use exceeded the {timeout}s driver deadline",
                    artifacts={
                        "external_cdp_compatibility": True,
                        "skipped_unconditional_resumes": compatibility.skipped_unconditional_resumes,
                        "bounded_resume_timeouts": compatibility.bounded_resume_timeouts,
                        "screenshot_window_checks": compatibility.screenshot_window_checks,
                        "window_normalizations": compatibility.window_normalizations,
                        "bring_to_front_calls": compatibility.bring_to_front_calls,
                        "official_target_locked": True,
                        "target_guard_interval_seconds": 1,
                        "target_guard_input_events": 0,
                        "model_temperature": 0,
                        "model_seed_applied": True,
                        "model_seed": attempt["seed"],
                        "framework_step_hook_calls": step_hook.calls,
                        "step_hook_contract_verified": step_hook_contract["verified"],
                        "step_hook_source_hash_count": step_hook_contract["source_hash_count"],
                    },
                )
            else:
                raw_actions = history.model_actions()
                framework_step_count = history.number_of_steps()
                framework_errors = history.errors()
                framework_error_count = sum(error is not None for error in framework_errors)
                framework_completed = history.is_done()
                outcome = FrameworkRun(
                    action_history=raw_actions,
                    action_count=len(raw_actions),
                    completed=framework_completed,
                    # Browser-Use returns a normal history object even when
                    # every model/provider step failed.  Preserve that as a
                    # framework failure without copying provider messages or
                    # response bodies into public evidence.
                    error=(
                        "Browser-Use ended with failed framework steps before completion"
                        if framework_error_count > 0 and not framework_completed
                        else None
                    ),
                    artifacts={
                        "framework_step_count": framework_step_count,
                        "framework_error_count": framework_error_count,
                        "framework_self_reported_success": history.is_successful(),
                        "external_cdp_compatibility": True,
                        "skipped_unconditional_resumes": compatibility.skipped_unconditional_resumes,
                        "bounded_resume_timeouts": compatibility.bounded_resume_timeouts,
                        "screenshot_window_checks": compatibility.screenshot_window_checks,
                        "window_normalizations": compatibility.window_normalizations,
                        "bring_to_front_calls": compatibility.bring_to_front_calls,
                        "official_target_locked": True,
                        "target_guard_interval_seconds": 1,
                        "target_guard_input_events": 0,
                        "model_temperature": 0,
                        "model_seed_applied": True,
                        "model_seed": attempt["seed"],
                        "framework_step_hook_calls": step_hook.calls,
                        "step_hook_contract_verified": step_hook_contract["verified"],
                        "step_hook_source_hash_count": step_hook_contract["source_hash_count"],
                    },
                )
        finally:
            # Agent.close() intentionally leaves keep_alive sessions connected.
            # Disconnect this framework CDP client while the compatibility
            # layer is still active; BrowserHost continues to own Chrome.
            try:
                await asyncio.wait_for(browser_session.stop(), timeout=15)
            except Exception as error:
                stop_error = error

        assert outcome is not None
        if stop_error is not None:
            outcome.error = f"Browser-Use BrowserSession stop failed: {type(stop_error).__name__}"
            outcome.artifacts["browser_session_stop_failed"] = True
        else:
            outcome.artifacts["browser_session_stopped"] = True
        return outcome


async def _probe_browser_session(browser_session: Any, expected_target: dict[str, str]) -> dict[str, Any]:
    """Exercise the guarded external-CDP lifecycle without creating an agent."""

    stopped = False
    try:
        await browser_session.start()
        await browser_session.get_or_create_cdp_session(expected_target["target_id"], focus=True)
        await _assert_official_target(browser_session, expected_target)
        tabs = await browser_session.get_tabs()
        current = await browser_session.get_current_target_info()
        return {
            "started": True,
            "focused": True,
            "page_count": len(tabs),
            "target_id": str(current.get("targetId") or "") if isinstance(current, dict) else "",
            "url": str(current.get("url") or "") if isinstance(current, dict) else "",
        }
    finally:
        await asyncio.wait_for(browser_session.stop(), timeout=15)
        stopped = True
        # Kept as an assertion rather than a return-field assignment in the
        # try block so stop failure always makes the probe fail closed.
        if not stopped:  # pragma: no cover - defensive invariant
            raise RuntimeError("Browser-Use probe did not stop its CDP session")


def run_probe(attempt: dict[str, Any], cdp_url: str) -> dict[str, Any]:
    """No-model start/focus/identity/stop probe on the official task target."""

    configure_browser_use_private_runtime()
    expected_target = _official_target(attempt)

    async def probe() -> dict[str, Any]:
        from browser_use import BrowserSession

        with browser_use_external_cdp_compatibility(expected_target) as compatibility:
            browser_session = BrowserSession(
                cdp_url=cdp_url,
                is_local=False,
                keep_alive=True,
                enable_default_extensions=False,
            )
            result = await _probe_browser_session(browser_session, expected_target)
            result.update(
                {
                    "schema_version": "cui-week1-browser-use-cdp-probe-v1",
                    "ok": True,
                    "browser_session_stopped": True,
                    "skipped_unconditional_resumes": compatibility.skipped_unconditional_resumes,
                    "bounded_resume_timeouts": compatibility.bounded_resume_timeouts,
                    "screenshot_window_checks": compatibility.screenshot_window_checks,
                    "window_normalizations": compatibility.window_normalizations,
                    "bring_to_front_calls": compatibility.bring_to_front_calls,
                }
            )
            return result

    with contextlib.redirect_stdout(sys.stderr):
        return run_coroutine_in_worker(probe)


def run(attempt: dict[str, Any], instruction: str, cdp_url: str) -> FrameworkRun:
    # Browser-Use configures its console handler on stdout at import time and
    # some transitive packages use print().  The adapter protocol reserves
    # stdout for exactly one result JSON object, so all framework output is
    # redirected to stderr; the orchestrator retains that stream privately.
    configure_browser_use_private_runtime()
    with contextlib.redirect_stdout(sys.stderr):
        return run_coroutine_in_worker(lambda: _run(attempt, instruction, cdp_url))


if __name__ == "__main__":
    raise SystemExit(adapter_cli("browser-use", run))
