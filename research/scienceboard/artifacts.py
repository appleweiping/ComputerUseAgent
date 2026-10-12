"""Fail-closed validation and hashing for a ScienceBoard session bundle."""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
from datetime import datetime
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from computer_use_agent.trace.io import load_trace, validate_event_stream
from research.scienceboard.host_worker import (
    WORKER_SPEC_FIELDS,
)
from research.scienceboard.host_worker import (
    validate_state as validate_worker_state,
)
from research.scienceboard.worker_contract import EVALUATOR_SOURCES


class EvidenceValidationError(ValueError):
    """Raised when a session cannot count toward the formal 27-cell matrix."""


# Derived from the single pin table in worker_contract.EVALUATOR_SOURCES.
EVALUATOR_SOURCE_HASHES = {
    application: {"module": module, "sha256": digest}
    for application, (module, digest) in EVALUATOR_SOURCES.items()
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(path: Path) -> str:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def artifact_descriptor(path: Path, *, relative_to: Path) -> dict[str, object]:
    resolved = Path(path).resolve()
    return {
        "path": resolved.relative_to(relative_to.resolve()).as_posix(),
        "sha256": sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def validate_png(path: Path, *, width: int, height: int) -> dict[str, int]:
    """Parse PNG chunks, CRCs and exact dimensions without image dependencies."""

    data = Path(path).read_bytes()
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        raise EvidenceValidationError("final_state.png does not have a PNG signature")
    offset, dimensions, saw_iend = len(signature), None, False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise EvidenceValidationError("final_state.png has a truncated chunk")
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        actual_crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise EvidenceValidationError("final_state.png has a CRC mismatch")
        if chunk_type == b"IHDR":
            if length != 13 or dimensions is not None:
                raise EvidenceValidationError("final_state.png has an invalid IHDR")
            dimensions = struct.unpack(">II", payload[:8])
        if chunk_type == b"IEND":
            if length != 0 or end != len(data):
                raise EvidenceValidationError("final_state.png has an invalid trailing IEND")
            saw_iend = True
            break
        offset = end
    if not saw_iend or dimensions is None:
        raise EvidenceValidationError("final_state.png is not a complete PNG")
    if dimensions != (width, height):
        raise EvidenceValidationError(
            f"final_state.png dimensions {dimensions} do not match pinned {(width, height)}"
        )
    return {"width": dimensions[0], "height": dimensions[1], "size_bytes": len(data)}


def _task_spec(manifest: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
    matches = [task for task in manifest["tasks"] if task.get("id") == task_id]
    if len(matches) != 1:
        raise EvidenceValidationError(f"Unknown or duplicate formal task_id: {task_id}")
    match = matches[0]
    if not isinstance(match, Mapping):
        raise EvidenceValidationError("Formal task specification is not an object")
    return dict(match)


def _missing_modalities(events: Sequence[Mapping[str, Any]]) -> list[str]:
    event_types = {str(event.get("type", "")).lower() for event in events}
    observed = {
        "pointer": any("pointer" in value or value in {"mousedown", "mouseup", "click"} for value in event_types),
        "keyboard": any("key" in value for value in event_types),
        "wheel": any("wheel" in value or "scroll" in value for value in event_types),
        "focus": any("focus" in value or "window" in value for value in event_types),
    }
    return sorted(name for name, present in observed.items() if not present)


def _validate_worker_records(
    spec_path: Path,
    state_path: Path,
    *,
    expected: Mapping[str, Any],
    task_hash: str,
    scienceboard_commit: str,
    model: str,
) -> Mapping[str, Any] | None:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(spec, Mapping) or spec.get("schema_version") != "scienceboard-host-worker-spec-v3":
        raise EvidenceValidationError("worker_spec.json schema is invalid")
    if set(spec) != WORKER_SPEC_FIELDS:
        raise EvidenceValidationError("worker_spec.json fields differ from the closed v3 contract")
    expected_spec = {
        "session_id": expected["session_id"],
        "task_id": expected["task_id"],
        "actor": expected["actor"],
        "scienceboard_commit": scienceboard_commit,
        "task_canonical_sha256": task_hash,
        "model": model,
    }
    mismatches = {
        key: {"expected": value, "observed": spec.get(key)}
        for key, value in expected_spec.items()
        if spec.get(key) != value
    }
    if mismatches:
        raise EvidenceValidationError(f"worker spec provenance mismatch: {mismatches}")
    worker_hash = spec.get("worker_source_sha256")
    if (
        not isinstance(worker_hash, str)
        or len(worker_hash) != 64
        or any(character not in "0123456789abcdef" for character in worker_hash)
    ):
        raise EvidenceValidationError("worker spec has no source hash")
    if not isinstance(state, Mapping):
        raise EvidenceValidationError("worker_state.json must be an object")
    try:
        validate_worker_state(state)
    except (ValueError, RuntimeError) as error:
        raise EvidenceValidationError(f"worker state is invalid: {error}") from error
    if state.get("session_id") != expected["session_id"] or state.get("phase") != "finalized":
        raise EvidenceValidationError("worker state is not the finalized expected session")
    history = state.get("history")
    if not isinstance(history, list):
        raise EvidenceValidationError("worker phase history is absent")
    observed = [item.get("command") for item in history if isinstance(item, Mapping)]
    required = [
        "prepare", "initialize", "start_recorder", "run_actor", "screenshot",
        "stop_recorder", "evaluate", "finalize",
    ]
    if observed != required:
        raise EvidenceValidationError("worker phase history is incomplete or reordered")
    if expected["actor"] != "human":
        return None
    if spec.get("self_consent_confirmed") is not True:
        raise EvidenceValidationError("human worker spec lacks explicit self-consent binding")
    manual_path = (spec_path.parent / ".manual-stop.json").resolve()
    if Path(str(spec.get("manual_stop_file"))).resolve() != manual_path or not manual_path.is_file():
        raise EvidenceValidationError("human manual stop evidence is missing or misbound")
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    if not isinstance(manual, Mapping) or set(manual) != {
        "schema_version", "session_id", "stop_type", "stop_args", "confirmed_at"
    }:
        raise EvidenceValidationError("human manual stop evidence schema is invalid")
    if (
        manual["schema_version"] != "scienceboard-manual-stop-v1"
        or manual["session_id"] != expected["session_id"]
        or manual["stop_type"] not in {"DONE", "FAIL", "TIMEOUT", "ANS"}
        or not isinstance(manual["stop_args"], list)
        or not all(
            isinstance(value, str) and len(value) <= 256 and "\n" not in value
            for value in manual["stop_args"]
        )
    ):
        raise EvidenceValidationError("human manual stop evidence is not bound to the session")
    try:
        datetime.fromisoformat(str(manual["confirmed_at"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceValidationError("human manual stop confirmed_at is invalid") from error
    return manual


def _validate_validator(
    path: Path,
    *,
    session_dir: Path,
    expected: Mapping[str, Any],
    task: Mapping[str, Any],
    task_hash: str,
    scienceboard_commit: str,
    expected_stop: Mapping[str, Any] | None,
) -> bool:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version", "session_id", "task_id", "passed", "evaluated_at",
        "stop", "evaluator", "official_evidence",
    }
    if set(payload) != required:
        raise EvidenceValidationError("validator_result.json fields differ from the normalized contract")
    if payload["schema_version"] != "scienceboard-validator-result-v2":
        raise EvidenceValidationError("validator_result.json schema is invalid")
    if payload["session_id"] != expected["session_id"] or payload["task_id"] != expected["task_id"]:
        raise EvidenceValidationError("validator_result.json is bound to another session/task")
    if type(payload["passed"]) is not bool:
        raise EvidenceValidationError("validator passed must be boolean")
    try:
        datetime.fromisoformat(str(payload["evaluated_at"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceValidationError("validator evaluated_at is not ISO-8601") from error
    stop = payload["stop"]
    if not isinstance(stop, Mapping) or set(stop) != {"type", "args"}:
        raise EvidenceValidationError("validator stop provenance is incomplete")
    if stop["type"] not in {"DONE", "FAIL", "TIMEOUT", "ANS"}:
        raise EvidenceValidationError("validator stop type is not an official Primitive")
    if not isinstance(stop["args"], list) or not all(
        isinstance(value, str) and len(value) <= 256 and "\n" not in value
        for value in stop["args"]
    ):
        raise EvidenceValidationError("validator stop args are invalid")
    if expected_stop is not None and (
        stop["type"] != expected_stop["stop_type"]
        or stop["args"] != expected_stop["stop_args"]
    ):
        raise EvidenceValidationError("validator stop differs from the human-confirmed stop")

    evaluator = payload["evaluator"]
    if not isinstance(evaluator, Mapping) or set(evaluator) != {
        "implementation", "scienceboard_commit", "task_canonical_sha256",
        "module", "class", "function", "source_sha256",
    }:
        raise EvidenceValidationError("validator evaluator provenance is incomplete")
    if evaluator["implementation"] != "ScienceBoard official Task.eval":
        raise EvidenceValidationError("validator is not marked as the official ScienceBoard evaluator")
    if evaluator["scienceboard_commit"] != scienceboard_commit:
        raise EvidenceValidationError("validator ScienceBoard commit does not match protocol")
    if evaluator["task_canonical_sha256"] != task_hash:
        raise EvidenceValidationError("validator task hash does not match task.json")
    application = str(task.get("application"))
    expected_evaluator = EVALUATOR_SOURCE_HASHES.get(application)
    if expected_evaluator is None:
        raise EvidenceValidationError("task has no pinned official evaluator source")
    if (
        evaluator["module"] != expected_evaluator["module"]
        or evaluator["source_sha256"] != expected_evaluator["sha256"]
        or evaluator["function"] != "eval"
        or not str(evaluator["class"]).endswith(".VMTask")
    ):
        raise EvidenceValidationError("official evaluator source/signature binding is invalid")

    official = payload["official_evidence"]
    if not isinstance(official, Mapping) or set(official) != {"result", "trajectory"}:
        raise EvidenceValidationError("official evaluator evidence is incomplete")
    result = official["result"]
    trajectory = official["trajectory"]
    if not isinstance(result, Mapping) or set(result) != {"path", "sha256", "value"}:
        raise EvidenceValidationError("official result.out descriptor is invalid")
    if not isinstance(trajectory, Mapping) or set(trajectory) != {
        "path", "sha256", "size_bytes", "origin"
    }:
        raise EvidenceValidationError("official trajectory descriptor is invalid")
    if result["path"] != "official/result.out" or trajectory["path"] != "official/traj.jsonl":
        raise EvidenceValidationError("official evidence paths differ from the normalized contract")
    result_path = (session_dir / str(result["path"])).resolve()
    trajectory_path = (session_dir / str(trajectory["path"])).resolve()
    for evidence_path, label in ((result_path, "result.out"), (trajectory_path, "trajectory")):
        try:
            evidence_path.relative_to(session_dir)
        except ValueError as error:
            raise EvidenceValidationError(f"official {label} escapes the session") from error
        if not evidence_path.is_file():
            raise EvidenceValidationError(f"official {label} is missing")
    value = result_path.read_text(encoding="utf-8")
    if value not in {"0", "1"} or result["value"] != value:
        raise EvidenceValidationError("official result.out value is invalid")
    if sha256(result_path) != result["sha256"]:
        raise EvidenceValidationError("official result.out hash is invalid")
    if sha256(trajectory_path) != trajectory["sha256"]:
        raise EvidenceValidationError("official trajectory hash is invalid")
    if trajectory_path.stat().st_size != trajectory["size_bytes"]:
        raise EvidenceValidationError("official trajectory size is invalid")
    if (value == "1") != payload["passed"]:
        raise EvidenceValidationError("official result.out disagrees with normalized validator outcome")
    return bool(payload["passed"])


def validate_session_evidence(
    session_dir: Path,
    *,
    expected: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate all evidence needed for a passed *or failed* task attempt.

    A validator outcome of ``false`` is valid retained evidence. Missing,
    malformed, unbound, or unhashed files are not.
    """

    session_dir = Path(session_dir).resolve()
    filenames = {
        "trace": "trace.jsonl",
        "task": "task.json",
        "final_screenshot": "final_state.png",
        "validator": "validator_result.json",
        "private_log": "run.log",
        "official_result": "official/result.out",
        "official_trajectory": "official/traj.jsonl",
        "worker_spec": "worker_spec.json",
        "worker_state": "worker_state.json",
    }
    if expected["actor"] == "human":
        filenames["manual_stop"] = ".manual-stop.json"
    paths = {name: session_dir / filename for name, filename in filenames.items()}
    missing = sorted(path.name for path in paths.values() if not path.is_file())
    if missing:
        raise EvidenceValidationError(f"Missing required evidence: {missing}")

    task = _task_spec(manifest, str(expected["task_id"]))
    task_hash = canonical_json_sha256(paths["task"])
    if task_hash != task["canonical_payload_sha256"]:
        raise EvidenceValidationError("task.json canonical hash does not match the pinned task")
    expected_stop = _validate_worker_records(
        paths["worker_spec"],
        paths["worker_state"],
        expected=expected,
        task_hash=task_hash,
        scienceboard_commit=manifest["upstream"]["commit"],
        model=manifest["agent_model"],
    )

    metadata, events, terminal = load_trace(paths["trace"])
    if terminal.get("status") != "complete" or terminal.get("event_count") != len(events):
        raise EvidenceValidationError("trace did not terminate cleanly")
    if float(terminal.get("duration_ms", 0)) <= 0:
        raise EvidenceValidationError("trace must have a positive full-session duration")
    quality = validate_event_stream(events)
    if quality["duplicate_event_id_count"] or quality["non_monotonic_transition_count"]:
        raise EvidenceValidationError("trace contains duplicate IDs or non-monotonic transitions")

    binding = manifest["guest_binding"]
    expected_metadata = {
        "session_id": expected["session_id"],
        "actor_label": expected["actor"],
        "actor_type": expected["actor_type"],
        "source_class": expected["source_class"],
        "task_id": expected["task_id"],
        "protocol_version": manifest["protocol_version"],
        "guest_id": binding["guest_id"],
        "guest_os_id": binding["guest_os_id"],
        "guest_os_version": binding["guest_os_version"],
        "screen_resolution": binding["screen_resolution"],
        "scienceboard_commit": manifest["upstream"]["commit"],
    }
    mismatches = {
        key: {"expected": value, "observed": metadata.get(key)}
        for key, value in expected_metadata.items()
        if metadata.get(key) != value
    }
    if mismatches:
        raise EvidenceValidationError(f"trace provenance mismatch: {mismatches}")

    resolution = binding["screen_resolution"]
    if any(
        event.get("viewport", {}).get("width") != resolution["width"]
        or event.get("viewport", {}).get("height") != resolution["height"]
        for event in events
    ):
        raise EvidenceValidationError("event viewport does not match the measured/pinned guest resolution")
    png = validate_png(
        paths["final_screenshot"], width=int(resolution["width"]), height=int(resolution["height"])
    )
    validator_passed = _validate_validator(
        paths["validator"],
        session_dir=session_dir,
        expected=expected,
        task=task,
        task_hash=task_hash,
        scienceboard_commit=manifest["upstream"]["commit"],
        expected_stop=expected_stop,
    )
    if paths["private_log"].stat().st_size <= 0:
        raise EvidenceValidationError("run.log is empty")

    artifacts = {
        name: artifact_descriptor(path, relative_to=session_dir) for name, path in paths.items()
    }
    return {
        "validator_passed": validator_passed,
        "trace_event_count": len(events),
        "trace_duration_ms": terminal["duration_ms"],
        "trace_quality": quality,
        "trace_empty": len(events) == 0,
        "trace_missing_modalities": _missing_modalities(events),
        "screenshot": png,
        "artifacts": artifacts,
    }


def verify_stored_artifact_hashes(session_dir: Path, run_manifest: Mapping[str, Any]) -> None:
    artifacts = run_manifest.get("artifacts")
    required = {
        "trace", "task", "final_screenshot", "validator", "private_log",
        "official_result", "official_trajectory", "worker_spec", "worker_state",
    }
    allowed = required | {"manual_stop"}
    if (
        not isinstance(artifacts, Mapping)
        or not required.issubset(artifacts)
        or not set(artifacts).issubset(allowed)
    ):
        raise EvidenceValidationError("run_manifest artifact descriptors are incomplete")
    root = Path(session_dir).resolve()
    for name, descriptor in artifacts.items():
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256", "size_bytes"}:
            raise EvidenceValidationError(f"artifact descriptor is invalid: {name}")
        path = (root / str(descriptor["path"])).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise EvidenceValidationError(f"artifact escapes session directory: {name}") from error
        if not path.is_file():
            raise EvidenceValidationError(f"stored artifact is missing: {name}")
        if sha256(path) != descriptor["sha256"] or path.stat().st_size != descriptor["size_bytes"]:
            raise EvidenceValidationError(f"stored artifact changed after manifesting: {name}")
