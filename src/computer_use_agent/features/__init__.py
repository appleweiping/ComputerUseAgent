"""Behavioral feature extraction over browser and X11 traces.

``FEATURE_NAMES`` is the ordered, reviewed 23-feature allowlist and
``FEATURE_GROUPS`` partitions it into the five modality groups used by the
detector ablations.
"""

from __future__ import annotations

from .extract import (
    FEATURE_GROUPS,
    FEATURE_NAMES,
    INPUT_EVENT_TYPES,
    LEAKAGE_FIELDS,
    extract_features,
    validate_feature_vector,
)
from .statistics import hedges_g, task_descriptive_summary

__all__ = [
    "FEATURE_GROUPS",
    "FEATURE_NAMES",
    "INPUT_EVENT_TYPES",
    "LEAKAGE_FIELDS",
    "extract_features",
    "hedges_g",
    "task_descriptive_summary",
    "validate_feature_vector",
]
