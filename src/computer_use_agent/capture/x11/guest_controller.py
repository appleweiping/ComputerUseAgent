"""Recorder-only controller deployed inside the official ScienceBoard guest.

This process does not load ScienceBoard source, an agent, a task, or an
evaluator.  The authenticated host worker owns all of those objects.  The guest
surface is deliberately limited to X11 capability checks and cooperative
start/stop of the privacy-minimised recorder.

In the guest it runs from the single-file bundle (see
:mod:`computer_use_agent.capture.x11.bundle`)::

    python3 computer-use-agent-x11-recorder.pyz doctor --control-dir ... --data-root ...
    python3 computer-use-agent-x11-recorder.pyz start-recorder ...
    python3 computer-use-agent-x11-recorder.pyz stop-recorder ...

``start-recorder`` launches the recorder from the same bundle
(``python3 <bundle> record ...``), so the guest needs exactly one deployed file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ...trace.io import load_trace
from .recorder import ADAPTER_VERSION, DEFAULT_GUEST_ID, DEFAULT_SCIENCEBOARD_COMMIT, x11_doctor

PINNED_COMMIT = DEFAULT_SCIENCEBOARD_COMMIT
GUEST_SCHEMA = "scienceboard-guest-recorder-capabilities-v2"


def _bundle_archive() -> Path | None:
    """Return the zipapp this module was imported from, if any."""

    loader = getattr(__spec__, "loader", None)
    archive = getattr(loader, "archive", None)
    if isinstance(archive, str) and archive:
        return Path(archive).resolve()
    return None


def recorder_command() -> list[str]:
    """Return the argv prefix that starts the X11 recorder process.

    Inside the deployed zipapp the recorder is the same archive's ``record``
    command; from a source checkout it is the package's ``__main__``.
    """

    archive = _bundle_archive()
    if archive is not None:
        return [sys.executable, str(archive), "record"]
    return [sys.executable, "-m", "computer_use_agent.capture.x11", "record"]


def _check(name: str, passed: bool, detail: object) -> dict[str, object]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _os_release() -> dict[str, str]:
    try:
        return dict(platform.freedesktop_os_release())
    except (AttributeError, OSError):
        return {}


def _screen_size() -> tuple[int, int] | None:
    try:
        from Xlib import display

        connection = display.Display()
        try:
            screen = connection.screen()
            return int(screen.width_in_pixels), int(screen.height_in_pixels)
        finally:
            connection.close()
    except Exception:
        return None


def _under_home_user(path: Path, *, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(Path("/home/user"))
    except ValueError as error:
        raise ValueError(f"{label} must remain under /home/user") from error
    return resolved


def _writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-probe-{secrets.token_hex(4)}"
        probe.write_text("private\n", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def capability_doctor(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, object]] = []
    release = _os_release()
    measured_os_id = release.get("ID", "unknown")
    measured_os_version = release.get("VERSION_ID", "unknown")
    checks.append(
        _check(
            "guest_os",
            (measured_os_id, measured_os_version)
            == (args.expected_os_id, args.expected_os_version),
            {
                "measured": [measured_os_id, measured_os_version],
                "expected": [args.expected_os_id, args.expected_os_version],
            },
        )
    )
    screen = _screen_size()
    checks.append(
        _check(
            "screen_resolution",
            screen == (args.expected_width, args.expected_height),
            {
                "measured": screen,
                "expected": [args.expected_width, args.expected_height],
            },
        )
    )
    x11 = x11_doctor()
    checks.append(_check("x11_record_xtest", bool(x11.get("ready")), x11))
    try:
        control_dir = _under_home_user(args.control_dir, label="control directory")
        data_root = _under_home_user(args.data_root, label="data root")
    except ValueError as error:
        checks.append(_check("private_guest_paths", False, str(error)))
    else:
        checks.append(
            _check(
                "private_guest_paths",
                _writable_directory(control_dir) and _writable_directory(data_root),
                {"control": str(control_dir), "data": str(data_root)},
            )
        )
    return {
        "schema_version": GUEST_SCHEMA,
        "ready": bool(checks) and all(bool(check["passed"]) for check in checks),
        "binding": {
            "guest_id": args.guest_id,
            "guest_os_id": measured_os_id,
            "guest_os_version": measured_os_version,
            "screen_resolution": (
                None if screen is None else {"width": screen[0], "height": screen[1]}
            ),
            "capture_adapter": "x11_vm",
            "recorder_version": ADAPTER_VERSION,
        },
        "checks": checks,
        "forbidden_capabilities": [
            "ScienceBoard source checkout",
            "AIOAgent/SeeAct execution",
            "Task.init/Task.eval",
            "validator launcher",
        ],
        "claim_boundary": "Guest recorder capability only; no task or actor was executed.",
    }


def _state_path(control_dir: Path, session_id: str) -> Path:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    if not session_id or len(session_id) > 160 or any(char not in allowed for char in session_id):
        raise ValueError("Unsafe session_id")
    root = _under_home_user(control_dir, label="control directory")
    return root / f"{session_id}.recorder-state.json"


def _atomic_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    if path.exists():
        temporary.unlink(missing_ok=True)
        raise FileExistsError("Refusing to overwrite recorder state")
    os.replace(temporary, path)


def _state_hash(payload: dict[str, object]) -> str:
    normalized = {key: value for key, value in payload.items() if key != "state_sha256"}
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def start_recorder(args: argparse.Namespace) -> dict[str, Any]:
    if args.actor == "human" and not args.self_consent_confirmed:
        raise ValueError("Self-human recording requires an explicit --self-consent-confirmed flag")
    data_root = _under_home_user(args.data_root, label="data root")
    output = args.output.resolve()
    try:
        output.relative_to(data_root)
    except ValueError as error:
        raise ValueError("Trace output must remain under the private guest data root") from error
    state_path = _state_path(args.control_dir, args.session_id)
    if (
        state_path.exists()
        or output.exists()
        or output.with_suffix(output.suffix + ".partial").exists()
    ):
        raise FileExistsError("Refusing to overwrite recorder state/trace")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    control_file = state_path.with_suffix(".stop")
    stdout_path = state_path.with_suffix(".stdout.log")
    stderr_path = state_path.with_suffix(".stderr.log")
    argv = [
        *recorder_command(),
        "--output",
        str(output),
        "--actor",
        args.actor,
        "--session-id",
        args.session_id,
        "--task-id",
        args.task_id,
        "--protocol-version",
        args.protocol_version,
        "--executor-version",
        args.executor_version,
        "--guest-id",
        args.guest_id,
        "--guest-os-id",
        args.guest_os_id,
        "--guest-os-version",
        args.guest_os_version,
        "--screen-width",
        str(args.screen_width),
        "--screen-height",
        str(args.screen_height),
        "--scienceboard-commit",
        args.scienceboard_commit,
        "--duration-seconds",
        str(args.duration_seconds),
        "--control-file",
        str(control_file),
    ]
    if args.actor == "human":
        argv.extend(["--participant-code", args.participant_code, "--consent-confirmed"])
    else:
        argv.append("--controller-confirmed")
    with (
        stdout_path.open("x", encoding="utf-8") as stdout,
        stderr_path.open("x", encoding="utf-8") as stderr,
    ):
        process = subprocess.Popen(
            argv,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
        )
    time.sleep(0.35)
    if process.poll() is not None:
        raise RuntimeError(f"Recorder exited during startup with code {process.returncode}")
    state = {
        "schema_version": "scienceboard-recorder-state-v2",
        "session_id": args.session_id,
        "pid": process.pid,
        "trace": str(output),
        "control_file": str(control_file),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "argv_sha256": hashlib.sha256(
            json.dumps(argv, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    state["state_sha256"] = _state_hash(state)
    try:
        _atomic_state(state_path, state)
    except Exception:
        control_file.write_text("startup-state-write-failed\n", encoding="utf-8")
        raise
    return state


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_recorder(args: argparse.Namespace) -> dict[str, Any]:
    state_path = _state_path(args.control_dir, args.session_id)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema_version") != "scienceboard-recorder-state-v2":
        raise RuntimeError("Recorder state schema mismatch")
    if state.get("state_sha256") != _state_hash(state):
        raise RuntimeError("Recorder state hash mismatch")
    control_file = Path(str(state["control_file"]))
    control_file.write_text("cooperative-stop\n", encoding="utf-8")
    deadline = time.monotonic() + args.stop_timeout
    trace_path = Path(str(state["trace"]))
    clean_terminal = False
    while time.monotonic() < deadline:
        try:
            metadata, events, terminal = load_trace(trace_path)
            clean_terminal = (
                metadata.get("session_id") == args.session_id
                and terminal.get("status") == "complete"
                and terminal.get("event_count") == len(events)
            )
        except (OSError, ValueError, json.JSONDecodeError):
            clean_terminal = False
        if clean_terminal or not _pid_exists(int(state["pid"])):
            break
        time.sleep(0.1)
    if not clean_terminal and _pid_exists(int(state["pid"])):
        raise RuntimeError(
            "Recorder did not cooperatively stop; it was not killed and evidence is invalid"
        )
    metadata, events, terminal = load_trace(trace_path)
    if (
        metadata.get("session_id") != args.session_id
        or terminal.get("status") != "complete"
        or terminal.get("event_count") != len(events)
    ):
        raise RuntimeError("Recorder trace lacks a matching clean terminal record")
    return {
        "session_id": args.session_id,
        "event_count": len(events),
        "terminal": terminal,
        "zero_event_trace": len(events) == 0,
    }


def _common_binding(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--guest-id", default=DEFAULT_GUEST_ID)
    parser.add_argument("--guest-os-id", default="ubuntu")
    parser.add_argument("--guest-os-version", default="22.04")
    parser.add_argument("--screen-width", type=int, default=1920)
    parser.add_argument("--screen-height", type=int, default=1080)
    parser.add_argument("--scienceboard-commit", default=PINNED_COMMIT)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--expected-os-id", default="ubuntu")
    doctor.add_argument("--expected-os-version", default="22.04")
    doctor.add_argument("--expected-width", type=int, default=1920)
    doctor.add_argument("--expected-height", type=int, default=1080)
    doctor.add_argument("--guest-id", default=DEFAULT_GUEST_ID)
    doctor.add_argument("--control-dir", type=Path, required=True)
    doctor.add_argument("--data-root", type=Path, required=True)

    start = subparsers.add_parser("start-recorder")
    start.add_argument("--control-dir", type=Path, required=True)
    start.add_argument("--data-root", type=Path, required=True)
    start.add_argument("--output", type=Path, required=True)
    start.add_argument("--actor", choices=("human", "aio", "seeact", "xtest"), required=True)
    start.add_argument("--session-id", required=True)
    start.add_argument("--task-id", required=True)
    start.add_argument("--protocol-version", required=True)
    start.add_argument("--executor-version", required=True)
    start.add_argument("--duration-seconds", type=float, required=True)
    start.add_argument("--participant-code", default="self-human-001")
    start.add_argument("--self-consent-confirmed", action="store_true")
    _common_binding(start)

    stop = subparsers.add_parser("stop-recorder")
    stop.add_argument("--control-dir", type=Path, required=True)
    stop.add_argument("--session-id", required=True)
    stop.add_argument("--stop-timeout", type=float, default=15.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "doctor":
            result = capability_doctor(args)
            code = 0 if result["ready"] else 2
        elif args.command == "start-recorder":
            result, code = start_recorder(args), 0
        else:
            result, code = stop_recorder(args), 0
    except Exception as error:
        result, code = {"error": type(error).__name__, "detail": str(error)}, 2
    print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
