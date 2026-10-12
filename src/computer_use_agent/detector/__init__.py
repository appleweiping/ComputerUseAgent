"""Leakage-controlled exploratory detector analysis.

This package deliberately separates behavioral feature vectors from labels and
task provenance.  It is analysis infrastructure, not a trained deployment
artifact. It never imports research code: stage-specific dataset provenance is
validated through :mod:`computer_use_agent.detector.provenance`.
"""

from __future__ import annotations

from typing import Any

from .contract import FEATURE_ALLOWLIST, validate_dataset
from .provenance import (
    get_provenance_validator,
    load_provenance_modules,
    register_provenance_validator,
)


def run_pipeline(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Lazy public entry point that keeps ``python -m ...pipeline`` warning-free."""

    from .pipeline import run_pipeline as implementation

    return implementation(*args, **kwargs)


__all__ = [
    "FEATURE_ALLOWLIST",
    "get_provenance_validator",
    "load_provenance_modules",
    "register_provenance_validator",
    "run_pipeline",
    "validate_dataset",
]
