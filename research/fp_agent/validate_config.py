"""Validate the separate non-formal retrospective policy without running work.

The policy references the historical formal-study config by the identity it had
in the source snapshot (``configs/formal-study.yaml`` at commit 437070f). The
live ``research/configs/formal-study.json`` may be edited (for example to
de-personalize it), so the binding is satisfied when either the live file still
has those exact bytes or the frozen release capsule under ``research/archive``
records the same path and SHA-256 for that snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


RESEARCH_ROOT = Path(__file__).resolve().parents[1]
CONFIG = RESEARCH_ROOT / "configs" / "retrospective-study.json"
FORMAL_CONFIG = RESEARCH_ROOT / "configs" / "formal-study.json"
ARCHIVED_CAPSULE = RESEARCH_ROOT / "archive" / "release-capsule-2026-08.json"
HISTORICAL_FORMAL_PATH = "configs/formal-study.yaml"
SOURCE_COMMIT = "437070f519d4bd40e9e4e46bf19fcca52b4b7557"
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "study_mode",
        "protocol_status",
        "historical_formal_config",
        "formal_claim_permitted",
        "budget",
        "data_policy",
        "citation_snapshot",
        "codex_cases",
        "stagehand_retirement",
    }
)


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read retrospective config {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("retrospective config root must be an object")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_keys(value: Any, expected: set[str] | frozenset[str], label: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label} must be an object"]
    missing = sorted(set(expected) - set(value))
    unexpected = sorted(set(value) - set(expected))
    if missing or unexpected:
        return [f"{label} keys differ; missing={missing}, unexpected={unexpected}"]
    return []


def _archived_formal_record() -> dict[str, Any] | None:
    """The frozen capsule's record of the historical formal config, if present."""

    try:
        capsule = json.loads(ARCHIVED_CAPSULE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(capsule, dict):
        return None
    for value in capsule.values():
        if isinstance(value, dict):
            record = value.get(HISTORICAL_FORMAL_PATH)
            if isinstance(record, dict) and record.get("path") == HISTORICAL_FORMAL_PATH:
                return record
    return None


def historical_formal_binding(expected_hash: str) -> str | None:
    """Return how the historical formal config is bound, or None when it is not."""

    try:
        if _sha256(FORMAL_CONFIG) == expected_hash:
            return "live_config_bytes"
    except OSError:
        pass
    record = _archived_formal_record()
    if record is not None and record.get("sha256") == expected_hash:
        return "archived_capsule_record"
    return None


def _utc(value: Any) -> bool:
    if not isinstance(value, str) or UTC_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def validate(path: Path = CONFIG) -> list[str]:
    errors: list[str] = []
    try:
        resolved = Path(path).resolve(strict=True)
        canonical = CONFIG.resolve(strict=True)
    except OSError as error:
        return [f"retrospective config is unavailable: {error}"]
    if resolved != canonical:
        return ["retrospective execution requires the canonical repository config"]
    try:
        payload = _read(resolved)
    except ValueError as error:
        return [str(error)]
    errors.extend(_exact_keys(payload, ROOT_FIELDS, "root"))
    if payload.get("schema_version") != "cui-retrospective-study-config-v1":
        errors.append("unexpected retrospective schema_version")
    if payload.get("study_mode") != "retrospective_non_interventional":
        errors.append("study_mode must remain retrospective_non_interventional")
    if payload.get("protocol_status") != "active_non_formal":
        errors.append("retrospective protocol status must remain active_non_formal")
    if payload.get("formal_claim_permitted") is not False:
        errors.append("retrospective artifacts can never permit a formal claim")

    historical = payload.get("historical_formal_config")
    errors.extend(
        _exact_keys(
            historical,
            {"repository_path", "source_commit", "sha256", "mutation_allowed"},
            "historical_formal_config",
        )
    )
    if isinstance(historical, dict):
        if historical.get("repository_path") != HISTORICAL_FORMAL_PATH:
            errors.append("historical formal config path changed")
        if historical.get("source_commit") != SOURCE_COMMIT:
            errors.append("historical formal config source commit changed")
        expected_hash = historical.get("sha256")
        if not isinstance(expected_hash, str) or HEX_SHA256.fullmatch(expected_hash) is None:
            errors.append("historical formal config hash is invalid")
        elif historical_formal_binding(expected_hash) is None:
            errors.append("historical formal config bytes changed")
        if historical.get("mutation_allowed") is not False:
            errors.append("retrospective policy must not mutate historical formal semantics")

    budget = payload.get("budget")
    errors.extend(
        _exact_keys(
            budget,
            {"currency", "hard_total_cap", "stop_if_projected_total_exceeds_cap"},
            "budget",
        )
    )
    if isinstance(budget, dict) and budget != {
        "currency": "USD",
        "hard_total_cap": 200.0,
        "stop_if_projected_total_exceeds_cap": True,
    }:
        errors.append("retrospective budget must retain the US$200 hard stop")

    data = payload.get("data_policy")
    errors.extend(
        _exact_keys(
            data,
            {
                "allowed_evidence_classes",
                "allowed_participant_scopes",
                "new_participant_collection_allowed",
                "raw_human_trace_publication_allowed",
                "source_manifest_schema",
                "detector_dataset_schema",
            },
            "data_policy",
        )
    )
    if isinstance(data, dict):
        if data.get("allowed_evidence_classes") != [
            "external_public",
            "external_restricted",
        ]:
            errors.append("retrospective evidence classes changed")
        if data.get("allowed_participant_scopes") != [
            "public_dataset",
            "third_party_existing_data",
        ]:
            errors.append("retrospective participant scopes changed")
        if (
            data.get("new_participant_collection_allowed") is not False
            or data.get("raw_human_trace_publication_allowed") is not False
        ):
            errors.append("retrospective human-data boundary must fail closed")
        if (
            data.get("source_manifest_schema") != "cui-source-manifest-v1"
            or data.get("detector_dataset_schema") != "cui-detector-dataset-v2"
        ):
            errors.append("retrospective public contract binding changed")

    citation = payload.get("citation_snapshot")
    errors.extend(
        _exact_keys(
            citation,
            {
                "snapshot_label_local_date",
                "display_timezone",
                "retrieval_window_utc",
                "primary_index",
                "cross_checks",
                "edge_schema",
                "raw_response_hash_required",
                "xpac_reporting",
                "claim_boundary",
            },
            "citation_snapshot",
        )
    )
    if isinstance(citation, dict):
        if (
            citation.get("snapshot_label_local_date") != "2026-08-11"
            or citation.get("display_timezone") != "Asia/Shanghai"
            or citation.get("primary_index") != "openalex"
            or citation.get("cross_checks") != ["semantic_scholar", "crossref"]
            or citation.get("edge_schema") != "cui-citation-assertion-v1"
            or citation.get("raw_response_hash_required") is not True
            or citation.get("xpac_reporting") != "separate_sensitivity_layer"
        ):
            errors.append("citation snapshot policy changed")
        window = citation.get("retrieval_window_utc")
        errors.extend(_exact_keys(window, {"start", "end_exclusive"}, "retrieval_window_utc"))
        if isinstance(window, dict):
            start = window.get("start")
            end = window.get("end_exclusive")
            if not _utc(start) or not _utc(end):
                errors.append("citation retrieval window must use valid UTC timestamps")
            elif datetime.fromisoformat(end[:-1] + "+00:00") <= datetime.fromisoformat(
                start[:-1] + "+00:00"
            ):
                errors.append("citation retrieval window is empty or reversed")

    cases = payload.get("codex_cases")
    errors.extend(
        _exact_keys(
            cases,
            {"schema_version", "human_participant_data_allowed", "formal_claim_permitted"},
            "codex_cases",
        )
    )
    if isinstance(cases, dict) and cases != {
        "schema_version": "cui-codex-case-v1",
        "human_participant_data_allowed": False,
        "formal_claim_permitted": False,
    }:
        errors.append("Codex case policy must remain non-human and non-formal")

    retired = payload.get("stagehand_retirement")
    errors.extend(
        _exact_keys(
            retired,
            {
                "version",
                "status",
                "active_install_allowed",
                "active_formal_execution_allowed",
                "historical_results_preserved",
                "replacement_implemented",
                "replacement_name",
            },
            "stagehand_retirement",
        )
    )
    if isinstance(retired, dict) and retired != {
        "version": "3.7.1",
        "status": "historical_only",
        "active_install_allowed": False,
        "active_formal_execution_allowed": False,
        "historical_results_preserved": True,
        "replacement_implemented": False,
        "replacement_name": None,
    }:
        errors.append("Stagehand retirement boundary changed")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args(argv)
    errors = validate(args.config)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print("PASS: retrospective policy is closed, non-formal, and historically isolated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
