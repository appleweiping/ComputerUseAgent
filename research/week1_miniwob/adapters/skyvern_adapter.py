"""Skyvern 1.0.47 embedded bridge for one official MiniWoB attempt.

Launched by ``dispatch.py`` as ``python -m research.week1_miniwob.adapters.skyvern_adapter``
from the repository root inside the isolated ``orchestrator`` environment.  The
standalone ``skyvern_local_bridge.py`` script runs in the ``skyvern-local`` environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .common import FrameworkRun, adapter_cli
from .dispatch import environment_python, environment_root
from .skyvern_local_bridge import BRIDGE_REQUEST_SCHEMA


BRIDGE = Path(__file__).with_name("skyvern_local_bridge.py")
REPO_ROOT = Path(__file__).resolve().parents[3]


def _skyvern_python() -> Path | None:
    root = environment_root()
    if root is None:
        return None
    return environment_python(root, "skyvern-local")


def _bridge_env(*, private_state: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    # Skyvern 1.0.47 opens its packaged domUtils.js without an explicit
    # encoding.  UTF-8 mode is therefore a fail-closed launcher invariant on
    # Windows; PYTHONIOENCODING also keeps framework logs off protocol stdout.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["OTEL_ENABLED"] = "false"
    env["ENABLE_CLEANUP_CRON"] = "false"
    if private_state is not None:
        temp_root = private_state / "tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        env["TMP"] = str(temp_root)
        env["TEMP"] = str(temp_root)
        env["TMPDIR"] = str(temp_root)
    return env


def _missing_bridge_self_test(detail: str) -> dict[str, Any]:
    return {
        "schema_version": "cui-week1-skyvern-local-self-test-v1",
        "ready": False,
        "mode": "embedded_in_memory",
        "checks": [{"id": "skyvern-local-environment", "status": "fail", "detail": detail}],
        "secrets_printed": False,
        "repository_writes_performed": False,
        "private_state_may_change": False,
        "writes_performed": False,
    }


def _repository_state() -> tuple[tuple[str, int, int], ...]:
    """Cheap write detector around a private-cwd framework self-test."""

    excluded = {".git", "node_modules", "__pycache__", "results", "deliverables", "artifacts"}
    rows: list[tuple[str, int, int]] = []
    for root, directories, files in os.walk(REPO_ROOT):
        directories[:] = [name for name in directories if name not in excluded]
        root_path = Path(root)
        for name in files:
            path = root_path / name
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append((path.relative_to(REPO_ROOT).as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(rows))


def bridge_self_test() -> dict[str, Any]:
    python = _skyvern_python()
    if python is None or not python.is_file():
        return _missing_bridge_self_test("isolated skyvern-local interpreter is missing")
    private_value = os.environ.get("CUI_WEEK1_PRIVATE_ROOT")
    if not private_value:
        return _missing_bridge_self_test("CUI_WEEK1_PRIVATE_ROOT is missing")
    private_state = (
        Path(private_value).expanduser().resolve() / "framework_state" / "skyvern-local" / "self-test"
    )
    private_state.mkdir(parents=True, exist_ok=True)
    before = _repository_state()
    try:
        completed = subprocess.run(
            [str(python), "-X", "utf8", str(BRIDGE), "--self-test"],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
            cwd=private_state,
            env=_bridge_env(private_state=private_state),
        )
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        return _missing_bridge_self_test(f"{type(error).__name__}: Skyvern local self-test failed")
    if completed.returncode != 0 and payload.get("ready") is not False:
        return _missing_bridge_self_test("Skyvern local self-test exited nonzero without a failure payload")
    repository_changed = before != _repository_state()
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    checks.append(
        {
            "id": "repository-unchanged",
            "status": "fail" if repository_changed else "pass",
            "detail": "repository files changed during self-test" if repository_changed else "no repository file changed",
        }
    )
    payload["checks"] = checks
    payload["ready"] = payload.get("ready") is True and not repository_changed
    payload["repository_writes_performed"] = repository_changed
    payload["private_state_may_change"] = True
    payload["writes_performed"] = True
    return payload


def _bridge_request(
    operation: str,
    attempt: dict[str, Any],
    instruction: str,
    cdp_url: str,
) -> dict[str, Any]:
    runtime = attempt.get("_runtime")
    official_target = runtime.get("official_target") if isinstance(runtime, dict) else None
    step_alignment = runtime.get("step_alignment") if isinstance(runtime, dict) else None
    if operation == "run" and step_alignment != {
        "supported": False,
        "support_status": "not_supported",
        "hook_type": "none",
        "max_steps": attempt.get("limits", {}).get("max_actions"),
    }:
        raise RuntimeError("Skyvern 1.0.47 step-alignment capability declaration changed")
    return {
        "schema_version": BRIDGE_REQUEST_SCHEMA,
        "operation": operation,
        "model": attempt.get("model"),
        "seed": attempt.get("seed"),
        "instruction": instruction,
        "cdp_url": cdp_url,
        "official_target": official_target,
        "max_actions": attempt.get("limits", {}).get("max_actions"),
        "timeout_seconds": attempt.get("limits", {}).get("timeout_seconds"),
        **({"step_alignment": step_alignment} if operation == "run" else {}),
    }


def _invoke_bridge(payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    python = _skyvern_python()
    if python is None or not python.is_file():
        raise RuntimeError("isolated skyvern-local interpreter is missing")
    private_value = os.environ.get("CUI_WEEK1_PRIVATE_ROOT")
    if not private_value:
        raise RuntimeError("CUI_WEEK1_PRIVATE_ROOT is missing")
    private_state = Path(private_value).expanduser().resolve() / "framework_state" / "skyvern-local"
    private_state.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [str(python), "-X", "utf8", str(BRIDGE)],
            input=json.dumps(payload, ensure_ascii=True),
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            cwd=private_state,
            env=_bridge_env(private_state=private_state),
        )
    except subprocess.TimeoutExpired as error:
        retained = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else error.stderr
        if retained:
            sys.stderr.write(retained)
        return {
            "schema_version": "cui-week1-skyvern-local-bridge-result-v1",
            "ok": False,
            "timed_out": True,
            "error": {"code": "skyvern_process_timeout"},
        }
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Skyvern local bridge did not return one valid protocol JSON object") from error
    if completed.returncode != 0 or result.get("ok") is not True:
        code = result.get("error", {}).get("code") if isinstance(result.get("error"), dict) else None
        raise RuntimeError(f"Skyvern local bridge failed: {code or 'unknown_bridge_error'}")
    return result


def run_probe(attempt: dict[str, Any], cdp_url: str) -> dict[str, Any]:
    """Attach/detach embedded Skyvern without starting an agent or model call."""

    payload = _bridge_request("probe_cdp", attempt, "", cdp_url)
    return _invoke_bridge(payload, timeout=75)


def run(attempt: dict[str, Any], instruction: str, cdp_url: str) -> FrameworkRun:
    payload = _bridge_request("run", attempt, instruction, cdp_url)
    # Skyvern.local uses an in-process ASGI transport whose initial run_task
    # request can remain open until background execution completes.  The child
    # process deadline is therefore the authoritative 8-minute watchdog.
    # The bridge starts its strict 480-second agent deadline only after local
    # construction and CDP attachment. Keep a separate startup/cleanup budget
    # so the outer watchdog cannot shorten or extend that agent deadline.
    result = _invoke_bridge(payload, timeout=attempt["limits"]["timeout_seconds"] + 120)
    if result.get("timed_out") is True:
        return FrameworkRun(
            action_history=[],
            action_count=0,
            completed=False,
            timed_out=True,
            artifacts={
                "embedded_in_memory": True,
                "process_watchdog_triggered": True,
                "framework_step_hook_calls": 0,
                "step_hook_contract_verified": True,
                "step_alignment_support_status": "not_supported",
            },
        )
    action_history = result.get("action_history")
    action_count = result.get("action_count")
    if not isinstance(action_history, list) or not isinstance(action_count, int):
        raise RuntimeError("Skyvern local bridge omitted structured action evidence")
    artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
    artifacts.update(
        {
            "framework_step_hook_calls": 0,
            "step_hook_contract_verified": True,
            "step_alignment_support_status": "not_supported",
        }
    )
    return FrameworkRun(
        action_history=action_history,
        action_count=action_count,
        completed=result.get("completed") is True,
        timed_out=result.get("timed_out") is True,
        artifacts=artifacts,
    )


if __name__ == "__main__":
    raise SystemExit(adapter_cli("skyvern", run))
