"""Drift tests: the dependency-free trace validator mirrors the packaged schemas.

The runtime validator in ``computer_use_agent.trace.validation`` duplicates
the security-critical parts of the JSON Schemas so the X11 guest can validate
without jsonschema. These tests read the schemas (never edit them) and fail
when the two field lists diverge.
"""

from __future__ import annotations

from typing import Any

import pytest

from computer_use_agent.schemas import load_schema
from computer_use_agent.trace import validation

BROWSER_V2 = load_schema("cui-trace-v2")
X11_V2 = load_schema("x11-trace-v2")


def _definition(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    reference = node.get("$ref")
    if reference is None:
        return node
    assert reference.startswith("#/$defs/"), reference
    return schema["$defs"][reference.removeprefix("#/$defs/")]


def _closed_fields(definition: dict[str, Any]) -> frozenset[str]:
    assert definition.get("additionalProperties") is False
    required = frozenset(definition["required"])
    assert required == frozenset(definition["properties"]), "every property must be required"
    return required


def test_browser_v2_session_event_and_summary_fields_match_the_schema():
    definitions = BROWSER_V2["$defs"]
    assert _closed_fields(definitions["metadata"]) == validation.BROWSER_V2_SESSION_FIELDS
    assert _closed_fields(definitions["event"]) == validation.BROWSER_V2_EVENT_FIELDS
    assert _closed_fields(definitions["summary"]) == validation.BROWSER_SUMMARY_FIELDS
    assert set(BROWSER_V2["required"]) == {"metadata", "summary", "events"}


@pytest.mark.parametrize("name", sorted(validation.BROWSER_V2_NESTED_FIELDS))
def test_browser_v2_nested_objects_match_the_schema(name):
    event = BROWSER_V2["$defs"]["event"]["properties"]
    definition = _definition(BROWSER_V2, event[name])
    assert _closed_fields(definition) == validation.BROWSER_V2_NESTED_FIELDS[name]


def test_x11_v2_records_match_the_schema():
    definitions = X11_V2["$defs"]
    session = definitions["session"]["properties"]["metadata"]
    assert _closed_fields(session) == validation.V2_SESSION_FIELDS
    event = _closed_fields(definitions["event"]) - {"record_type"}
    assert event == validation.V2_EVENT_FIELDS
    terminal = _closed_fields(definitions["sessionEnd"]) - {"record_type"}
    assert terminal == validation.V2_TERMINAL_FIELDS
