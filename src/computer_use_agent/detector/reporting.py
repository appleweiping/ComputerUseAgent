"""Closed descriptive and privacy-projected reporting contracts."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Any

from computer_use_agent.features.statistics import task_descriptive_summary

from .contract import FEATURE_ALLOWLIST, canonical_json

RESULT_SCHEMA = "cui-detector-analysis-v2"
DESCRIPTIVE_SCHEMA = "cui-detector-task-descriptive-v1"
TRAJECTORY_SCHEMA = "cui-detector-trajectory-projection-v1"
TRAJECTORY_ARTIFACT = "trajectory_projection.json"
TASK_ALIAS_METHOD = "sorted_source_task_to_ordinal_v1"
PROJECTION_PRIVACY = {
    "aggregation_level": "task_class",
    "task_identifiers_replaced": True,
    "record_identifiers_embedded": False,
    "source_paths_embedded": False,
    "raw_feature_rows_embedded": False,
    "raw_event_sequences_embedded": False,
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def detector_descriptive_summary(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build task-internal binary summaries without publishing source task names."""

    tasks = sorted({str(record["task_group"]) for record in records})
    aliases = {task: f"task-{index:02d}" for index, task in enumerate(tasks, start=1)}
    mapping_binding = [{"source_task": task, "task_ref": aliases[task]} for task in tasks]
    summary = task_descriptive_summary(
        records,
        feature_names=FEATURE_ALLOWLIST,
        task_field="task_group",
        group_field="label",
        group_collection_key="labels",
        task_aliases=aliases,
    )
    result = {
        "schema_version": DESCRIPTIVE_SCHEMA,
        "feature_names": list(FEATURE_ALLOWLIST),
        "task_aliasing": {
            "method": TASK_ALIAS_METHOD,
            "source_names_embedded": False,
            "mapping_sha256": _sha256(canonical_json(mapping_binding)),
        },
        **summary,
    }
    validate_detector_descriptive(result)
    return result


def validate_detector_descriptive(payload: Mapping[str, Any]) -> None:
    """Reject any descriptive payload outside the aggregate-only schema."""

    if (
        set(payload)
        != {
            "schema_version",
            "feature_names",
            "task_aliasing",
            "by_task",
        }
        or payload.get("schema_version") != DESCRIPTIVE_SCHEMA
    ):
        raise ValueError("Detector descriptive statistics do not match the closed schema")
    if payload.get("feature_names") != list(FEATURE_ALLOWLIST):
        raise ValueError("Detector descriptive feature order differs from the allowlist")
    aliasing = payload.get("task_aliasing")
    if not isinstance(aliasing, Mapping) or set(aliasing) != {
        "method",
        "source_names_embedded",
        "mapping_sha256",
    }:
        raise ValueError("Detector descriptive task aliasing is malformed")
    if (
        aliasing.get("method") != TASK_ALIAS_METHOD
        or aliasing.get("source_names_embedded") is not False
        or not _is_sha256(aliasing.get("mapping_sha256"))
    ):
        raise ValueError("Detector descriptive task aliases are not privacy-bound")
    by_task = payload.get("by_task")
    if not isinstance(by_task, Mapping) or len(by_task) < 3:
        raise ValueError("Detector descriptive statistics require at least three tasks")
    expected_refs = [f"task-{index:02d}" for index in range(1, len(by_task) + 1)]
    if list(by_task) != expected_refs:
        raise ValueError("Detector descriptive task references are not canonical aliases")
    for task_ref, task in by_task.items():
        if not isinstance(task, Mapping) or set(task) != {
            "labels",
            "human_minus_agent_hedges_g",
        }:
            raise ValueError(f"Detector descriptive task {task_ref} is malformed")
        labels = task.get("labels")
        effects = task.get("human_minus_agent_hedges_g")
        if not isinstance(labels, Mapping) or set(labels) != {"agent", "human"}:
            raise ValueError(f"Detector descriptive task {task_ref} lacks binary labels")
        if not isinstance(effects, Mapping) or set(effects) != set(FEATURE_ALLOWLIST):
            raise ValueError(f"Detector descriptive task {task_ref} has invalid effects")
        for value in effects.values():
            if value is not None and not _finite_number(value):
                raise ValueError("Hedges-g values must be finite numbers or null")
        for label, feature_rows in labels.items():
            if not isinstance(feature_rows, Mapping) or set(feature_rows) != set(FEATURE_ALLOWLIST):
                raise ValueError(
                    f"Detector descriptive {task_ref}/{label} feature order is invalid"
                )
            expected_n: int | None = None
            for statistics_row in feature_rows.values():
                if not isinstance(statistics_row, Mapping) or set(statistics_row) != {
                    "mean",
                    "median",
                    "n",
                }:
                    raise ValueError("Descriptive rows require exactly n/mean/median")
                count = statistics_row.get("n")
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise ValueError("Descriptive sample counts must be positive integers")
                if expected_n is None:
                    expected_n = count
                elif count != expected_n:
                    raise ValueError("Descriptive sample counts differ across features")
                if not _finite_number(statistics_row.get("mean")) or not _finite_number(
                    statistics_row.get("median")
                ):
                    raise ValueError("Descriptive means and medians must be finite")


