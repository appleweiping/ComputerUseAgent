"""Strict public-evidence validation for the complete Week 1 formal matrix."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from computer_use_agent.study.policy import validate_ledger_binding_snapshot
from computer_use_agent.study.ledger import LedgerError, validate_ledger

from .protocol import aggregate_records, build_matrix, load_manifest


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_MANIFEST = ROOT / "research/week1_miniwob/benchmark_manifest.json"


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is unreadable JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} root is not an object")
    return payload


def validate_formal_evidence(
    formal_dir: Path,
    *,
    aggregate_fn: Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]] = aggregate_records,
) -> dict[str, Any]:
    """Recompute all 27 records and verify their immutable sealed run ledger."""

    formal_dir = Path(formal_dir)
    manifest = load_manifest(CANONICAL_MANIFEST)
    matrix = build_matrix(manifest)
    summary_path = formal_dir / "summary.json"
    attempt_files = sorted((formal_dir / "attempts").glob("*.json"))
    if not summary_path.is_file() or len(attempt_files) != 27 or len(matrix) != 27:
        raise ValueError("formal directory does not contain the canonical complete 27-attempt set")
    planned_ids = [str(item["attempt_id"]) for item in matrix]
    ledger = _read_object(formal_dir / "study_ledger.json", label="study ledger")
    try:
        validate_ledger(ledger, ledger)
    except LedgerError as error:
        raise ValueError(f"study ledger is invalid: {error}") from error
    if ledger.get("stage") != "week1":
        raise ValueError("study ledger stage is not week1")
    if ledger.get("planned_count") != 27 or ledger.get("planned_ids") != planned_ids:
        raise ValueError("study ledger matrix differs from the canonical Week 1 matrix")
    expected_protocol = hashlib.sha256(CANONICAL_MANIFEST.read_bytes()).hexdigest()
    if ledger.get("protocol_sha256") != expected_protocol:
        raise ValueError("study ledger protocol hash differs from the canonical manifest")
    binding_errors = validate_ledger_binding_snapshot(
        ledger.get("binding"), require_sealed=True
    )
    if binding_errors:
        raise ValueError("study ledger binding is invalid: " + "; ".join(binding_errors))

    records: list[dict[str, Any]] = []
    for path in attempt_files:
        record = _read_object(path, label=f"attempt {path.name}")
        attempt = record.get("attempt")
        attempt_id = attempt.get("attempt_id") if isinstance(attempt, dict) else None
        if not isinstance(attempt_id, str) or path.name != f"{attempt_id}.json":
            raise ValueError(f"attempt filename/payload identity mismatch: {path.name}")
        records.append(record)
    computed = aggregate_fn(records, matrix)
    required_true = (
        "matrix_artifacts_complete",
        "record_contracts_valid",
        "source_provenance_consistent",
        "selected_artifact_bundles_complete",
        "selected_formal_matrix_eligible",
        "all_27_artifact_bundles_complete",
        "all_27_formal_matrix_eligible",
    )
    if (
        computed.get("planned_attempts") != 27
        or computed.get("recorded_attempt_artifacts") != 27
        or any(computed.get(field) is not True for field in required_true)
    ):
        failed = [field for field in required_true if computed.get(field) is not True]
        raise ValueError(
            "canonical protocol aggregate failed: "
            + (", ".join(failed) if failed else "attempt counts")
        )
    published = _read_object(summary_path, label="formal summary")
    compared_fields = (
        "schema_version",
        "planned_attempts",
        "recorded_attempt_artifacts",
        "record_contracts_valid",
        "source_provenance",
        "source_provenance_consistent",
        "provider_entitlement",
        "provider_entitlement_consistent",
        "artifact_bundle_complete_attempts",
        "formal_matrix_eligible_attempts",
        "analysis_eligible_attempts",
        *required_true,
        "status_counts",
        "missing_attempt_ids",
        "duplicate_attempt_ids",
        "unknown_attempt_ids",
        "malformed_record_ids",
        "attempt_payload_mismatch_ids",
        "manifest_hash_mismatch_ids",
        "evidence_scope_mismatch_ids",
        "protocol_invalid_attempt_ids",
        "source_provenance_invalid_attempt_ids",
        "adapter_source_invalid_attempt_ids",
        "adapter_argv_invalid_attempt_ids",
        "provider_entitlement_invalid_attempt_ids",
    )
    mismatches = [field for field in compared_fields if published.get(field) != computed.get(field)]
    if mismatches:
        raise ValueError(
            "published summary differs from recomputed canonical aggregate: "
            + ", ".join(mismatches)
        )
    return computed
