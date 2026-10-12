"""Authenticated host worker for one official ScienceBoard task lifecycle.

ScienceBoard's ``VManager``, task object, agent community, and evaluator all
live on the host. They stay alive in the *same process* from ``Task.init()``
through ``Task.eval()``. This stable entrypoint owns the authenticated local
service and phase-based CLI; implementation contracts, the read-only doctor,
and the official runtime bridge live in focused sibling modules.

``doctor`` never starts a VM or calls a model. ``start`` launches one private,
localhost-only worker; subsequent ``phase`` calls authenticate with a nonce
file and advance an immutable state chain. Replayed, reordered, or tampered
requests fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

if __package__ in {None, ""}:
    from official_runtime import FakeOfficialRuntime, OfficialRuntime  # type: ignore[import-not-found]
    from runtime_doctor import (  # type: ignore[import-not-found]
        DESKTOP_ENV_INIT_ARGUMENTS,
        HOST_DEPENDENCY_VERSIONS,
        RUNTIME_COMPAT_HASHES,
        SOURCE_HASHES,
        SOURCE_SIGNATURES,
        activate_runtime_compat,
        inspect_source_signatures,
        runtime_compat_doctor,
        source_doctor,
    )
    from worker_contract import (  # type: ignore[import-not-found]
        EVALUATOR_SOURCES,
        HOST_DOCTOR_SCHEMA,
        HOST_WORKER_SOURCE_FILES,
        MAX_MESSAGE_BYTES,
        OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
        PHASE_RESPONSE_MARGIN_SECONDS,
        PINNED_COMMIT,
        TRANSITIONS,
        VALIDATOR_SCHEMA,
        WORKER_SPEC_FIELDS,
        WORKER_IO_TIMEOUT_SECONDS,
        WORKER_SPEC_SCHEMA,
        WORKER_STATE_SCHEMA,
        OfficialRuntimeProtocol,
        WorkerProtocolError,
        WorkerStateMachine,
        _atomic_json,
        _within,
        canonical_sha256,
        sha256,
        state_hash,
        utc_now,
        validate_state,
        worker_source_manifest,
        worker_source_sha256,
    )
else:
    from .official_runtime import FakeOfficialRuntime, OfficialRuntime
    from .runtime_doctor import (
        DESKTOP_ENV_INIT_ARGUMENTS,
        HOST_DEPENDENCY_VERSIONS,
        RUNTIME_COMPAT_HASHES,
        SOURCE_HASHES,
        SOURCE_SIGNATURES,
        activate_runtime_compat,
        inspect_source_signatures,
        runtime_compat_doctor,
        source_doctor,
    )
    from .worker_contract import (
        EVALUATOR_SOURCES,
        HOST_DOCTOR_SCHEMA,
        HOST_WORKER_SOURCE_FILES,
        MAX_MESSAGE_BYTES,
        OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
        PHASE_RESPONSE_MARGIN_SECONDS,
        PINNED_COMMIT,
        TRANSITIONS,
        VALIDATOR_SCHEMA,
        WORKER_SPEC_FIELDS,
        WORKER_IO_TIMEOUT_SECONDS,
        WORKER_SPEC_SCHEMA,
        WORKER_STATE_SCHEMA,
        OfficialRuntimeProtocol,
        WorkerProtocolError,
        WorkerStateMachine,
        _atomic_json,
        _within,
        canonical_sha256,
        sha256,
        state_hash,
        utc_now,
        validate_state,
        worker_source_manifest,
        worker_source_sha256,
    )


__all__ = [
    "DESKTOP_ENV_INIT_ARGUMENTS",
    "EVALUATOR_SOURCES",
    "FakeOfficialRuntime",
    "HOST_DEPENDENCY_VERSIONS",
    "HOST_DOCTOR_SCHEMA",
    "HOST_WORKER_SOURCE_FILES",
    "MAX_MESSAGE_BYTES",
    "OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL",
    "OfficialRuntime",
    "OfficialRuntimeProtocol",
    "PHASE_RESPONSE_MARGIN_SECONDS",
    "PINNED_COMMIT",
    "RUNTIME_COMPAT_HASHES",
    "SOURCE_HASHES",
    "SOURCE_SIGNATURES",
    "TRANSITIONS",
    "VALIDATOR_SCHEMA",
    "WORKER_IO_TIMEOUT_SECONDS",
    "WORKER_SPEC_FIELDS",
    "WORKER_SPEC_SCHEMA",
    "WORKER_STATE_SCHEMA",
    "WorkerProtocolError",
    "WorkerStateMachine",
    "activate_runtime_compat",
    "call_phase",
    "canonical_sha256",
    "inspect_source_signatures",
    "main",
    "parse_args",
    "phase_client_timeout",
    "runtime_compat_doctor",
    "serve",
    "sha256",
    "signal_manual_stop",
    "source_doctor",
    "start_worker",
    "state_hash",
    "utc_now",
    "validate_state",
    "worker_source_manifest",
    "worker_source_sha256",
]


def phase_client_timeout(phase_timeout: float) -> float:
    """Return how long a phase client waits for the worker's response.

    The worker may legitimately use the whole phase budget (for example the
    actor timeout) before it answers, so the client waits for that budget plus
    :data:`PHASE_RESPONSE_MARGIN_SECONDS`.
    """

    if phase_timeout <= 0:
        raise WorkerProtocolError("Phase timeout must be positive")
    return float(phase_timeout) + float(PHASE_RESPONSE_MARGIN_SECONDS)


def _recv_json(connection: socket.socket) -> dict[str, object]:
    """Read exactly one JSON line; the caller must set a socket timeout."""

    chunks, size = [], 0
    while True:
        block = connection.recv(65536)
        if not block:
            break
        size += len(block)
        if size > MAX_MESSAGE_BYTES:
            raise WorkerProtocolError("Worker message exceeds size limit")
        chunks.append(block)
        if b"\n" in block:
            break
    raw = b"".join(chunks)
    line, separator, trailing = raw.partition(b"\n")
    if not separator or trailing:
        raise WorkerProtocolError("Worker message must be exactly one JSON line")
    payload = json.loads(line.decode("utf-8"))
    if not isinstance(payload, dict):
        raise WorkerProtocolError("Worker message must be a JSON object")
    return payload


def _send_json(connection: socket.socket, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    connection.sendall(encoded)


def _write_state(path: Path, machine: WorkerStateMachine) -> None:
    validate_state(machine.state)
    _atomic_json(path, machine.state, replace=path.exists())


def serve(spec_path: Path, state_path: Path, nonce_path: Path) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    nonce = nonce_path.read_text(encoding="utf-8").strip()
    runtime = OfficialRuntime(spec)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    try:
        prepared = runtime.prepare()
        machine = WorkerStateMachine(
            session_id=str(spec["session_id"]),
            nonce=nonce,
            endpoint={"host": host, "port": port},
            pid=os.getpid(),
            prepared_evidence=prepared,
        )
        _write_state(state_path, machine)
        methods: dict[str, Callable[[], Mapping[str, object]]] = {
            "initialize": runtime.initialize,
            "start_recorder": runtime.start_recorder,
            "run_actor": runtime.run_actor,
            "screenshot": runtime.screenshot,
            "stop_recorder": runtime.stop_recorder,
            "evaluate": runtime.evaluate,
            "finalize": runtime.finalize,
        }
        finished = False
        while not finished:
            connection, _ = listener.accept()
            with connection:
                # A client that connects but never sends must not wedge the
                # single-client worker; the phase action itself is unbounded by
                # this timeout because it runs between receive and send.
                connection.settimeout(WORKER_IO_TIMEOUT_SECONDS)
                try:
                    request = _recv_json(connection)
                    if request.get("session_id") != spec["session_id"]:
                        raise WorkerProtocolError(
                            "Request is bound to another session"
                        )
                    command = str(request.get("command", ""))
                    if command not in methods:
                        raise WorkerProtocolError("Unknown worker command")
                    evidence = machine.transition(
                        nonce=str(request.get("nonce", "")),
                        request_id=str(request.get("request_id", "")),
                        command=command,
                        expected_state_hash=str(
                            request.get("expected_state_hash", "")
                        ),
                        action=methods[command],
                    )
                    _write_state(state_path, machine)
                    # Decide before replying: a finalized session ends even if
                    # the client is no longer there to read the response.
                    finished = machine.state["phase"] == "finalized"
                    _send_json(
                        connection,
                        {
                            "ok": True,
                            "phase": machine.state["phase"],
                            "state_hash": machine.state["state_hash"],
                            "evidence": evidence,
                        },
                    )
                except Exception as error:
                    if "machine" in locals():
                        _write_state(state_path, machine)
                        finished = machine.state["phase"] == "finalized"
                    try:
                        _send_json(
                            connection,
                            {
                                "ok": False,
                                "error": type(error).__name__,
                                "detail": str(error),
                            },
                        )
                    except OSError:
                        # The client already gave up (timeout or disconnect);
                        # the persisted state chain still records the outcome.
                        pass
        return 0
    finally:
        listener.close()
        runtime.teardown()


def start_worker(
    spec_path: Path,
    state_path: Path,
    nonce_path: Path,
    timeout: float,
) -> dict[str, object]:
    for path in (spec_path, nonce_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    if state_path.exists():
        raise FileExistsError("Refusing to overwrite an existing worker state")
    session_dir = spec_path.resolve().parent
    stdout_path = session_dir / ".host-worker.stdout.log"
    stderr_path = session_dir / ".host-worker.stderr.log"
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_serve",
        "--spec",
        str(spec_path.resolve()),
        "--state",
        str(state_path.resolve()),
        "--nonce-file",
        str(nonce_path.resolve()),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
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
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if state_path.is_file():
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise WorkerProtocolError("Worker state must be a JSON object")
            state: dict[str, object] = loaded
            validate_state(state)
            if state.get("phase") != "prepared" or state.get("pid") != process.pid:
                raise WorkerProtocolError("Worker prepared state is inconsistent")
            return state
        if process.poll() is not None:
            raise RuntimeError(
                "Host worker exited during preparation with "
                f"code {process.returncode}"
            )
        time.sleep(0.05)
    raise TimeoutError("Host worker did not publish a prepared state before timeout")


def call_phase(
    state_path: Path,
    nonce_path: Path,
    *,
    command: str,
    request_id: str,
    timeout: float,
) -> dict[str, object]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    validate_state(state)
    endpoint = state.get("endpoint")
    if not isinstance(endpoint, Mapping):
        raise WorkerProtocolError("Worker endpoint is absent")
    nonce = nonce_path.read_text(encoding="utf-8").strip()
    request = {
        "session_id": state["session_id"],
        "nonce": nonce,
        "request_id": request_id,
        "command": command,
        "expected_state_hash": state["state_hash"],
    }
    response_timeout = phase_client_timeout(timeout)
    with socket.create_connection(
        (str(endpoint["host"]), int(endpoint["port"])),
        timeout=WORKER_IO_TIMEOUT_SECONDS,
    ) as connection:
        connection.settimeout(WORKER_IO_TIMEOUT_SECONDS)
        _send_json(connection, request)
        connection.settimeout(response_timeout)
        response = _recv_json(connection)
    if response.get("ok") is not True:
        raise WorkerProtocolError(
            f"Worker rejected {command}: {response.get('error')}: "
            f"{response.get('detail')}"
        )
    updated = json.loads(state_path.read_text(encoding="utf-8"))
    validate_state(updated)
    if updated.get("state_hash") != response.get("state_hash"):
        raise WorkerProtocolError("Worker response/state hash mismatch")
    return response


def signal_manual_stop(
    spec_path: Path,
    state_path: Path,
    nonce_path: Path,
    *,
    stop_type: str,
    stop_args: list[str],
    confirmed: bool,
) -> dict[str, object]:
    """Write the human's explicit stop decision while ``run_human`` waits."""

    if not confirmed:
        raise WorkerProtocolError("Manual stop requires --confirm-self-action")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(spec, Mapping) or set(spec) != WORKER_SPEC_FIELDS:
        raise WorkerProtocolError("Manual stop worker spec is invalid")
    if not isinstance(state, Mapping):
        raise WorkerProtocolError("Manual stop worker state is invalid")
    validate_state(state)
    if spec.get("actor") != "human" or spec.get("session_id") != state.get(
        "session_id"
    ):
        raise WorkerProtocolError("Manual stop is not bound to this human session")
    if state.get("phase") != "capturing":
        raise WorkerProtocolError(
            "Manual stop is accepted only while the human phase is active"
        )
    nonce = nonce_path.read_text(encoding="utf-8").strip()
    observed_nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(observed_nonce_hash, str(state.get("nonce_sha256"))):
        raise WorkerProtocolError("Manual stop nonce mismatch")
    if stop_type not in {"DONE", "FAIL", "TIMEOUT", "ANS"}:
        raise WorkerProtocolError("Manual stop type is not an official Primitive")
    if not all(
        isinstance(value, str) and len(value) <= 256 and "\n" not in value
        for value in stop_args
    ):
        raise WorkerProtocolError("Manual stop args are invalid")
    session_dir = Path(str(spec["session_dir"])).resolve()
    destination = _within(
        Path(str(spec["manual_stop_file"])), session_dir, label="manual stop file"
    )
    payload = {
        "schema_version": "scienceboard-manual-stop-v1",
        "session_id": str(spec["session_id"]),
        "stop_type": stop_type,
        "stop_args": stop_args,
        "confirmed_at": utc_now(),
    }
    _atomic_json(destination, payload, replace=False)
    return {
        "session_id": str(spec["session_id"]),
        "stop_type": stop_type,
        "stop_args_sha256": canonical_sha256({"stop_args": stop_args}),
        "signal_sha256": sha256(destination),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--scienceboard-root", type=Path, required=True)
    doctor.add_argument("--expected-commit", default=PINNED_COMMIT)
    doctor.add_argument("--task-bindings-json", required=True)
    doctor.add_argument("--guest-bundle-root", type=Path, required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--spec", type=Path, required=True)
    start.add_argument("--state", type=Path, required=True)
    start.add_argument("--nonce-file", type=Path, required=True)
    start.add_argument("--startup-timeout", type=float, default=120.0)

    phase = subparsers.add_parser("phase")
    phase.add_argument("--state", type=Path, required=True)
    phase.add_argument("--nonce-file", type=Path, required=True)
    phase.add_argument(
        "--name",
        choices=tuple(item[0] for item in TRANSITIONS.values()),
        required=True,
    )
    phase.add_argument("--request-id", required=True)
    phase.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help=(
            "phase budget in seconds; the client waits this long plus "
            f"{PHASE_RESPONSE_MARGIN_SECONDS:g} s for the worker's response"
        ),
    )

    manual = subparsers.add_parser("signal-manual-stop")
    manual.add_argument("--spec", type=Path, required=True)
    manual.add_argument("--state", type=Path, required=True)
    manual.add_argument("--nonce-file", type=Path, required=True)
    manual.add_argument(
        "--stop-type",
        choices=("DONE", "FAIL", "TIMEOUT", "ANS"),
        required=True,
    )
    manual.add_argument("--stop-arg", action="append", default=[])
    manual.add_argument("--confirm-self-action", action="store_true")

    serve_parser = subparsers.add_parser("_serve")
    serve_parser.add_argument("--spec", type=Path, required=True)
    serve_parser.add_argument("--state", type=Path, required=True)
    serve_parser.add_argument("--nonce-file", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "doctor":
            bindings = json.loads(args.task_bindings_json)
            if not isinstance(bindings, list):
                raise WorkerProtocolError(
                    "task-bindings-json must decode to a list"
                )
            result = source_doctor(
                args.scienceboard_root,
                expected_commit=args.expected_commit,
                task_bindings=bindings,
                guest_bundle_root=args.guest_bundle_root,
            )
            code = 0 if result["ready"] else 2
        elif args.command == "start":
            result = start_worker(
                args.spec, args.state, args.nonce_file, args.startup_timeout
            )
            code = 0
        elif args.command == "phase":
            result = call_phase(
                args.state,
                args.nonce_file,
                command=args.name,
                request_id=args.request_id,
                timeout=args.timeout,
            )
            code = 0
        elif args.command == "signal-manual-stop":
            result = signal_manual_stop(
                args.spec,
                args.state,
                args.nonce_file,
                stop_type=args.stop_type,
                stop_args=args.stop_arg,
                confirmed=args.confirm_self_action,
            )
            code = 0
        else:
            return serve(args.spec, args.state, args.nonce_file)
    except Exception as error:
        result = {
            "ok": False,
            "error": type(error).__name__,
            "detail": str(error),
        }
        code = 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
