"""Drift tests: one 23-feature contract across code and the dataset schema."""

from __future__ import annotations

from collections import Counter

from computer_use_agent.detector.contract import FEATURE_ALLOWLIST
from computer_use_agent.detector.contract import FEATURE_GROUPS as CONTRACT_GROUPS
from computer_use_agent.features import FEATURE_GROUPS, FEATURE_NAMES
from computer_use_agent.schemas import load_schema

EXPECTED_GROUP_SIZES = {
    "timing": 7,
    "pointer": 6,
    "keyboard": 3,
    "focus_and_mix": 3,
    "missingness": 4,
}


def test_feature_names_equal_the_detector_allowlist():
    assert len(FEATURE_NAMES) == 23
    assert len(set(FEATURE_NAMES)) == 23
    assert tuple(FEATURE_NAMES) == FEATURE_ALLOWLIST


def test_feature_groups_partition_the_feature_names():
    assert CONTRACT_GROUPS is FEATURE_GROUPS
    members = Counter(name for group in FEATURE_GROUPS.values() for name in group)
    assert set(members) == set(FEATURE_NAMES)
    assert all(count == 1 for count in members.values()), "a feature is in two groups"
    assert {group: len(names) for group, names in FEATURE_GROUPS.items()} == EXPECTED_GROUP_SIZES


def test_dataset_schema_features_equal_feature_names():
    features = load_schema("cui-detector-dataset-v2")["$defs"]["features"]
    assert features["additionalProperties"] is False
    assert features["required"] == list(FEATURE_NAMES)
    assert list(features["properties"]) == list(FEATURE_NAMES)
