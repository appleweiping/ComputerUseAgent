"""Packaged JSON Schemas, the single source of truth for every wire format.

The schema files ship as package data and are always read through
:mod:`importlib.resources`, so validation works from a wheel, an editable
install or a source checkout alike. Schema identifiers are the historical
``cui-`` wire names (``schema_version`` values); only the ``$id`` URLs carry the
project name.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from importlib import resources
from importlib.resources.abc import Traversable
from types import MappingProxyType
from typing import Any

SCHEMA_ID_BASE = "https://github.com/appleweiping/ComputerUseAgent/schemas/"

#: Schema identifier -> packaged file name.
SCHEMA_FILES: Mapping[str, str] = MappingProxyType(
    {
        "cui-citation-assertion-v1": "cui-citation-assertion-v1.schema.json",
        "cui-codex-case-v1": "cui-codex-case-v1.schema.json",
        "cui-detector-dataset-v2": "cui-detector-dataset-v2.schema.json",
        "cui-source-manifest-v1": "cui-source-manifest-v1.schema.json",
        "cui-trace-v1": "cui-trace-v1.schema.json",
        "cui-trace-v2": "cui-trace-v2.schema.json",
        "x11-trace-v2": "x11-trace-v2.schema.json",
    }
)

__all__ = [
    "SCHEMA_FILES",
    "SCHEMA_ID_BASE",
    "UnknownSchemaError",
    "load_schema",
    "schema_ids",
    "schema_resource",
    "schema_text",
]


class UnknownSchemaError(KeyError):
    """Raised for a schema identifier that is not packaged."""

    def __str__(self) -> str:
        return f"unknown schema {self.args[0]!r}; known: {', '.join(schema_ids())}"


def schema_ids() -> tuple[str, ...]:
    """Return every packaged schema identifier in sorted order."""

    return tuple(sorted(SCHEMA_FILES))


def schema_resource(schema_id: str) -> Traversable:
    """Return the packaged resource that holds ``schema_id``."""

    try:
        file_name = SCHEMA_FILES[schema_id]
    except KeyError:
        raise UnknownSchemaError(schema_id) from None
    return resources.files(__name__).joinpath(file_name)


def schema_text(schema_id: str) -> str:
    """Return the exact UTF-8 text of one packaged schema."""

    return schema_resource(schema_id).read_text(encoding="utf-8")


@cache
def _parsed(schema_id: str) -> str:
    # Cache the canonical serialization, never a mutable object.
    payload = json.loads(schema_text(schema_id))
    if not isinstance(payload, dict):
        raise ValueError(f"packaged schema {schema_id} is not a JSON object")
    return json.dumps(payload)


def load_schema(schema_id: str) -> dict[str, Any]:
    """Return a fresh parsed copy of one packaged schema."""

    value: dict[str, Any] = json.loads(_parsed(schema_id))
    return value
