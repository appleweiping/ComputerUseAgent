"""Private log retention and killable adapter-process lifecycle support."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .protocol import ProtocolError


def retain_private_stderr(
    attempt_id: str,
    stderr_text: str,
    *,
    repo_root: Path,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    private_value = os.environ.get("CUI_WEEK1_PRIVATE_ROOT")
    if not private_value:
        raise ProtocolError("CUI_WEEK1_PRIVATE_ROOT is required for private framework logs")
    private_root = Path(private_value).expanduser().resolve()
    try:
        private_root.relative_to(repo_root.resolve())
    except ValueError:
        pass
    else:
        raise ProtocolError("CUI_WEEK1_PRIVATE_ROOT must be outside the Git repository")
    log_root = private_root / "week1" / "orchestrator_logs"
    log_root.mkdir(parents=True, exist_ok=True)
    namespace = (
        hashlib.sha256(str(output_dir.resolve()).encode("utf-8")).hexdigest()[:12]
        if output_dir is not None
        else "legacy"
    )
    destination = log_root / f"{namespace}__{attempt_id}.stderr.log"
    payload = stderr_text.encode("utf-8", errors="replace")
    try:
        with destination.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as error:
        raise ProtocolError("private stderr artifact already exists for this attempt") from error
    sha256 = hashlib.sha256(payload).hexdigest()
    return {
        "artifact_id": f"private-stderr-{sha256[:16]}",
        "retained_private": True,
        "bytes": len(payload),
        "sha256": sha256,
    }


def _text_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return ""


def terminate_process_tree(
    process: subprocess.Popen[str],
    *,
    platform_name: str | None = None,
    shutdown_seconds: int,
) -> str:
    """Terminate only the process group/tree created for one adapter attempt."""

    if process.poll() is not None:
        return "adapter process had already exited"
    platform_name = platform_name or os.name
    if platform_name == "nt":
        detail = "Windows taskkill tree termination requested"
        try:
            terminated = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=shutdown_seconds,
                check=False,
            )
            detail = f"Windows taskkill /T /F exited {terminated.returncode} for owned PID {process.pid}"
        except (OSError, subprocess.TimeoutExpired) as error:
            detail = f"Windows taskkill failed for owned PID {process.pid}: {type(error).__name__}"
        try:
            process.wait(timeout=shutdown_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=shutdown_seconds)
            detail += "; direct parent kill fallback was required"
        return detail

    detail = f"POSIX process-group termination requested for owned PGID {process.pid}"
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return "adapter process group had already exited"
    except OSError as error:
        process.kill()
        return f"process-group SIGTERM failed ({type(error).__name__}); direct parent kill fallback used"
    try:
        process.wait(timeout=shutdown_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=shutdown_seconds)
        detail += "; SIGKILL escalation was required"
    return detail


def run_adapter_process(
    command: list[str],
    *,
    input_text: str,
    timeout_seconds: int,
    terminate_process_tree_fn: Callable[[subprocess.Popen[str]], str],
    shutdown_seconds: int,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Run an adapter in its own killable tree under an outer watchdog."""

    popen_options: dict[str, Any] = {
        "cwd": cwd,
        "text": True,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        popen_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(command, **popen_options)
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        termination_detail = terminate_process_tree_fn(process)
        try:
            final_stdout, final_stderr = process.communicate(timeout=shutdown_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            final_stdout, final_stderr = process.communicate(timeout=shutdown_seconds)
            termination_detail += "; final direct process kill was required"
        error.output = _text_output(final_stdout) or _text_output(error.output)
        error.stderr = _text_output(final_stderr) or _text_output(error.stderr)
        error.stderr += f"\n[orchestrator] {termination_detail}\n"
        raise error
    return subprocess.CompletedProcess(command, int(process.returncode or 0), stdout, stderr)
