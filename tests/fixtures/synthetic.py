"""Deterministic synthetic fixtures shared by the product tests.

Every value here is synthetic: there are no participants, no real traces
and no private paths. ``make_records`` builds detector records that exercise
the pipeline; ``synthetic_browser_trace`` builds schema-valid ``cui-trace-v2``
browser exports from the checked-in minimal example.

``examples/detector/synthetic-dataset.json`` is generated from
``synthetic_dataset_payload()``; ``tests/detector/test_examples.py`` keeps the
two byte-identical. Regenerate it with::

    python -m tests.fixtures.synthetic examples/detector/synthetic-dataset.json
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from computer_use_agent.detector.contract import (
    DATASET_SCHEMA,
    FEATURE_ALLOWLIST,
    RECORD_SCHEMA,
    canonical_json,
    validate_dataset,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_TRACE_V2 = REPO_ROOT / "examples" / "traces" / "minimal-session-v2.json"
EXAMPLE_DATASET = REPO_ROOT / "examples" / "detector" / "synthetic-dataset.json"
UNIT_FEATURES = frozenset(
    {"pointer_straightness", "pointer_turning_entropy", "correction_ratio", "event_type_entropy"}
)


def _feature_value(
    feature: str, feature_index: int, *, task_index: int, repetition: int, agent: bool
) -> float:
    # Class signal is intentionally simple because these rows test the
    # pipeline, not a scientific hypothesis. Task and repetition offsets keep
    # the matrix non-degenerate.
    direction = 1.0 if agent else -1.0
    if feature.startswith("missing_"):
        return float((task_index + repetition + int(agent)) % 2)
    if feature in UNIT_FEATURES:
        return (
            0.5
            + direction * (0.12 + (feature_index % 2) * 0.03)
            + task_index * 0.015
            + repetition * 0.005
        )
    if feature == "inter_event_burstiness":
        return direction * 0.28 + task_index * 0.025 + repetition * 0.01
    return (
        10.0
        + feature_index * 0.7
        + direction * (1.2 + (feature_index % 4) * 0.25)
        + task_index * (0.11 + (feature_index % 3) * 0.02)
        + repetition * 0.035
    )


def make_records(*, task_count: int = 3, repetitions: int = 3) -> list[dict[str, Any]]:
    """Return ``task_count * 2 * repetitions`` synthetic ``cui-detector-record-v2`` rows."""

    records: list[dict[str, Any]] = []
    for task_index in range(task_count):
        task = f"synthetic-task-{task_index + 1}"
        for label in ("human", "agent"):
            for repetition in range(repetitions):
                token = f"{task}|{label}|{repetition}"
                features = {
                    feature: _feature_value(
                        feature,
                        feature_index,
                        task_index=task_index,
                        repetition=repetition,
                        agent=label == "agent",
                    )
                    for feature_index, feature in enumerate(FEATURE_ALLOWLIST)
                }
                records.append(
                    {
                        "schema_version": RECORD_SCHEMA,
                        "record_ref": hashlib.sha256(f"record|{token}".encode("ascii")).hexdigest()[
                            :16
                        ],
                        "source_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
                        "label": label,
                        "task_group": task,
                        "agent_family": "synthetic-agent" if label == "agent" else None,
                        "evidence_class": "synthetic_test_fixture",
                        "participant_scope": "synthetic_no_participant",
                        "features": features,
                        "quality": {"fixture": True},
                    }
                )
    return records


def synthetic_dataset_payload(*, task_count: int = 3, repetitions: int = 3) -> dict[str, Any]:
    """A complete, audited ``cui-detector-dataset-v2`` object."""

    records = make_records(task_count=task_count, repetitions=repetitions)
    return {
        "schema_version": DATASET_SCHEMA,
        "records": records,
        "audit": validate_dataset(records),
    }


def write_synthetic_dataset(path: Path, **kwargs: int) -> Path:
    """Write the canonical JSON bytes of :func:`synthetic_dataset_payload`."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(synthetic_dataset_payload(**kwargs)))
    return path


def synthetic_browser_trace(*, task_id: str, actor_type: str, index: int) -> dict[str, Any]:
    """A schema-valid ``cui-trace-v2`` browser export with unique bytes per ``index``."""

    trace: dict[str, Any] = copy.deepcopy(json.loads(EXAMPLE_TRACE_V2.read_text(encoding="utf-8")))
    metadata = trace["metadata"]
    actor = "human-run" if actor_type == "human" else "agent-run"
    metadata.update(
        {
            "session_id": f"synthetic-session-{index:03d}",
            "actor_type": actor_type,
            "actor_label": f"{actor}-{index:03d}",
            "participant_code": f"{actor}-{index:03d}",
            "task_id": task_id,
            "source_class": "human_self" if actor_type == "human" else "cui_agent",
            "executor": "human" if actor_type == "human" else "synthetic-agent",
        }
    )
    event = trace["events"][0]
    event["event_id"] = f"synthetic-event-{index:03d}"
    event["monotonic_ms"] = 10 + index
    event["t_document_ms"] = 10 + index
    return trace


