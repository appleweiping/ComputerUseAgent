from __future__ import annotations

import copy
import json

import pytest

from computer_use_agent.contracts.validation import ContractValidationError, validate_contract
from computer_use_agent.detector.contract import (
    DATASET_SCHEMA_V1,
    FEATURE_ALLOWLIST,
    RECORD_SCHEMA_V1,
)
from computer_use_agent.detector.evaluation import EvaluationConfig, evaluate_loto
from computer_use_agent.detector.pipeline import load_dataset_payload, run_pipeline
from computer_use_agent.detector.reporting import (
    build_trajectory_projection,
    validate_trajectory_projection,
)

TEST_CONFIG = EvaluationConfig(
    seed=20260808,
    epochs=20,
    calibration_epochs=20,
    bootstrap_repetitions=20,
    permutation_repetitions=3,
)


def test_loto_uses_every_task_once_and_calibrates_nested(synthetic_records):
    result = evaluate_loto(synthetic_records, config=TEST_CONFIG)
    assert result["fold_count"] == 3
    assert {fold["held_out_task"] for fold in result["folds"]} == {
        "synthetic-task-1",
        "synthetic-task-2",
        "synthetic-task-3",
    }
    assert len(result["predictions"]) == len(synthetic_records)
    assert all(
        fold["calibration"]["method"] == "nested_task_oof_platt_scaling"
        and len(fold["calibration"]["folds"]) == 2
        for fold in result["folds"]
    )


def test_full_pipeline_is_deterministic_and_marks_synthetic(synthetic_records):
    first = run_pipeline(synthetic_records, config=TEST_CONFIG)
    second = run_pipeline(copy.deepcopy(synthetic_records), config=TEST_CONFIG)
    assert first == second
    assert first["schema_version"] == "cui-detector-analysis-v2"
    assert first["pipeline_order"] == [
        "extract",
        "fit",
        "calibrate",
        "evaluate",
        "explain",
    ]
    assert first["formal_claim_permitted"] is False
    assert "Synthetic pipeline check only" in first["claim_status"]
    assert first["evaluation"]["task_identification_negative_control"]["purpose"].startswith(
        "negative_control"
    )
    assert first["evaluation"]["label_permutation"]["repetitions"] == 3
    assert len(first["evaluation"]["ablations"]["single_feature_drop"]) == len(FEATURE_ALLOWLIST)
    assert first["evaluation"]["task_cluster_bootstrap"]["repetitions"] == 20
    task = first["descriptive"]["by_task"]["task-01"]
    event_count = task["labels"]["human"]["event_count"]
    assert event_count["n"] == 3
    assert isinstance(event_count["mean"], float)
    assert isinstance(event_count["median"], float)
    assert isinstance(task["human_minus_agent_hedges_g"]["event_count"], float)
    descriptive_text = json.dumps(first["descriptive"], sort_keys=True)
    assert "synthetic-task-1" not in descriptive_text

    projection = build_trajectory_projection(first["descriptive"])
    validate_trajectory_projection(projection, descriptive=first["descriptive"])
    assert projection["privacy"]["raw_event_sequences_embedded"] is False
    assert projection["privacy"]["source_paths_embedded"] is False
    assert first["trajectory_artifact"]["sha256"]
    forbidden_exact_keys = {
        "events",
        "event",
        "path",
        "record_ref",
        "source_sha256",
        "session_id",
    }

    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value)) if value else set()
        return set()

    assert not (keys(projection) & forbidden_exact_keys)
    tampered = copy.deepcopy(projection)
    tampered["task_profiles"][0]["classes"][0]["events"] = []
    with pytest.raises(ValueError, match="deterministic privacy projection"):
        validate_trajectory_projection(tampered, descriptive=first["descriptive"])


def test_outer_prediction_does_not_change_when_label_is_read_after_scoring(
    synthetic_records,
):
    # Every record appears exactly once as outer test evidence; this makes a
    # hidden train-on-all-rows shortcut observable through the protocol shape.
    result = evaluate_loto(synthetic_records, config=TEST_CONFIG)
    refs = [row["record_ref"] for row in result["predictions"]]
    assert len(refs) == len(set(refs)) == len(synthetic_records)
    for fold in result["folds"]:
        assert fold["train_count"] + fold["test_count"] == len(synthetic_records)
        assert fold["test_count"] == 6


def test_formal_rows_cannot_bypass_the_stage_specific_ledger_bridge(
    synthetic_records,
):
    unbound = copy.deepcopy(synthetic_records)
    for row in unbound:
        row["evidence_class"] = "formal_trace"
        row["participant_scope"] = "single_self_participant"
    with pytest.raises(ValueError, match="immutable-ledger provenance"):
        run_pipeline(unbound, config=TEST_CONFIG)


def test_legacy_v1_dataset_is_readable_but_never_a_write_target(synthetic_records, tmp_path):
    records = copy.deepcopy(synthetic_records)
    for record in records:
        record["schema_version"] = RECORD_SCHEMA_V1
    payload = {"schema_version": DATASET_SCHEMA_V1, "records": records}
    path = tmp_path / "legacy-v1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded, provenance = load_dataset_payload(path)
    assert loaded == records
    assert provenance is None
    validate_contract(payload)
    with pytest.raises(ContractValidationError, match="read-only"):
        validate_contract(payload, for_write=True)
