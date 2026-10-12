"""Isolated Skyvern 1.0.47 embedded bridge.

This process intentionally contains Skyvern and its newer Playwright release,
but no BrowserGym installation.  The parent orchestrator owns the official
MiniWoB task, validator, Chrome profile, and recorder extension.  This bridge
attaches to that exact CDP target and never substitutes scripted actions.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.metadata
import inspect
import json
import subprocess
import sys
import time
from collections import defaultdict
from typing import Any


BRIDGE_REQUEST_SCHEMA = "cui-week1-skyvern-local-bridge-request-v1"
BRIDGE_RESULT_SCHEMA = "cui-week1-skyvern-local-bridge-result-v1"
EXPECTED_SKYVERN = "1.0.47"
EXPECTED_PLAYWRIGHT = "1.62.0"
EXPECTED_MODEL = "gpt-5.5"
MODEL_TEMPERATURE = 1
MODEL_MAX_COMPLETION_TOKENS = 8192
TARGET_GUARD_INTERVAL_SECONDS = 1.0
EXPECTED_SOURCE_GUARDS = {
    "local": "093713cd50309bd367bd6092a19fda2da5aac3325161f69a6cd4c805caf7003d",
    "connect_to_browser_over_cdp": "257bc03bcaff87b5b732f5b162f040b4c0d070a3daaed722cb665372763cb63f",
    "run_task": "ebeee2e8f94801fa85db203c31188a2e9f38147e4d7eaf09d5569b1c6b2a12f7",
    "aclose": "e1ab27e9e19d0a222cf083bfc9133b137826738e7452018ce6f1b84fe93794e8",
}
EXPECTED_API_PARAMETERS = {
    "local": ("llm_config", "settings", "use_in_memory_db"),
    "connect_to_browser_over_cdp": ("self", "cdp_url"),
    "run_task": (
        "self", "prompt", "engine", "model", "url", "webhook_url", "totp_identifier", "totp_url",
        "title", "error_code_mapping", "data_extraction_schema", "proxy_location", "max_steps",
        "wait_for_completion", "timeout", "browser_session_id", "user_agent", "extra_http_headers",
        "publish_workflow", "include_action_history_in_verification", "max_screenshot_scrolls",
        "browser_address", "request_options",
    ),
    "aclose": ("self",),
}
EXPECTED_API_MODULE = "skyvern.library.skyvern"
TERMINAL_STATUSES = {"completed", "failed", "terminated", "canceled", "cancelled", "timed_out"}


class BridgeError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def _version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, dict):
        return value
    return {"value_type": type(value).__name__}


def _verify_source_guards(skyvern_class: type[Any]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    for name, expected_hash in EXPECTED_SOURCE_GUARDS.items():
        value = getattr(skyvern_class, name)
        source_hash = hashlib.sha256(inspect.getsource(value).encode("utf-8")).hexdigest()
        module = str(getattr(value, "__module__", ""))
        parameters = tuple(inspect.signature(value).parameters)
        passed = (
            source_hash == expected_hash
            and module == EXPECTED_API_MODULE
            and parameters == EXPECTED_API_PARAMETERS[name]
        )
        checks.append(
            {
                "id": f"api-source-{name}",
                "status": "pass" if passed else "fail",
                "detail": (
                    f"module={module} parameters={','.join(parameters)} "
                    f"sha256={source_hash} expected={expected_hash}"
                ),
            }
        )
    return checks


async def _instantiate_and_close() -> None:
    from skyvern import Skyvern

    client = Skyvern.local(use_in_memory_db=True)
    await client.aclose()


def self_test() -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def add(check_id: str, passed: bool, detail: str) -> None:
        checks.append({"id": check_id, "status": "pass" if passed else "fail", "detail": detail})

    python_ok = (3, 11) <= sys.version_info[:2] < (3, 14)
    add(
        "python-version",
        python_ok,
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}; required >=3.11,<3.14",
    )
    add(
        "python-utf8-mode",
        sys.flags.utf8_mode == 1,
        f"utf8_mode={sys.flags.utf8_mode}; bridge launcher must force UTF-8 on Windows",
    )
    for distribution, expected in (("skyvern", EXPECTED_SKYVERN), ("playwright", EXPECTED_PLAYWRIGHT)):
        actual = _version(distribution)
        add(f"package-{distribution}", actual == expected, f"installed={actual or 'missing'} expected={expected}")
    for distribution in ("browsergym-core", "browsergym-miniwob"):
        actual = _version(distribution)
        add(
            f"package-absent-{distribution}",
            actual is None,
            "absent as required" if actual is None else f"unexpectedly installed={actual}",
        )
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        detail = "dependency graph consistent" if completed.returncode == 0 else "pip check reported conflicts"
        add("dependency-consistency", completed.returncode == 0, detail)
    except (OSError, subprocess.TimeoutExpired) as error:
        add("dependency-consistency", False, f"{type(error).__name__}: pip check unavailable")

    try:
        from skyvern import Skyvern
        from skyvern.schemas.llm import LLMConfig

        guards = _verify_source_guards(Skyvern)
        checks.extend(guards)
        local_signature = inspect.signature(Skyvern.local)
        run_signature = inspect.signature(Skyvern.run_task)
        api_ok = (
            "use_in_memory_db" in local_signature.parameters
            and "browser_address" in run_signature.parameters
            and "max_steps" in run_signature.parameters
            and "wait_for_completion" in run_signature.parameters
            and LLMConfig is not None
        )
        add("embedded-api", api_ok, "Skyvern.local, LLMConfig, external CDP, and run limits available")
        callback_parameters = {
            "on_step_start", "before_action", "pre_action_hook", "prepare_step", "callbacks"
        }
        no_public_pre_action_hook = callback_parameters.isdisjoint(run_signature.parameters)
        add(
            "step-alignment-capability",
            no_public_pre_action_hook,
            "guarded Skyvern.run_task exposes no reliable public pre-action callback; step-level analysis is unsupported",
        )
    except Exception as error:
        add("embedded-api", False, f"{type(error).__name__}: embedded API import/guard failed")

    if all(row["status"] == "pass" for row in checks):
        try:
            # The first embedded construction imports Skyvern's local stack
            # and takes roughly 45 seconds on the pinned Windows environment.
            # This remains a no-model lifecycle check, but its deadline must
            # cover that deterministic cold start rather than fail spuriously.
            asyncio.run(asyncio.wait_for(_instantiate_and_close(), timeout=90))
            add("embedded-lifecycle", True, "Skyvern.local(use_in_memory_db=True) instantiated and closed")
        except Exception as error:
            add("embedded-lifecycle", False, f"{type(error).__name__}: embedded lifecycle failed")
    else:
        add("embedded-lifecycle", False, "not attempted because an isolation/API guard failed")

    return {
        "schema_version": "cui-week1-skyvern-local-self-test-v1",
        "ready": all(row["status"] == "pass" for row in checks),
        "mode": "embedded_in_memory",
        "checks": checks,
        "secrets_printed": False,
        "repository_writes_performed": False,
        # The launcher forces cwd/TMP below CUI_WEEK1_PRIVATE_ROOT.  Skyvern
        # may create local logs/cache while importing or closing; those writes
        # are private state, never a claim that the probe is globally no-write.
        "private_state_may_change": True,
        "writes_performed": True,
    }


def _validate_target(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise BridgeError("official_target_missing", "official_target must be an object")
    target_id = value.get("target_id")
    url = value.get("url")
    if not isinstance(target_id, str) or not target_id or len(target_id) > 128:
        raise BridgeError("official_target_invalid", "official target ID is missing or malformed")
    if not isinstance(url, str) or not url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise BridgeError("official_target_invalid", "official target must be a loopback MiniWoB URL")
    return {"target_id": target_id, "url": url}


def validate_request(value: Any, operation: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != BRIDGE_REQUEST_SCHEMA:
        raise BridgeError("invalid_request", "bridge request schema mismatch")
    if value.get("operation") != operation:
        raise BridgeError("invalid_request", f"bridge operation must be {operation}")
    target = _validate_target(value.get("official_target"))
    cdp_url = value.get("cdp_url")
    if not isinstance(cdp_url, str) or not cdp_url.startswith(("ws://127.0.0.1:", "http://127.0.0.1:")):
        raise BridgeError("invalid_request", "CDP endpoint must be loopback")
    if operation == "run":
        if value.get("model") != EXPECTED_MODEL:
            raise BridgeError("model_mismatch", f"Skyvern local bridge requires {EXPECTED_MODEL}")
        if value.get("max_actions") != 30 or value.get("timeout_seconds") != 480:
            raise BridgeError("limit_mismatch", "Skyvern local bridge requires 30 actions and 480 seconds")
        if value.get("seed") not in {20260808, 20260809, 20260810}:
            raise BridgeError("seed_mismatch", "Skyvern task seed is outside the pinned repetition set")
        if not isinstance(value.get("instruction"), str) or not value["instruction"].strip():
            raise BridgeError("invalid_request", "task instruction is missing")
        if value.get("step_alignment") != {
            "supported": False,
            "support_status": "not_supported",
            "hook_type": "none",
            "max_steps": 30,
        }:
            raise BridgeError(
                "step_alignment_contract_invalid",
                "Skyvern must declare its missing public pre-action hook",
            )
    value["official_target"] = target
    return value


async def _target_snapshot(browser: Any) -> list[dict[str, str]]:
    pages = list(browser.pages)
    snapshots: list[dict[str, str]] = []
    for page in pages:
        session = await browser.new_cdp_session(page)
        try:
            response = await session.send("Target.getTargetInfo")
        finally:
            await session.detach()
        info = response.get("targetInfo") or {}
        snapshots.append({"target_id": str(info.get("targetId") or ""), "url": str(page.url)})
    return snapshots


async def _assert_official_target(browser: Any, expected: dict[str, str]) -> Any:
    pages = await _target_snapshot(browser)
    if len(pages) != 1:
        raise BridgeError("official_target_count_changed", f"expected one page target, observed {len(pages)}")
    observed = pages[0]
    if observed["target_id"] != expected["target_id"] or observed["url"] != expected["url"]:
        raise BridgeError("official_target_changed", "agent focus/page drifted away from the official task target")
    return list(browser.pages)[0]


async def _target_guard(browser: Any, official_page: Any, expected: dict[str, str], stop: asyncio.Event) -> None:
    while not stop.is_set():
        pages = list(browser.pages)
        if len(pages) != 1:
            raise BridgeError("official_target_count_changed", f"expected one page target, observed {len(pages)}")
        if pages[0] is not official_page or str(pages[0].url) != expected["url"]:
            raise BridgeError("official_target_changed", "agent page drifted away from the official task target")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TARGET_GUARD_INTERVAL_SECONDS)
        except TimeoutError:
            continue


async def _await_or_target_guard(
    awaitable: Any,
    guard_task: asyncio.Task[None],
    *,
    timeout: float,
) -> Any:
    """Race a framework operation against identity drift and one absolute deadline."""

    if timeout <= 0:
        raise TimeoutError
    operation = asyncio.create_task(awaitable)
    try:
        done, _pending = await asyncio.wait(
            {operation, guard_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if guard_task in done:
            operation.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await operation
            await guard_task
            raise BridgeError("official_target_guard_stopped", "official target guard stopped unexpectedly")
        if operation not in done:
            operation.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await operation
            raise TimeoutError
        return await operation
    except BaseException:
        if not operation.done():
            operation.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await operation
        raise


async def probe_cdp(request: dict[str, Any]) -> dict[str, Any]:
    from skyvern import Skyvern

    guards = _verify_source_guards(Skyvern)
    if any(row["status"] != "pass" for row in guards):
        raise BridgeError("skyvern_source_guard_failed", "pinned Skyvern API source changed")

    client = Skyvern.local(use_in_memory_db=True)
    connected = False
    try:
        browser = await asyncio.wait_for(client.connect_to_browser_over_cdp(request["cdp_url"]), timeout=30)
        connected = True
        await _assert_official_target(browser, request["official_target"])
    finally:
        await asyncio.wait_for(client.aclose(), timeout=20)
    return {
        "schema_version": BRIDGE_RESULT_SCHEMA,
        "operation": "probe_cdp",
        "ok": True,
        "embedded": True,
        "external_cdp_connected": connected,
        "official_target_verified": True,
        "client_closed": True,
    }


async def run_task(request: dict[str, Any]) -> dict[str, Any]:
    from skyvern import Skyvern
    from skyvern.schemas.llm import LLMConfig

    guards = _verify_source_guards(Skyvern)
    if any(row["status"] != "pass" for row in guards):
        raise BridgeError("skyvern_source_guard_failed", "pinned Skyvern API source changed")

    llm_config = LLMConfig(
        model_name=EXPECTED_MODEL,
        required_env_vars=["OPENAI_API_KEY"],
        supports_vision=True,
        add_assistant_prefix=False,
        max_completion_tokens=MODEL_MAX_COMPLETION_TOKENS,
        # Skyvern's pinned OPENAI_GPT5_5 config explicitly documents that the
        # GPT-5 family accepts temperature=1 only.  The adapter reports this
        # protocol-visible exception instead of silently claiming temperature=0.
        temperature=MODEL_TEMPERATURE,
    )
    client = Skyvern.local(llm_config=llm_config, use_in_memory_db=True)
    guard_stop = asyncio.Event()
    guard_task: asyncio.Task[None] | None = None
    task_run: Any = None
    artifacts: list[Any] = []
    timed_out = False
    try:
        browser = await asyncio.wait_for(client.connect_to_browser_over_cdp(request["cdp_url"]), timeout=30)
        official_page = await _assert_official_target(browser, request["official_target"])
        guard_task = asyncio.create_task(
            _target_guard(browser, official_page, request["official_target"], guard_stop)
        )
        # One monotonic deadline covers the initial embedded-ASGI request and
        # every subsequent status poll. Skyvern.local may not return from the
        # initial request until its BackgroundTask is done, so resetting the
        # clock after run_task would silently allow almost two eight-minute
        # windows.
        deadline = time.monotonic() + request["timeout_seconds"]
        try:
            task_run = await _await_or_target_guard(
                client.run_task(
                prompt=(
                    "Complete the MiniWoB task already open in the attached browser: "
                    f"{request['instruction']} Do not navigate away or open another tab. "
                    "Stop immediately when the task reports completion."
                ),
                # Supplying the already-open exact URL prevents Skyvern from
                # spending a separate LLM call inferring a start URL.  Its
                # pinned agent skips navigation when current_url already
                # equals task.url, preserving the BrowserGym episode state.
                url=request["official_target"]["url"],
                max_steps=request["max_actions"],
                wait_for_completion=False,
                browser_address=request["cdp_url"],
                include_action_history_in_verification=True,
                max_screenshot_scrolls=0,
                ),
                guard_task,
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except TimeoutError:
            timed_out = True
        while task_run is not None and str(_dump(task_run).get("status") or "").lower() not in TERMINAL_STATUSES:
            if guard_task.done():
                await guard_task
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            await asyncio.sleep(min(1.0, remaining))
            if guard_task.done():
                await guard_task
            try:
                task_run = await _await_or_target_guard(
                    client.get_run(task_run.run_id),
                    guard_task,
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            except TimeoutError:
                timed_out = True
                break
        if timed_out and task_run is not None and getattr(task_run, "run_id", None):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.cancel_run(task_run.run_id), timeout=5)
        if guard_task.done():
            await guard_task
        await _assert_official_target(browser, request["official_target"])
        if task_run is not None and getattr(task_run, "run_id", None):
            artifacts = await asyncio.wait_for(client.get_run_artifacts(task_run.run_id), timeout=10)
    finally:
        guard_stop.set()
        if guard_task is not None:
            guard_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await guard_task
        await asyncio.wait_for(client.aclose(), timeout=30)

    run_data = _dump(task_run)
    grouped: dict[str, set[str]] = defaultdict(set)
    artifact_rows: list[dict[str, Any]] = []
    for artifact in artifacts:
        value = _dump(artifact)
        artifact_type = str(value.get("artifact_type") or "unknown")
        step_id = str(value.get("step_id") or "")
        if step_id:
            grouped[step_id].add(artifact_type)
        artifact_rows.append(
            {
                "artifact_id": value.get("artifact_id"),
                "artifact_type": artifact_type,
                "step_id": value.get("step_id"),
                "checksum": value.get("checksum"),
                "file_size": value.get("file_size"),
            }
        )
    action_history = [
        {"step_id": step_id, "type": "skyvern_step", "artifact_types": sorted(types)}
        for step_id, types in sorted(grouped.items())
    ]
    step_count = run_data.get("step_count")
    if not isinstance(step_count, int):
        step_count = len(action_history)
    if step_count < 0 or step_count > request["max_actions"]:
        raise BridgeError("action_count_invalid", "Skyvern returned an invalid action count")
    status = str(run_data.get("status") or "unknown").lower()
    return {
        "schema_version": BRIDGE_RESULT_SCHEMA,
        "operation": "run",
        "ok": True,
        "action_history": action_history,
        "action_count": step_count,
        "completed": status in {"completed", "success", "succeeded"},
        "timed_out": timed_out or status == "timed_out",
        "artifacts": {
            "skyvern_run_id": run_data.get("run_id"),
            "skyvern_status": run_data.get("status"),
            "skyvern_artifacts": artifact_rows,
            "embedded_in_memory": True,
            "external_cdp": True,
            "official_target_locked": True,
            "target_guard_interval_seconds": TARGET_GUARD_INTERVAL_SECONDS,
            "target_guard_input_events": 0,
            "model_temperature": MODEL_TEMPERATURE,
            "model_max_completion_tokens": MODEL_MAX_COMPLETION_TOKENS,
            "task_seed": request.get("seed"),
            "model_seed_applied": False,
            "model_seed_support": "not_exposed_by_skyvern_1.0.47_llm_config",
            "step_alignment_support_status": "not_supported",
            "step_hook_contract_verified": True,
            "framework_step_hook_calls": 0,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    protocol_stdout = sys.stdout
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if args.self_test:
                result = self_test()
            else:
                try:
                    request = json.loads(sys.stdin.read())
                except json.JSONDecodeError as error:
                    raise BridgeError("invalid_json", "could not parse bridge request JSON") from error
                operation = request.get("operation") if isinstance(request, dict) else None
                if operation not in {"run", "probe_cdp"}:
                    raise BridgeError("invalid_request", "unsupported bridge operation")
                request = validate_request(request, operation)
                result = asyncio.run(run_task(request) if operation == "run" else probe_cdp(request))
    except BridgeError as error:
        print(f"{error.code}: {type(error).__name__}", file=sys.stderr)
        result = {"schema_version": BRIDGE_RESULT_SCHEMA, "ok": False, "error": {"code": error.code}}
    except Exception as error:
        print(f"skyvern_bridge_exception: {type(error).__name__}: {error}", file=sys.stderr)
        result = {
            "schema_version": BRIDGE_RESULT_SCHEMA,
            "ok": False,
            "error": {"code": "skyvern_bridge_exception", "type": type(error).__name__},
        }
    protocol_stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    if args.self_test:
        return 0 if result.get("ready") is True else 2
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
