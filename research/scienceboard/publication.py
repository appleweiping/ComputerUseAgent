"""Publish a privacy-reduced ScienceBoard matrix summary from private evidence.

The private batch (under ``CUA_PRIVATE_ROOT``) is fully revalidated before
publication. Raw traces, logs, screenshots, guest paths, and action sequences
never enter the public summary.

A session whose recorder finalized cleanly with zero events is complete,
retained evidence (``trace_empty``): the evidence contract counts it, so the
publication contract accepts ``trace_event_count == 0`` as well. Only a
non-positive session duration or a malformed count is rejected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from computer_use_agent.study.policy import (
    FormalStudyError,
    require_human_research_use,
    validate_ledger_binding_snapshot,
)
from computer_use_agent.study.ledger import LedgerError, atomic_write_json, validate_ledger

from .orchestrate import (
    ROOT,
    _private_output,
    build_schedule,
    collect_ledger_sessions,
    load_manifest,
    sha256,
)
from .preflight import DEFAULT_MANIFEST


PUBLIC_SCHEMA = "scienceboard-public-summary-v1"
PUBLIC_FIELDS = {
    "schema_version",
    "planned_sessions",
    "artifact_bundle_complete_sessions",
    "formal_matrix_evidence_complete",
    "by_actor",
    "task_outcomes",
    "session_descriptors",
    "study_ledger",
    "source_provenance",
    "source_batch_manifest_sha256",
    "human_research_attestation",
    "human_raw_traces_published",
    "claim_class",
}
SESSION_FIELDS = {
    "session_id",
    "actor",
    "task_id",
    "repetition",
    "status",
    "validator_passed",
    "trace_event_count",
    "trace_duration_ms",
    "bundle_root_sha256",
}
PROVENANCE_FIELDS = {
    "orchestrator_repository_commit",
    "scienceboard_commit",
    "driver_config_sha256",
    "guest_capability_report_sha256",
    "task_matrix_sha256",
    "task_matrix_canonical_sha256",
    "vm_zip_sha256",
    "snapshot",
    "agent_model",
    "selection_status",
}


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read JSON object {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"Expected one JSON object in {path}")
    return payload


def _hex_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def session_bundle_root_sha256(
    result: Mapping[str, Any],
    run_manifest_path: Path,
) -> str:
    """Hash the complete revalidated artifact set and its immutable run record."""

    run_path = Path(run_manifest_path).resolve(strict=True)
    bundle_payload = {
        "artifacts": result["artifacts"],
        "run_manifest": {
            "sha256": sha256(run_path),
            "size_bytes": run_path.stat().st_size,
        },
    }
    return hashlib.sha256(
        json.dumps(
            bundle_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def validate_public_summary(
    payload: Mapping[str, Any],
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> dict[str, int]:
    """Validate a closed 27-session, ledger-bound publication summary."""

    if set(payload) != PUBLIC_FIELDS or payload.get("schema_version") != PUBLIC_SCHEMA:
        raise ValueError("ScienceBoard public summary does not match the closed schema")
    manifest = load_manifest(manifest_path)
    schedule = build_schedule(manifest)
    expected_ids = [cell["session_id"] for cell in schedule]
    if payload.get("planned_sessions") != 27:
        raise ValueError("ScienceBoard public summary planned_sessions must equal 27")
    if (
        payload.get("artifact_bundle_complete_sessions") != 27
        or payload.get("formal_matrix_evidence_complete") is not True
    ):
        raise ValueError("ScienceBoard public summary is not a complete 27-bundle matrix")
    if payload.get("human_raw_traces_published") is not False:
        raise ValueError("ScienceBoard public summary must exclude raw human traces")
    if payload.get("claim_class") != "single_participant_pipeline_probe":
        raise ValueError("ScienceBoard public summary has an invalid claim class")

    ledger = payload.get("study_ledger")
    if not isinstance(ledger, dict):
        raise ValueError("ScienceBoard public summary has no immutable study ledger")
    try:
        validate_ledger(ledger, ledger)
    except LedgerError as error:
        raise ValueError(f"ScienceBoard study ledger is invalid: {error}") from error
    if ledger.get("stage") != "scienceboard":
        raise ValueError("ScienceBoard study ledger has the wrong stage")
    if ledger.get("planned_count") != 27 or ledger.get("planned_ids") != expected_ids:
        raise ValueError("ScienceBoard study ledger differs from the canonical 27-cell schedule")
    if ledger.get("protocol_sha256") != sha256(manifest_path):
        raise ValueError("ScienceBoard study ledger protocol hash differs from the task manifest")
    binding_errors = validate_ledger_binding_snapshot(
        ledger.get("binding"), require_sealed=True
    )
    if binding_errors:
        raise ValueError("ScienceBoard ledger binding is invalid: " + "; ".join(binding_errors))
    binding = ledger["binding"]
    try:
        require_human_research_use(
            binding["formal_config_snapshot"],
            use="scienceboard_formal",
            participant_scope="single_self_participant",
            verify_private_record=False,
        )
    except FormalStudyError as error:
        raise ValueError(f"ScienceBoard human evidence is not authorized: {error}") from error

    descriptors = payload.get("session_descriptors")
    if not isinstance(descriptors, list) or len(descriptors) != 27:
        raise ValueError("ScienceBoard public summary must contain 27 session descriptors")
    by_schedule = {cell["session_id"]: cell for cell in schedule}
    observed_ids: list[str] = []
    actor_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or set(descriptor) != SESSION_FIELDS:
            raise ValueError("ScienceBoard session descriptor does not match the closed schema")
        session_id = descriptor.get("session_id")
        if not isinstance(session_id, str) or session_id not in by_schedule:
            raise ValueError("ScienceBoard session descriptor has an unknown session_id")
        expected = by_schedule[session_id]
        exact = {
            "actor": expected["actor"],
            "task_id": expected["task_id"],
            "repetition": expected["repetition"],
        }
        if any(descriptor.get(key) != value for key, value in exact.items()):
            raise ValueError("ScienceBoard session descriptor identity differs from the schedule")
        status = descriptor.get("status")
        validator_passed = descriptor.get("validator_passed")
        if status not in {"task_passed", "task_failed"} or type(validator_passed) is not bool:
            raise ValueError("ScienceBoard descriptor outcome is invalid")
        if (status == "task_passed") != validator_passed:
            raise ValueError("ScienceBoard descriptor status and validator outcome disagree")
        event_count = descriptor.get("trace_event_count")
        duration = descriptor.get("trace_duration_ms")
        # Zero events is a valid, complete outcome (trace_empty) and must stay
        # in the published denominator; negative counts are malformed.
        if (
            not isinstance(event_count, int)
            or isinstance(event_count, bool)
            or event_count < 0
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or float(duration) <= 0
        ):
            raise ValueError("ScienceBoard descriptor has invalid trace completeness metrics")
        if not _hex_sha256(descriptor.get("bundle_root_sha256")):
            raise ValueError("ScienceBoard descriptor bundle root is not SHA-256")
        observed_ids.append(session_id)
        actor_counts[str(descriptor["actor"])] += 1
        outcome_counts["validator_passed" if validator_passed else "validator_failed"] += 1
    if observed_ids != sorted(expected_ids):
        raise ValueError("ScienceBoard descriptors must be unique and sorted by session_id")
    expected_actor_counts = {"aio": 9, "human": 9, "seeact": 9}
    if dict(sorted(actor_counts.items())) != expected_actor_counts:
        raise ValueError("ScienceBoard public descriptors do not contain 9 sessions per actor")
    if payload.get("by_actor") != expected_actor_counts:
        raise ValueError("ScienceBoard by_actor counts differ from the descriptors")
    expected_outcomes = {
        "validator_failed": outcome_counts["validator_failed"],
        "validator_passed": outcome_counts["validator_passed"],
    }
    if payload.get("task_outcomes") != expected_outcomes or sum(expected_outcomes.values()) != 27:
        raise ValueError("ScienceBoard task outcome counts differ from the descriptors")

    provenance = payload.get("source_provenance")
    if not isinstance(provenance, dict) or set(provenance) != PROVENANCE_FIELDS:
        raise ValueError("ScienceBoard public source provenance is incomplete")
    for field in (
        "orchestrator_repository_commit",
        "scienceboard_commit",
        "driver_config_sha256",
        "guest_capability_report_sha256",
        "task_matrix_sha256",
        "task_matrix_canonical_sha256",
        "vm_zip_sha256",
    ):
        value = provenance.get(field)
        expected_length = 40 if field.endswith("commit") else 64
        if not isinstance(value, str) or len(value) != expected_length or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError(f"ScienceBoard public provenance {field} is invalid")
    if provenance.get("snapshot") != manifest["snapshot"]:
        raise ValueError("ScienceBoard public provenance snapshot is invalid")
    if provenance.get("agent_model") != manifest["agent_model"]:
        raise ValueError("ScienceBoard public provenance model is invalid")
    if provenance.get("selection_status") != manifest["selection_status"]:
        raise ValueError("ScienceBoard public provenance task-selection status is invalid")
    if not _hex_sha256(payload.get("source_batch_manifest_sha256")):
        raise ValueError("ScienceBoard source batch manifest hash is invalid")
    policy = binding["formal_config_snapshot"]["human_research_policy"]
    expected_attestation = {
        "determination_scope": policy["determination_scope"],
        "determination_record_sha256": policy["determination_record_sha256"],
        "verification_method": "runtime_sha256_match_before_publication",
        "private_record_published": False,
    }
    if payload.get("human_research_attestation") != expected_attestation:
        raise ValueError("ScienceBoard human-research attestation is missing or inconsistent")
    return expected_actor_counts


def build_public_summary(private_batch: Path) -> dict[str, Any]:
    """Revalidate a private batch and return its path-free public projection."""

    batch_dir = _private_output(private_batch)
    batch_path = batch_dir / "batch_manifest.json"
    batch = _read_object(batch_path)
    manifest = load_manifest(DEFAULT_MANIFEST)
    schedule = build_schedule(manifest)
    ledger = _read_object(batch_dir / "study_ledger.json")
    validate_ledger(ledger, ledger)
    binding_errors = validate_ledger_binding_snapshot(
        ledger.get("binding"), require_sealed=True
    )
    if binding_errors:
        raise ValueError("ScienceBoard ledger binding is invalid: " + "; ".join(binding_errors))
    require_human_research_use(
        ledger["binding"]["formal_config_snapshot"],
        use="scienceboard_formal",
        participant_scope="single_self_participant",
        verify_private_record=True,
    )
    provenance = batch.get("source_provenance")
    if not isinstance(provenance, dict):
        raise ValueError("Private ScienceBoard batch has no formal source provenance")
    results, missing, unknown = collect_ledger_sessions(
        batch_dir,
        schedule=schedule,
        manifest=manifest,
        expected_provenance=provenance,
    )
    if missing or unknown or len(results) != 27 or any(
        result.get("evidence_valid") is not True for result in results
    ):
        raise ValueError("Private ScienceBoard batch is not a complete revalidated 27-session matrix")

    descriptors: list[dict[str, Any]] = []
    for result in sorted(results, key=lambda item: str(item["session_id"])):
        run_path = batch_dir / str(result["session_id"]) / "run_manifest.json"
        bundle_root = session_bundle_root_sha256(result, run_path)
        descriptors.append(
            {
                "session_id": result["session_id"],
                "actor": result["actor"],
                "task_id": result["task_id"],
                "repetition": result["repetition"],
                "status": result["status"],
                "validator_passed": result["validator_passed"],
                "trace_event_count": result["trace_event_count"],
                "trace_duration_ms": result["trace_duration_ms"],
                "bundle_root_sha256": bundle_root,
            }
        )
    by_actor = dict(sorted(Counter(item["actor"] for item in descriptors).items()))
    outcomes = Counter(
        "validator_passed" if item["validator_passed"] else "validator_failed"
        for item in descriptors
    )
    safe_provenance = {
        field: provenance[field] for field in sorted(PROVENANCE_FIELDS)
    }
    public = {
        "schema_version": PUBLIC_SCHEMA,
        "planned_sessions": 27,
        "artifact_bundle_complete_sessions": 27,
        "formal_matrix_evidence_complete": True,
        "by_actor": by_actor,
        "task_outcomes": {
            "validator_failed": outcomes["validator_failed"],
            "validator_passed": outcomes["validator_passed"],
        },
        "session_descriptors": descriptors,
        "study_ledger": ledger,
        "source_provenance": safe_provenance,
        "source_batch_manifest_sha256": sha256(batch_path),
        "human_research_attestation": {
            "determination_scope": ledger["binding"]["formal_config_snapshot"][
                "human_research_policy"
            ]["determination_scope"],
            "determination_record_sha256": ledger["binding"]["formal_config_snapshot"][
                "human_research_policy"
            ]["determination_record_sha256"],
            "verification_method": "runtime_sha256_match_before_publication",
            "private_record_published": False,
        },
        "human_raw_traces_published": False,
        "claim_class": "single_participant_pipeline_probe",
    }
    validate_public_summary(public)
    return public


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "research/evidence/scienceboard_formal/summary.json",
    )
    args = parser.parse_args(argv)
    output = args.output.resolve()
    expected = (ROOT / "research/evidence/scienceboard_formal/summary.json").resolve()
    if output != expected:
        parser.error(
            "public ScienceBoard evidence must use research/evidence/scienceboard_formal/summary.json"
        )
    if output.exists():
        parser.error("refusing to overwrite an existing public ScienceBoard summary")
    try:
        payload = build_public_summary(args.input_dir)
        atomic_write_json(output, payload, replace=False)
    except (FormalStudyError, LedgerError, OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"output": output.relative_to(ROOT).as_posix(), "sessions": 27}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
