"""Closed data contract for detector inputs and model matrices."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from computer_use_agent.features import FEATURE_GROUPS as FEATURE_GROUPS  # re-export
from computer_use_agent.features import FEATURE_NAMES, validate_feature_vector
from computer_use_agent.study.policy import (
    DENY_FORMAL_HUMAN_DATA,
    ConfigHumanDataPolicy,
    FormalStudyError,
    HumanDataPolicy,
)

DATASET_SCHEMA_V1 = "cui-detector-dataset-v1"
DATASET_SCHEMA_V2 = "cui-detector-dataset-v2"
DATASET_SCHEMA = DATASET_SCHEMA_V2
DATASET_SCHEMAS = frozenset({DATASET_SCHEMA_V1, DATASET_SCHEMA_V2})
RECORD_SCHEMA_V1 = "cui-detector-record-v1"
RECORD_SCHEMA_V2 = "cui-detector-record-v2"
RECORD_SCHEMA = RECORD_SCHEMA_V2
FEATURE_ALLOWLIST = tuple(FEATURE_NAMES)
TARGET_LABELS = ("agent", "human")
EVIDENCE_CLASSES_V1 = frozenset({"synthetic_test_fixture", "formal_trace", "external_public"})
EVIDENCE_CLASSES_V2 = EVIDENCE_CLASSES_V1 | {"external_restricted"}
EVIDENCE_CLASSES = EVIDENCE_CLASSES_V2
PARTICIPANT_SCOPES_V1 = frozenset(
    {
        "synthetic_no_participant",
        "single_self_participant",
        "approved_multi_participant",
        "public_dataset",
    }
)
PARTICIPANT_SCOPES_V2 = PARTICIPANT_SCOPES_V1 | {"third_party_existing_data"}
PARTICIPANT_SCOPES = PARTICIPANT_SCOPES_V2

# These tokens are forbidden from the model matrix even if a future feature
# extractor changes its names.  The exact allowlist below is the primary gate;
# this list documents the threat model and catches accidental near-matches.
FORBIDDEN_FEATURE_TOKENS = frozenset(
    {
        "actor",
        "framework",
        "task",
        "url",
        "uri",
        "selector",
        "uuid",
        "session",
        "absolute_time",
        "wall_time",
        "epoch",
        "browser_version",
        "os_version",
        "software_version",
        "validator",
        "trusted",
        "action_text",
        "instruction",
        "executor",
        "participant",
    }
)

# FEATURE_GROUPS (re-exported above) partitions the allowlist into the five
# modality groups used by the ablations; it lives in computer_use_agent.features.

UNIT_INTERVAL_FEATURES = frozenset(
    {
        "pointer_straightness",
        "pointer_turning_entropy",
        "correction_ratio",
        "event_type_entropy",
        "missing_pointer",
        "missing_keyboard",
        "missing_click_dwell",
        "missing_focus",
    }
)
BINARY_FEATURES = frozenset(
    {
        "missing_pointer",
        "missing_keyboard",
        "missing_click_dwell",
        "missing_focus",
    }
)
SIGNED_FEATURES = frozenset({"inter_event_burstiness"})

RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "record_ref",
        "source_sha256",
        "label",
        "task_group",
        "agent_family",
        "evidence_class",
        "participant_scope",
        "features",
        "quality",
    }
)


def canonical_json(payload: object) -> bytes:
    """Serialize research state deterministically and reject NaN/Infinity."""

    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def dataset_fingerprint(records: Sequence[Mapping[str, Any]]) -> str:
    """Hash the closed, path-free detector records."""

    return hashlib.sha256(canonical_json(list(records))).hexdigest()


def validate_feature_names(names: Sequence[str]) -> tuple[str, ...]:
    """Require a non-empty ordered subset of the reviewed allowlist."""

    return _validate_feature_names(tuple(names))


@lru_cache(maxsize=1024)
def _validate_feature_names(selected: tuple[str, ...]) -> tuple[str, ...]:
    # Pure function of the (hashable) name tuple; exceptions are not cached.
    if not selected:
        raise ValueError("The model feature set is empty")
    if len(selected) != len(set(selected)):
        raise ValueError("The model feature set contains duplicates")
    unexpected = sorted(set(selected) - set(FEATURE_ALLOWLIST))
    if unexpected:
        raise ValueError(f"Features are outside the allowlist: {unexpected}")
    lowered = {name.lower() for name in selected}
    token_hits = sorted(
        token for token in FORBIDDEN_FEATURE_TOKENS if any(token in name for name in lowered)
    )
    if token_hits:
        raise ValueError(f"Forbidden leakage tokens entered features: {token_hits}")
    return selected


def validate_record(
    record: Mapping[str, Any],
    *,
    record_schema: str | None = None,
) -> None:
    if set(record) != RECORD_FIELDS:
        missing = sorted(RECORD_FIELDS - set(record))
        unexpected = sorted(set(record) - RECORD_FIELDS)
        raise ValueError(
            f"Detector record schema mismatch; missing={missing}, unexpected={unexpected}"
        )
    selected_schema = record_schema or record["schema_version"]
    if selected_schema not in {RECORD_SCHEMA_V1, RECORD_SCHEMA_V2}:
        raise ValueError("Unsupported detector record schema")
    if record["schema_version"] != selected_schema:
        raise ValueError("Detector record schema does not match its dataset")
    record_ref = str(record["record_ref"])
    if len(record_ref) != 16 or any(
        character not in "0123456789abcdef" for character in record_ref
    ):
        raise ValueError("record_ref must be a 16-character lowercase hex token")
    source_hash = str(record["source_sha256"])
    if len(source_hash) != 64 or any(
        character not in "0123456789abcdef" for character in source_hash
    ):
        raise ValueError("source_sha256 must be lowercase SHA-256")
    if record["label"] not in TARGET_LABELS:
        raise ValueError(f"Primary label must be one of {TARGET_LABELS}")
    if not isinstance(record["task_group"], str) or not record["task_group"]:
        raise ValueError("task_group is required for leakage-safe splitting")
    if record["agent_family"] is not None and (
        not isinstance(record["agent_family"], str) or not record["agent_family"]
    ):
        raise ValueError("agent_family must be null or a non-empty string")
    if record["label"] == "human" and record["agent_family"] is not None:
        raise ValueError("Human records cannot carry an agent_family")
    evidence_classes = (
        EVIDENCE_CLASSES_V1 if selected_schema == RECORD_SCHEMA_V1 else EVIDENCE_CLASSES_V2
    )
    participant_scopes = (
        PARTICIPANT_SCOPES_V1 if selected_schema == RECORD_SCHEMA_V1 else PARTICIPANT_SCOPES_V2
    )
    if record["evidence_class"] not in evidence_classes:
        raise ValueError("Unknown evidence_class")
    if record["participant_scope"] not in participant_scopes:
        raise ValueError("participant_scope is outside the reviewed scope vocabulary")
    expected_scopes = {
        "synthetic_test_fixture": {"synthetic_no_participant"},
        "external_public": {"public_dataset"},
        "formal_trace": {"single_self_participant", "approved_multi_participant"},
    }
    if selected_schema == RECORD_SCHEMA_V2:
        expected_scopes["external_restricted"] = {"third_party_existing_data"}
    if record["participant_scope"] not in expected_scopes[record["evidence_class"]]:
        raise ValueError("participant_scope contradicts evidence_class")
    if not isinstance(record["quality"], Mapping):
        raise ValueError("quality must be an object")
    features = record["features"]
    if not isinstance(features, Mapping):
        raise ValueError("features must be an object")
    validate_feature_vector(features)
    validate_feature_names(tuple(features))
    for name, value in features.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Every feature must be numeric and non-boolean")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("Every feature must be finite")
        if name in UNIT_INTERVAL_FEATURES and not 0 <= numeric <= 1:
            raise ValueError(f"Feature {name} must be in [0, 1]")
        if name in BINARY_FEATURES and numeric not in {0.0, 1.0}:
            raise ValueError(f"Feature {name} must be binary")
        if name in SIGNED_FEATURES:
            if not -1 <= numeric <= 1:
                raise ValueError(f"Feature {name} must be in [-1, 1]")
        elif name not in UNIT_INTERVAL_FEATURES and numeric < 0:
            raise ValueError(f"Feature {name} must be non-negative")


def resolve_human_data_policy(
    *,
    human_data_policy: HumanDataPolicy | None = None,
    human_research_config: Mapping[str, Any] | None = None,
    verify_private_record: bool = True,
) -> HumanDataPolicy:
    """Pick the effective policy: explicit policy, else a config policy, else deny."""

    if human_data_policy is not None:
        if human_research_config is not None:
            raise ValueError("Pass either human_data_policy or human_research_config, not both")
        return human_data_policy
    if human_research_config is not None:
        # Detach an immutable ledger snapshot from caller mutation and reject
        # non-JSON values before handing it to the shared authorization gate.
        snapshot = json.loads(canonical_json(dict(human_research_config)))
        return ConfigHumanDataPolicy(snapshot, verify_private_record=verify_private_record)
    return DENY_FORMAL_HUMAN_DATA


def validate_human_data_use(
    records: Sequence[Mapping[str, Any]],
    *,
    human_research_config: Mapping[str, Any] | None = None,
    verify_private_record: bool = True,
    human_data_policy: HumanDataPolicy | None = None,
) -> None:
    """Authorize every formal human record through the injected policy.

    Without an explicit ``human_data_policy`` or ``human_research_config``
    formal human data are denied (fail closed).
    """

    formal_human = [
        record
        for record in records
        if record.get("evidence_class") == "formal_trace" and record.get("label") == "human"
    ]
    if not formal_human:
        return
    policy = resolve_human_data_policy(
        human_data_policy=human_data_policy,
        human_research_config=human_research_config,
        verify_private_record=verify_private_record,
    )
    for scope in sorted({str(record.get("participant_scope")) for record in formal_human}):
        try:
            policy.authorize(use="detector_training", participant_scope=scope)
        except FormalStudyError as error:
            raise ValueError(f"Formal human detector input is not authorized: {error}") from error


def validate_dataset(
    records: Sequence[Mapping[str, Any]],
    *,
    minimum_tasks: int = 3,
    dataset_schema: str | None = None,
    human_research_config: Mapping[str, Any] | None = None,
    verify_private_record: bool = True,
    human_data_policy: HumanDataPolicy | None = None,
) -> dict[str, Any]:
    """Fail closed unless task-held-out evaluation is structurally possible.

    Formal human records are authorized through ``human_data_policy`` (or a
    ``human_research_config``); by default they are denied.
    """

    if not records:
        raise ValueError("Detector dataset is empty")
    observed_record_schemas = {record.get("schema_version") for record in records}
    if dataset_schema is None:
        if observed_record_schemas == {RECORD_SCHEMA_V1}:
            dataset_schema = DATASET_SCHEMA_V1
        elif observed_record_schemas == {RECORD_SCHEMA_V2}:
            dataset_schema = DATASET_SCHEMA_V2
        else:
            raise ValueError("Detector dataset cannot mix record schema versions")
    if dataset_schema not in DATASET_SCHEMAS:
        raise ValueError("Unsupported detector dataset schema")
    expected_record_schema = (
        RECORD_SCHEMA_V1 if dataset_schema == DATASET_SCHEMA_V1 else RECORD_SCHEMA_V2
    )
    if observed_record_schemas != {expected_record_schema}:
        raise ValueError("Detector dataset and record schema versions do not agree")
    seen_refs: set[str] = set()
    seen_hashes: set[str] = set()
    by_task: Counter[str] = Counter()
    labels_by_task: dict[str, set[str]] = {}
    for record in records:
        validate_record(record, record_schema=expected_record_schema)
        record_ref = str(record["record_ref"])
        source_hash = str(record["source_sha256"])
        if record_ref in seen_refs or source_hash in seen_hashes:
            raise ValueError("Duplicate record or source trace detected")
        seen_refs.add(record_ref)
        seen_hashes.add(source_hash)
        task = str(record["task_group"])
        by_task[task] += 1
        labels_by_task.setdefault(task, set()).add(str(record["label"]))

    validate_human_data_use(
        records,
        human_research_config=human_research_config,
        verify_private_record=verify_private_record,
        human_data_policy=human_data_policy,
    )

    tasks = sorted(by_task)
    if len(tasks) < minimum_tasks:
        raise ValueError(
            f"Leave-one-task-out requires at least {minimum_tasks} tasks; got {len(tasks)}"
        )
    labels = sorted({str(record["label"]) for record in records})
    if labels != list(TARGET_LABELS):
        raise ValueError(f"Binary detector requires exactly {TARGET_LABELS}; got {labels}")
    incomplete = {
        task: sorted(labels_by_task[task])
        for task in tasks
        if labels_by_task[task] != set(TARGET_LABELS)
    }
    if incomplete:
        raise ValueError(f"Every held-out task must contain both classes: {incomplete}")
    if any(by_task[task] < 3 for task in tasks):
        raise ValueError("Each task needs at least three records for the negative control")
    return {
        "record_count": len(records),
        "task_count": len(tasks),
        "tasks": tasks,
        "class_counts": dict(sorted(Counter(str(row["label"]) for row in records).items())),
        "task_counts": dict(sorted(by_task.items())),
        "evidence_classes": sorted({str(row["evidence_class"]) for row in records}),
        "participant_scopes": sorted({str(row["participant_scope"]) for row in records}),
        "dataset_sha256": dataset_fingerprint(records),
    }


def model_matrix(
    records: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str] = FEATURE_ALLOWLIST,
) -> list[list[float]]:
    """Select values by allowlist only; provenance can never enter implicitly."""

    names = validate_feature_names(feature_names)
    rows: list[list[float]] = []
    for record in records:
        validate_record(record)
        features = record["features"]
        assert isinstance(features, Mapping)
        rows.append([float(features[name]) for name in names])
    return rows
