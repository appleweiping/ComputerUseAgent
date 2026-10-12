"""Closed schemas, protocol pins, hashing primitives, and state chain for the host worker.

This module is the single home of the ScienceBoard protocol pins (commit, model
endpoint, VM archive hash, evaluator sources, guest bundle name and phase
timeouts). It imports only the standard library because the host worker runs
it flat (``python host_worker.py``) inside the pinned host interpreter, which
has neither this repository's ``research`` package nor ``computer_use_agent``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


PINNED_COMMIT = "c8d5010bdba32afc5dfb16472873bed148c39a45"
OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
# SHA-256 of the official ScienceBoard VM archive (checked by vm_provenance/preflight).
EXPECTED_VM_ZIP_SHA256 = "6795b1e5b3c14a45250c9cad972d86278cceef703c29c84a957df3b567b4a24e"
# Official per-application evaluator modules at PINNED_COMMIT and their SHA-256.
EVALUATOR_SOURCES: dict[str, tuple[str, str]] = {
    "KAlgebra": (
        "sci/KAlgebra/task.py",
        "40f3a0e5e0ef649099ab7796cd91eef6df326cc34a29d128557371c33d22806f",
    ),
    "Celestia": (
        "sci/Celestia/task.py",
        "50fbe14ea0129b8620c4898e4356df266ae68a8112d687b3912fa40465fc72fb",
    ),
    "GrassGIS": (
        "sci/GrassGIS/task.py",
        "606f5876496ce4f17352d3ae5d8a83df9ebe9bf471633290cbec764c085ea973",
    ),
}
# The single guest deployment unit built by computer_use_agent.capture.x11.bundle.
GUEST_BUNDLE_NAME = "computer-use-agent-x11-recorder.pyz"
GUEST_PYTHON = "/usr/bin/python3"
# Guest recorder data/control directories (overridable, but always under /home/user).
DEFAULT_GUEST_DATA_ROOT = "/home/user/.local/share/computer-use-agent/sessions"
DEFAULT_GUEST_CONTROL_ROOT = "/home/user/.local/share/computer-use-agent/control"
# Driver v2 guest-side agent/validator settings; v3 rejects them when set.
DEPRECATED_GUEST_VARIABLES = (
    "SCIENCEBOARD_GUEST_ROOT",
    "SCIENCEBOARD_AIO_LAUNCHER",
    "SCIENCEBOARD_SEEACT_LAUNCHER",
    "SCIENCEBOARD_VALIDATOR_LAUNCHER",
)
WORKER_SPEC_SCHEMA = "scienceboard-host-worker-spec-v3"
WORKER_STATE_SCHEMA = "scienceboard-host-worker-state-v3"
HOST_DOCTOR_SCHEMA = "scienceboard-host-capabilities-v3"
VALIDATOR_SCHEMA = "scienceboard-validator-result-v2"
MAX_MESSAGE_BYTES = 1_048_576
# A phase client waits for the phase's own budget plus this margin, so a phase
# that finishes at its deadline can still deliver its response. A driver's
# outer subprocess timeout must exceed the phase budget by at least twice the
# margin (preflight enforces this).
PHASE_RESPONSE_MARGIN_SECONDS = 15.0
# The worker service gives a connected client this long to deliver its one
# request line, and the same bound to accept the response.
WORKER_IO_TIMEOUT_SECONDS = 30.0

WORKER_SPEC_FIELDS = {
    "schema_version", "session_id", "task_id", "task_path",
    "task_canonical_sha256", "task_binding", "session_dir",
    "scienceboard_root", "scienceboard_commit", "worker_source_sha256",
    "vm_path", "snapshot", "actor", "community", "model",
    "reasoning_effort", "openai_chat_completions_url", "action_limit",
    "timeout_seconds", "protocol_version", "executor_version", "guest_id",
    "guest_os_id", "guest_os_version", "screen_width", "screen_height",
    "guest_data_root", "guest_control_root", "guest_bundle_root",
    "guest_bundle_hashes", "manual_stop_file", "self_consent_confirmed",
}

TRANSITIONS = {
    "prepared": ("initialize", "initialized"),
    "initialized": ("start_recorder", "capturing"),
    "capturing": ("run_actor", "acted"),
    "acted": ("screenshot", "screenshot_captured"),
    "screenshot_captured": ("stop_recorder", "capture_stopped"),
    "capture_stopped": ("evaluate", "evaluated"),
    "evaluated": ("finalize", "finalized"),
}

# The existing v3 ``worker_source_sha256`` field binds this deterministic
# aggregate. The schema stays closed and unchanged while every executable
# module introduced by the split remains tamper-evident.
HOST_WORKER_SOURCE_FILES = (
    "host_worker.py",
    "worker_contract.py",
    "runtime_doctor.py",
    "official_runtime.py",
)


class WorkerProtocolError(RuntimeError):
    """Raised for a replay, ordering, authentication, or integrity error."""


class OfficialRuntimeProtocol(Protocol):
    def prepare(self) -> Mapping[str, object]: ...
    def initialize(self) -> Mapping[str, object]: ...
    def start_recorder(self) -> Mapping[str, object]: ...
    def run_actor(self) -> Mapping[str, object]: ...
    def screenshot(self) -> Mapping[str, object]: ...
    def stop_recorder(self) -> Mapping[str, object]: ...
    def evaluate(self) -> Mapping[str, object]: ...
    def finalize(self) -> Mapping[str, object]: ...
    def teardown(self) -> None: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def worker_source_manifest(root: Path | None = None) -> dict[str, str]:
    source_root = Path(root or Path(__file__).resolve().parent).resolve()
    return {
        name: sha256(source_root / name)
        for name in HOST_WORKER_SOURCE_FILES
    }


def worker_source_sha256(root: Path | None = None) -> str:
    return canonical_sha256(worker_source_manifest(root))


def _atomic_json(path: Path, payload: Mapping[str, object], *, replace: bool) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    if not replace and path.exists():
        temporary.unlink(missing_ok=True)
        raise FileExistsError(f"Refusing to overwrite {path}")
    os.replace(temporary, path)


def _safe_session_id(value: object) -> str:
    session_id = str(value)
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    if (
        not session_id
        or len(session_id) > 160
        or any(character not in allowed for character in session_id)
    ):
        raise WorkerProtocolError("Unsafe session_id")
    return session_id


def _within(path: Path, root: Path, *, label: str) -> Path:
    resolved, boundary = Path(path).resolve(), Path(root).resolve()
    try:
        resolved.relative_to(boundary)
    except ValueError as error:
        raise WorkerProtocolError(f"{label} escapes its declared root") from error
    return resolved


def _state_without_hash(state: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in state.items() if key != "state_hash"}


def state_hash(state: Mapping[str, object]) -> str:
    return canonical_sha256(_state_without_hash(state))


def validate_state(state: Mapping[str, object]) -> None:
    if state.get("schema_version") != WORKER_STATE_SCHEMA:
        raise WorkerProtocolError("Worker state schema mismatch")
    observed = state.get("state_hash")
    if not isinstance(observed, str) or not hmac.compare_digest(
        observed, state_hash(state)
    ):
        raise WorkerProtocolError("Worker state hash mismatch")
    _safe_session_id(state.get("session_id"))


class WorkerStateMachine:
    """Pure, testable state chain used by the live worker service."""

    def __init__(
        self,
        *,
        session_id: str,
        nonce: str,
        endpoint: Mapping[str, object],
        pid: int,
        prepared_evidence: Mapping[str, object],
    ) -> None:
        if len(nonce) < 32:
            raise WorkerProtocolError("Worker nonce must contain at least 32 characters")
        self.state: dict[str, object] = {
            "schema_version": WORKER_STATE_SCHEMA,
            "session_id": _safe_session_id(session_id),
            "nonce_sha256": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
            "endpoint": dict(endpoint),
            "pid": int(pid),
            "phase": "prepared",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "request_ids": [],
            "history": [
                {
                    "command": "prepare",
                    "from": "new",
                    "to": "prepared",
                    "at": utc_now(),
                    "evidence_sha256": canonical_sha256(dict(prepared_evidence)),
                    "previous_state_hash": None,
                }
            ],
            "failures": [],
        }
        self.state["state_hash"] = state_hash(self.state)

    def authenticate(self, nonce: str) -> None:
        expected = str(self.state["nonce_sha256"])
        observed = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected, observed):
            raise WorkerProtocolError("Worker nonce mismatch")

    def transition(
        self,
        *,
        nonce: str,
        request_id: str,
        command: str,
        expected_state_hash: str,
        action: Callable[[], Mapping[str, object]],
    ) -> Mapping[str, object]:
        validate_state(self.state)
        self.authenticate(nonce)
        if not request_id or len(request_id) > 240:
            raise WorkerProtocolError("Invalid request_id")
        request_ids_value = self.state["request_ids"]
        if not isinstance(request_ids_value, list):
            raise WorkerProtocolError("Worker request-id ledger is invalid")
        request_ids = list(request_ids_value)
        if request_id in request_ids:
            raise WorkerProtocolError("Replayed request_id")
        if not hmac.compare_digest(str(self.state["state_hash"]), expected_state_hash):
            raise WorkerProtocolError("Stale or tampered expected_state_hash")
        phase = str(self.state["phase"])
        transition = TRANSITIONS.get(phase)
        if transition is None or transition[0] != command:
            raise WorkerProtocolError(
                f"Command {command!r} is invalid from phase {phase!r}"
            )

        previous_hash = str(self.state["state_hash"])
        request_ids.append(request_id)
        try:
            evidence = dict(action())
        except Exception as error:
            failures_value = self.state["failures"]
            if not isinstance(failures_value, list):
                raise WorkerProtocolError("Worker failure ledger is invalid")
            failures = list(failures_value)
            failures.append(
                {
                    "request_id": request_id,
                    "command": command,
                    "at": utc_now(),
                    "error_type": type(error).__name__,
                    "error_sha256": hashlib.sha256(
                        str(error).encode("utf-8")
                    ).hexdigest(),
                    "previous_state_hash": previous_hash,
                }
            )
            self.state.update(
                {
                    "request_ids": request_ids,
                    "failures": failures,
                    "updated_at": utc_now(),
                }
            )
            self.state["state_hash"] = state_hash(self.state)
            raise

        history_value = self.state["history"]
        if not isinstance(history_value, list):
            raise WorkerProtocolError("Worker history ledger is invalid")
        history = list(history_value)
        history.append(
            {
                "request_id": request_id,
                "command": command,
                "from": phase,
                "to": transition[1],
                "at": utc_now(),
                "evidence_sha256": canonical_sha256(evidence),
                "previous_state_hash": previous_hash,
            }
        )
        self.state.update(
            {
                "phase": transition[1],
                "request_ids": request_ids,
                "history": history,
                "updated_at": utc_now(),
            }
        )
        self.state["state_hash"] = state_hash(self.state)
        return evidence
