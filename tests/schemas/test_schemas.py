"""Packaged schemas: discoverable through importlib.resources with project $id values."""

from __future__ import annotations

from importlib import resources

import pytest
from jsonschema import Draft202012Validator

from computer_use_agent.schemas import (
    SCHEMA_FILES,
    SCHEMA_ID_BASE,
    UnknownSchemaError,
    load_schema,
    schema_ids,
    schema_resource,
    schema_text,
)


def test_every_packaged_schema_file_is_registered():
    packaged = {
        entry.name
        for entry in resources.files("computer_use_agent.schemas").iterdir()
        if entry.name.endswith(".schema.json")
    }
    assert packaged == set(SCHEMA_FILES.values())
    assert schema_ids() == tuple(sorted(SCHEMA_FILES))
    assert SCHEMA_ID_BASE == "https://github.com/appleweiping/ComputerUseAgent/schemas/"


@pytest.mark.parametrize("schema_id", sorted(SCHEMA_FILES))
def test_schema_is_draft_2020_12_with_project_id(schema_id):
    schema = load_schema(schema_id)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == SCHEMA_ID_BASE + SCHEMA_FILES[schema_id]
    text = schema_text(schema_id)
    assert "minnesotanlp" not in text and "cu-detector" not in text
    assert schema_resource(schema_id).name == SCHEMA_FILES[schema_id]


@pytest.mark.parametrize(
    "schema_id",
    [
        "cui-citation-assertion-v1",
        "cui-codex-case-v1",
        "cui-detector-dataset-v2",
        "cui-source-manifest-v1",
    ],
)
def test_contract_schema_versions_keep_their_wire_names(schema_id):
    assert load_schema(schema_id)["properties"]["schema_version"] == {"const": schema_id}


def test_load_schema_returns_independent_copies():
    first = load_schema("cui-trace-v2")
    first["title"] = "mutated"
    assert load_schema("cui-trace-v2")["title"] != "mutated"


def test_unknown_schema_is_reported():
    with pytest.raises(UnknownSchemaError, match="unknown schema 'nope'") as raised:
        load_schema("nope")
    assert "cui-trace-v2" in str(raised.value)
    with pytest.raises(KeyError):
        schema_text("nope")
