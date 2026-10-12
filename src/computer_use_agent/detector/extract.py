"""Deterministically extract path-free detector records from validated traces."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from computer_use_agent.features import extract_features
from computer_use_agent.study.policy import HumanDataPolicy
from computer_use_agent.trace.io import load_trace

from .contract import (
    DATASET_SCHEMA,
    RECORD_SCHEMA,
    canonical_json,
    validate_dataset,
    validate_human_data_use,
    validate_record,
)

MANIFEST_SCHEMA = "cui-detector-extraction-manifest-v1"
MANIFEST_FIELDS = frozenset(
    {
        "trace",
        "label",
        "task_group",
        "agent_family",
        "evidence_class",
        "participant_scope",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def trace_to_record(
    path: Path,
    *,
    label: str,
    task_group: str,
    evidence_class: str,
    participant_scope: str,
    agent_family: str | None = None,
    human_research_config: Mapping[str, Any] | None = None,
    human_data_policy: HumanDataPolicy | None = None,
) -> dict[str, Any]:
    """Extract one reviewed feature vector without retaining a path or UUID.

    Formal human traces need an authorizing ``human_data_policy`` (or
    ``human_research_config``); by default they are denied.
    """

    path = Path(path).resolve(strict=True)
    source_sha256 = _sha256(path)
    metadata, events, terminal = load_trace(path)
    metadata_label = metadata.get("actor_type")
    if metadata_label in {"human", "agent"} and metadata_label != label:
        raise ValueError("Manifest label disagrees with validated trace metadata")
    metadata_task = metadata.get("task_id")
    if isinstance(metadata_task, str) and metadata_task and metadata_task != task_group:
        raise ValueError("Manifest task_group disagrees with validated trace metadata")
    extraction = extract_features(
        events,
        trace_origin_ms=float(metadata.get("monotonic_origin_ms", 0.0)),
        session_duration_ms=(
            float(terminal["duration_ms"]) if terminal.get("duration_ms") is not None else None
        ),
    )
    record = {
        "schema_version": RECORD_SCHEMA,
        "record_ref": hashlib.sha256(
            f"{RECORD_SCHEMA}\0{source_sha256}".encode("ascii")
        ).hexdigest()[:16],
        "source_sha256": source_sha256,
        "label": label,
        "task_group": task_group,
        "agent_family": agent_family,
        "evidence_class": evidence_class,
        "participant_scope": participant_scope,
        "features": extraction["features"],
        "quality": extraction["quality"],
    }
    validate_record(record)
    validate_human_data_use(
        [record],
        human_research_config=human_research_config,
        human_data_policy=human_data_policy,
    )
    return record


def extract_manifest(path: Path) -> dict[str, Any]:
    """Resolve traces relative to a manifest, then emit a location-free dataset."""

    manifest_path = Path(path).resolve(strict=True)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or set(payload) != {"schema_version", "traces"}:
        raise ValueError("Extraction manifest must contain schema_version and traces")
    if payload["schema_version"] != MANIFEST_SCHEMA:
        raise ValueError("Unsupported extraction manifest schema")
    if not isinstance(payload["traces"], list) or not payload["traces"]:
        raise ValueError("Extraction manifest has no traces")
    records = []
    for index, item in enumerate(payload["traces"]):
        if not isinstance(item, Mapping) or set(item) != MANIFEST_FIELDS:
            raise ValueError(f"Trace entry {index} does not match the closed manifest schema")
        trace_value = item["trace"]
        if not isinstance(trace_value, str) or not trace_value:
            raise ValueError(f"Trace entry {index} has no path")
        if item["evidence_class"] == "formal_trace":
            raise ValueError(
                "Formal traces require a stage-specific immutable-ledger bridge; "
                "the generic extraction manifest is not formal provenance"
            )
        trace_path = Path(trace_value)
        if not trace_path.is_absolute():
            trace_path = manifest_path.parent / trace_path
        records.append(
            trace_to_record(
                trace_path,
                label=str(item["label"]),
                task_group=str(item["task_group"]),
                agent_family=(
                    str(item["agent_family"]) if item["agent_family"] is not None else None
                ),
                evidence_class=str(item["evidence_class"]),
                participant_scope=str(item["participant_scope"]),
            )
        )
    audit = validate_dataset(records)
    return {
        "schema_version": DATASET_SCHEMA,
        "records": records,
        "audit": audit,
    }


def write_dataset(dataset: Mapping[str, Any], output: Path) -> None:
    """Atomically publish a dataset: a same-directory temporary file is renamed.

    A reader never observes a partially written dataset, and a failed run
    leaves any previous file untouched.
    """

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(dict(dataset))
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        dataset = extract_manifest(args.manifest)
        write_dataset(dataset, args.output)
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(dataset["audit"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