def _projection_from_valid_descriptive(
    descriptive: Mapping[str, Any],
) -> dict[str, Any]:
    task_profiles = []
    by_task = descriptive["by_task"]
    assert isinstance(by_task, Mapping)
    for task_ref, task in by_task.items():
        assert isinstance(task, Mapping)
        labels = task["labels"]
        assert isinstance(labels, Mapping)
        classes = []
        for label in ("agent", "human"):
            feature_rows = labels[label]
            assert isinstance(feature_rows, Mapping)
            first = feature_rows[FEATURE_ALLOWLIST[0]]
            assert isinstance(first, Mapping)
            classes.append(
                {
                    "label": label,
                    "n": first["n"],
                    "points": [
                        {
                            "feature": feature,
                            "mean": feature_rows[feature]["mean"],
                            "median": feature_rows[feature]["median"],
                        }
                        for feature in FEATURE_ALLOWLIST
                    ],
                }
            )
        task_profiles.append({"task_ref": task_ref, "classes": classes})
    return {
        "schema_version": TRAJECTORY_SCHEMA,
        "projection_type": "task_class_aggregate_feature_profile_not_event_timeline",
        "feature_order": list(FEATURE_ALLOWLIST),
        "task_profiles": task_profiles,
        "source_descriptive_sha256": _sha256(canonical_json(descriptive)),
        "privacy": dict(PROJECTION_PRIVACY),
    }


def build_trajectory_projection(
    descriptive: Mapping[str, Any],
) -> dict[str, Any]:
    """Create plot-ready aggregate profiles with no record or event sequence."""

    validate_detector_descriptive(descriptive)
    return _projection_from_valid_descriptive(descriptive)


def validate_trajectory_projection(
    payload: Mapping[str, Any],
    *,
    descriptive: Mapping[str, Any],
) -> None:
    """Require byte-equivalent regeneration from validated descriptive statistics."""

    validate_detector_descriptive(descriptive)
    expected = _projection_from_valid_descriptive(descriptive)
    if payload != expected:
        raise ValueError("Trajectory projection differs from its deterministic privacy projection")


def trajectory_artifact_binding(
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    if projection.get("schema_version") != TRAJECTORY_SCHEMA:
        raise ValueError("Trajectory projection has an unsupported schema")
    source_hash = projection.get("source_descriptive_sha256")
    if not _is_sha256(source_hash) or projection.get("privacy") != PROJECTION_PRIVACY:
        raise ValueError("Trajectory projection privacy/source binding is invalid")
    return {
        "artifact_name": TRAJECTORY_ARTIFACT,
        "schema_version": TRAJECTORY_SCHEMA,
        "sha256": _sha256(canonical_json(projection)),
        "source_descriptive_sha256": source_hash,
        "privacy": dict(PROJECTION_PRIVACY),
    }


def validate_reporting_contract(result: Mapping[str, Any]) -> None:
    """Validate result-level descriptive and trajectory artifact requirements."""

    if result.get("schema_version") != RESULT_SCHEMA:
        raise ValueError("Detector result has an unsupported reporting schema")
    descriptive = result.get("descriptive")
    binding = result.get("trajectory_artifact")
    if not isinstance(descriptive, Mapping) or not isinstance(binding, Mapping):
        raise ValueError("Detector result lacks descriptive/trajectory reporting")
    validate_detector_descriptive(descriptive)
    expected_source = _sha256(canonical_json(descriptive))
    if set(binding) != {
        "artifact_name",
        "schema_version",
        "sha256",
        "source_descriptive_sha256",
        "privacy",
    }:
        raise ValueError("Detector trajectory artifact binding is not closed")
    if (
        binding.get("artifact_name") != TRAJECTORY_ARTIFACT
        or binding.get("schema_version") != TRAJECTORY_SCHEMA
        or not _is_sha256(binding.get("sha256"))
        or binding.get("source_descriptive_sha256") != expected_source
        or binding.get("privacy") != PROJECTION_PRIVACY
    ):
        raise ValueError("Detector trajectory artifact binding is invalid")
