"""Public, publication-safe JSON contracts used across research workstreams."""

from __future__ import annotations

from typing import Any

__all__ = [
    "SCHEMA_PATHS",
    "ContractValidationError",
    "load_json_records",
    "validate_contract",
    "validate_contract_records",
    "validate_file",
]


def __getattr__(name: str) -> Any:
    """Keep convenience imports lazy so ``python -m ...validation`` is warning-free."""

    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import validation

    return getattr(validation, name)
