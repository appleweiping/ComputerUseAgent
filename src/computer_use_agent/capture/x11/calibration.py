"""Evaluate X11/XTest and direct-human recorder calibration evidence.

This command does not inject input or pretend that calibration happened. Run
the guest recorder twice (one deterministic XTest probe and one short
self-human probe, both driven by ``calibration-probe``), then supply their
paths and the observed post-stop counts in a private evidence JSON. A passing
certificate is a prerequisite for the formal ScienceBoard orchestrator.

Per phase it checks: X11 capture adapter, a complete terminal record, a
positive session duration, the five required input types (keydown, keyup,
pointermove, pointerdown, pointerup), unique event IDs, monotonic timestamps,
the post-stop negative control (event count unchanged after stop), the
calibration task/protocol binding, the actor binding and the privacy field
scan. Across phases it requires distinct traces, hashes and session IDs and an
identical guest binding. Focus changes are not part of the certificate.

    python -m computer_use_agent.capture.x11 calibrate --dry-run
    python -m computer_use_agent.capture.x11 calibrate --evidence private/evidence.json         --output private/certificate.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...trace.io import load_trace, validate_event_stream
from ...trace.privacy import assert_privacy_safe_event

REQUIRED_INPUT_TYPES = frozenset({"keydown", "keyup", "pointermove", "pointerdown", "pointerup"})
CALIBRATION_TASK_ID = "scienceboard-x11-calibration-v1"
CALIBRATION_PROTOCOL = "scienceboard-x11-calibration-v1"
BINDING_FIELDS = (
    "guest_id",
    "guest_os_id",
    "guest_os_version",
    "screen_resolution",
    "scienceboard_commit",
    "capture_adapter",
    "recorder_version",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def evaluate_phase(name: str, specification: dict[str, Any], base: Path) -> dict[str, Any]:
    trace_path = (base / specification["trace"]).resolve()
    metadata, events, terminal = load_trace(trace_path)
    for event in events:
        assert_privacy_safe_event(event)
    quality = validate_event_stream(events)
    observed_types = {str(event.get("type")) for event in events}
    missing_types = sorted(REQUIRED_INPUT_TYPES - observed_types)
    at_stop = specification.get("event_count_at_stop")
    after_probe = specification.get("event_count_after_post_stop_probe")
    checks = {
        "capture_adapter_x11": metadata.get("capture_adapter") == "x11_vm",
        "terminal_complete": terminal.get("status") == "complete",
        "positive_session_duration": float(terminal.get("duration_ms", 0)) > 0,
        "required_input_types": not missing_types,
        "unique_event_ids": quality["duplicate_event_id_count"] == 0,
        "monotonic_timestamps": quality["non_monotonic_transition_count"] == 0,
        "post_stop_negative_control": (
            type(at_stop) is int
            and type(after_probe) is int
            and at_stop == after_probe == len(events)
        ),
        "calibration_task_bound": metadata.get("task_id") == CALIBRATION_TASK_ID,
        "calibration_protocol_bound": metadata.get("protocol_version") == CALIBRATION_PROTOCOL,
        "actor_bound": (
            (
                name == "xtest"
                and metadata.get("actor_label") == "xtest"
                and metadata.get("source_class") == "scripted_automation_control"
                and metadata.get("executor") == "xtest"
            )
            or (
                name == "self_human"
                and metadata.get("actor_label") == "human"
                and metadata.get("source_class") == "self_human"
                and metadata.get("executor") == "manual"
            )
        ),
    }
    return {
        "name": name,
        "passed": all(checks.values()),
        "trace_sha256": sha256(trace_path),
        "trace_path": str(trace_path),
        "session_id": metadata.get("session_id"),
        "binding": {field: metadata.get(field) for field in BINDING_FIELDS},
        "event_count": len(events),
        "observed_event_types": sorted(observed_types),
        "missing_required_event_types": missing_types,
        "quality": quality,
        "checks": checks,
    }


def evaluate_calibration(evidence_path: Path) -> dict[str, Any]:
    evidence_path = evidence_path.resolve()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence.get("schema_version") != "scienceboard-x11-calibration-evidence-v1":
        raise ValueError("Unexpected calibration evidence schema")
    phases = [
        evaluate_phase("xtest", evidence["xtest"], evidence_path.parent),
        evaluate_phase("self_human", evidence["self_human"], evidence_path.parent),
    ]
    distinct_paths = phases[0]["trace_path"] != phases[1]["trace_path"]
    distinct_hashes = phases[0]["trace_sha256"] != phases[1]["trace_sha256"]
    distinct_sessions = phases[0]["session_id"] != phases[1]["session_id"]
    same_binding = phases[0]["binding"] == phases[1]["binding"]
    cross_phase_checks = {
        "distinct_trace_paths": distinct_paths,
        "distinct_trace_hashes": distinct_hashes,
        "distinct_session_ids": distinct_sessions,
        "same_guest_binding": same_binding,
    }
    public_phases = [
        {key: value for key, value in phase.items() if key != "trace_path"} for phase in phases
    ]
    return {
        "schema_version": "scienceboard-x11-calibration-certificate-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capture_adapter": "x11_vm",
        "evidence_sha256": sha256(evidence_path),
        "binding": phases[0]["binding"] if same_binding else None,
        "passed": all(phase["passed"] for phase in phases) and all(cross_phase_checks.values()),
        "phases": public_phases,
        "cross_phase_checks": cross_phase_checks,
        "claim_boundary": (
            "A pass establishes X11/XTest and direct self-human capture, privacy filtering, "
            "and stop behavior on this guest configuration. It is not a ScienceBoard task result."
        ),
    }


def dry_run_payload() -> dict[str, Any]:
    return {
        "schema_version": "scienceboard-x11-calibration-plan-v1",
        "required_phases": ["xtest", "self_human"],
        "required_event_types_per_phase": sorted(REQUIRED_INPUT_TYPES),
        "required_controls": [
            "privacy field scan",
            "unique event IDs",
            "monotonic timestamps",
            "event count unchanged after stop",
        ],
        "evidence_template": {
            "schema_version": "scienceboard-x11-calibration-evidence-v1",
            "xtest": {
                "trace": "private/path/to/distinct-xtest-trace.jsonl",
                "event_count_at_stop": 0,
                "event_count_after_post_stop_probe": 0,
            },
            "self_human": {
                "trace": "private/path/to/distinct-self-human-trace.jsonl",
                "event_count_at_stop": 0,
                "event_count_after_post_stop_probe": 0,
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibrate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(json.dumps(dry_run_payload(), indent=2))
        return 0
    if args.evidence is None:
        print("--evidence is required unless --dry-run is used", file=sys.stderr)
        return 2
    certificate = evaluate_calibration(args.evidence)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(certificate, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(certificate, indent=2))
    return 0 if certificate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
