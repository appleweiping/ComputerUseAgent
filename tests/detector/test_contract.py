from __future__ import annotations

import copy

import pytest

from computer_use_agent.detector.contract import (
    DATASET_SCHEMA_V1,
    DATASET_SCHEMA_V2,
    FEATURE_ALLOWLIST,
    RECORD_SCHEMA_V1,
    model_matrix,
    validate_dataset,
    validate_feature_names,
    validate_record,
)
from tests.fixtures.synthetic import make_records


def test_closed_allowlist_and_model_matrix(synthetic_records):
    audit = validate_dataset(synthetic_records)
    matrix = model_matrix(synthetic_records)
    assert audit["task_count"] == 3
    assert len(matrix) == 18
    assert len(matrix[0]) == len(FEATURE_ALLOWLIST)
    assert "label" not in FEATURE_ALLOWLIST
    assert "task_group" not in FEATURE_ALLOWLIST


@pytest.mark.parametrize(
    "name",
    ["actor_type", "framework_version", "task_id", "url_hash", "validator_passed", "action_text"],
)
def test_leakage_like_feature_name_is_rejected(name):
    with pytest.raises(ValueError, match="outside the allowlist|Forbidden"):
        validate_feature_names([*FEATURE_ALLOWLIST, name])


def test_unexpected_feature_fails_closed(synthetic_records):
    record = copy.deepcopy(synthetic_records[0])
    record["features"]["actor_numeric"] = 1.0
    with pytest.raises(ValueError, match="Feature schema mismatch"):
        validate_record(record)


def test_two_tasks_fail_closed():
    with pytest.raises(ValueError, match="at least 3 tasks"):
        validate_dataset(make_records(task_count=2))


def test_held_out_task_missing_class_fails_closed(synthetic_records):
    damaged = [
        row
        for row in synthetic_records
        if not (row["task_group"] == "synthetic-task-3" and row["label"] == "human")
    ]
    with pytest.raises(ValueError, match="both classes"):
        validate_dataset(damaged)


def test_duplicate_source_trace_fails_closed(synthetic_records):
    duplicate = copy.deepcopy(synthetic_records)
    duplicate[-1]["source_sha256"] = duplicate[0]["source_sha256"]
    with pytest.raises(ValueError, match="Duplicate"):
        validate_dataset(duplicate)


def test_dataset_v2_accepts_restricted_existing_data(synthetic_records):
    records = copy.deepcopy(synthetic_records)
    for record in records:
        record["evidence_class"] = "external_restricted"
        record["participant_scope"] = "third_party_existing_data"
    audit = validate_dataset(records, dataset_schema=DATASET_SCHEMA_V2)
    assert audit["evidence_classes"] == ["external_restricted"]
    assert audit["participant_scopes"] == ["third_party_existing_data"]


def test_legacy_v1_records_keep_the_old_closed_vocabulary(synthetic_records):
    records = copy.deepcopy(synthetic_records)
    for record in records:
        record["schema_version"] = RECORD_SCHEMA_V1
    validate_dataset(records, dataset_schema=DATASET_SCHEMA_V1)
    records[0]["evidence_class"] = "external_restricted"
    records[0]["participant_scope"] = "third_party_existing_data"
    with pytest.raises(ValueError, match="Unknown evidence_class"):
        validate_dataset(records, dataset_schema=DATASET_SCHEMA_V1)