def late_start_browser_trace() -> dict[str, Any]:
    """A ``cui-trace-v2`` browser export whose first event comes after t=0.

    Like a real extension export, ``summary.duration_ms`` is the
    first-to-last event span (6078.002 ms) while the final event sits at
    ``t_session_ms == 6085.002`` on the session clock.
    """

    trace: dict[str, Any] = copy.deepcopy(json.loads(EXAMPLE_TRACE_V2.read_text(encoding="utf-8")))
    first = trace["events"][0]
    first.update({"t_session_ms": 7, "epoch_ms": 1786147200007})
    second = copy.deepcopy(first)
    second.update(
        {
            "event_id": "example-event-v2-002",
            "event_seq": 2,
            "document_epoch": 1,
            "t_session_ms": 6085.002,
            "t_document_ms": 5,
            "monotonic_ms": 5,
            "epoch_ms": 1786147206085,
        }
    )
    trace["events"].append(second)
    trace["metadata"].update(
        {
            "event_count": 2,
            "event_seq_last": 2,
            "document_epoch_count": 2,
            "stopped_at": "2026-08-08T00:00:07Z",
        }
    )
    trace["summary"].update(
        {"event_count": 2, "duration_ms": 6078.002, "by_type": {"navigation": 2}}
    )
    return trace


def write_trace_manifest(
    root: Path, *, task_count: int = 3, repetitions: int = 3
) -> tuple[Path, list[Path]]:
    """Write synthetic traces plus a ``cui-detector-extraction-manifest-v1`` under ``root``."""

    root = Path(root)
    traces_dir = root / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    paths: list[Path] = []
    index = 0
    for task_index in range(task_count):
        task_id = f"synthetic-task-{task_index + 1}"
        for label in ("human", "agent"):
            for _ in range(repetitions):
                index += 1
                path = traces_dir / f"trace-{index:03d}.json"
                trace = synthetic_browser_trace(task_id=task_id, actor_type=label, index=index)
                path.write_text(json.dumps(trace, indent=2) + "\n", encoding="utf-8")
                paths.append(path)
                entries.append(
                    {
                        "trace": f"traces/{path.name}",
                        "label": label,
                        "task_group": task_id,
                        "agent_family": "synthetic-agent" if label == "agent" else None,
                        "evidence_class": "synthetic_test_fixture",
                        "participant_scope": "synthetic_no_participant",
                    }
                )
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"schema_version": "cui-detector-extraction-manifest-v1", "traces": entries},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest, paths


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    target = Path(arguments[0]) if arguments else EXAMPLE_DATASET
    write_synthetic_dataset(target)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def synthetic_x11_metadata(
    *, actor: str = "aio", session_id: str = "synthetic-x11-001"
) -> dict[str, Any]:
    """Session metadata accepted by the X11 ``cui-trace-v2`` runtime contract."""

    human = actor == "human"
    return {
        "schema_version": "cui-trace-v2",
        "session_id": session_id,
        "actor_type": "human" if human else "agent",
        "actor_label": actor,
        "participant_code": "self-human-001" if human else None,
        "pair_id": None,
        "task_id": "synthetic-desktop-task",
        "protocol_version": "synthetic-protocol-v1",
        "executor": "manual" if human else "scienceboard-agent",
        "executor_version": "synthetic-executor-1.0",
        "recorder_version": "x11-v2",
        "capture_adapter": "x11_vm",
        "environment_scope": "scienceboard_vm_x11",
        "source_class": "self_human" if human else "reasoning_agent",
        "environment": "Linux_synthetic_X11",
        "guest_id": "synthetic-guest",
        "guest_os_id": "ubuntu",
        "guest_os_version": "22.04",
        "screen_resolution": {"width": 1920, "height": 1080},
        "scienceboard_commit": "0" * 40,
        "raw_key_capture": False,
        "include_query": False,
        "consent_confirmed": human,
        "controller_confirmed": not human,
        "started_at": "2026-08-08T00:00:00+00:00",
        "status": "recording",
        "privacy_policy": "x11-metadata-only-v1",
        "monotonic_origin_ms": 0.0,
        "timing_model": "session-global-monotonic-v1",
        "document_epoch_count": 1,
    }


def write_synthetic_x11_trace(path: Path, *, events: int = 3) -> Path:
    """Write a complete append-only X11 JSONL trace with ``events`` pointer moves."""

    from computer_use_agent.trace.io import TraceWriter

    writer = TraceWriter(Path(path), synthetic_x11_metadata())
    for index in range(1, events + 1):
        writer.append(
            {
                # The writer assigns the V2 ordering envelope (event_seq,
                # document_epoch, session/document clocks) to a V1-shaped event.
                "schema_version": "cui-trace-v1",
                "event_id": f"synthetic-x11-event-{index:02d}",
                "type": "pointermove",
                "epoch_ms": 1_000.0 + index,
                "monotonic_ms": float(index),
                "viewport": {"width": 1920, "height": 1080, "device_pixel_ratio": 1},
                "target": {"role": "desktop_application", "application_id": "unknown"},
                "key_class": None,
                "keyboard": None,
                "pointer": {
                    "x": float(10 * index),
                    "y": float(5 * index),
                    "button": -1,
                    "pointer_type": "mouse",
                },
                "wheel": None,
            }
        )
    writer.close(status="complete", stopped_at="2026-08-08T00:00:01+00:00")
    return Path(path)
