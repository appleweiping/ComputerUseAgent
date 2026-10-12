from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from computer_use_agent.contracts.validation import (
    SCHEMA_PATHS,
    ContractValidationError,
    validate_contract,
    validate_contract_records,
)

FIXTURES = Path(__file__).with_name("fixtures")
VALID_FIXTURES = {
    "cui-source-manifest-v1": "source_manifest.valid.json",
    "cui-detector-dataset-v2": "detector_dataset_v2.valid.json",
    "cui-citation-assertion-v1": "citation_assertion.valid.json",
    "cui-codex-case-v1": "codex_case.valid.json",
}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / VALID_FIXTURES[name]).read_text(encoding="utf-8"))


def test_all_public_schemas_are_valid_draft_2020_12_and_closed():
    explicit_maps = {"provenance", "quality", "stringIntMap"}

    def walk(value, path=()):
        yield path, value
        if isinstance(value, dict):
            for key, child in value.items():
                yield from walk(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from walk(child, (*path, index))

    for name, path in SCHEMA_PATHS.items():
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False, name
        for location, node in walk(schema):
            if not isinstance(node, dict) or node.get("type") != "object":
                continue
            if any(part in {"if", "then", "contains"} for part in location):
                continue
            assert "additionalProperties" in node, (name, location)
            if node["additionalProperties"] is not False:
                assert location[-1] in explicit_maps, (name, location)
                assert str(node.get("$comment", "")).startswith("Explicit"), (
                    name,
                    location,
                )


@pytest.mark.parametrize("schema_version", sorted(VALID_FIXTURES))
def test_valid_public_fixture(schema_version):
    validate_contract(fixture(schema_version), schema_version=schema_version)


@pytest.mark.parametrize("schema_version", sorted(VALID_FIXTURES))
def test_top_level_extension_fails_closed(schema_version):
    payload = fixture(schema_version)
    payload["unexpected"] = True
    with pytest.raises(ContractValidationError, match="additional properties|unexpected"):
        validate_contract(payload, schema_version=schema_version)


def test_utc_and_sha256_are_strict():
    payload = fixture("cui-source-manifest-v1")
    payload["created_at_utc"] = "2026-08-11T08:00:00+08:00"
    with pytest.raises(ContractValidationError, match="does not match|validation failed"):
        validate_contract(payload)
    payload = fixture("cui-source-manifest-v1")
    payload["sources"][0]["sha256"] = "A" * 64
    with pytest.raises(ContractValidationError, match="does not match|validation failed"):
        validate_contract(payload)


@pytest.mark.parametrize(
    "unsafe_value",
    ["Z:/private/source.json", "/home/user/source.json", "file:///tmp/source.json"],
)
def test_absolute_paths_are_rejected_even_in_extension_points(unsafe_value):
    payload = fixture("cui-detector-dataset-v2")
    payload["records"][0]["quality"]["source"] = unsafe_value
    with pytest.raises(ContractValidationError, match="absolute path"):
        validate_contract(payload)


def test_secret_like_keys_and_values_are_rejected_in_extension_points():
    payload = fixture("cui-detector-dataset-v2")
    payload["records"][0]["quality"]["api_key"] = "redacted"
    with pytest.raises(ContractValidationError, match="secret-like key"):
        validate_contract(payload)
    payload = fixture("cui-detector-dataset-v2")
    payload["records"][0]["quality"]["note"] = "sk-" + "x" * 24
    with pytest.raises(ContractValidationError, match="secret-like value"):
        validate_contract(payload)


def test_source_ids_and_citation_edges_have_semantic_guards():
    payload = fixture("cui-source-manifest-v1")
    payload["sources"].append(copy.deepcopy(payload["sources"][0]))
    with pytest.raises(ContractValidationError, match="duplicate source_id"):
        validate_contract(payload)
    assertion = fixture("cui-citation-assertion-v1")
    assertion["cited_work"] = copy.deepcopy(assertion["citing_work"])
    with pytest.raises(ContractValidationError, match="self-edge"):
        validate_contract(assertion)
    assertions = [
        fixture("cui-citation-assertion-v1"),
        fixture("cui-citation-assertion-v1"),
    ]
    with pytest.raises(ContractValidationError, match="duplicate assertion_id"):
        validate_contract_records(
            assertions,
            schema_version="cui-citation-assertion-v1",
        )


def test_non_openalex_assertion_cannot_claim_xpac():
    assertion = fixture("cui-citation-assertion-v1")
    assertion["provider"] = "semantic_scholar"
    with pytest.raises(ContractValidationError, match="is_xpac"):
        validate_contract(assertion)


def test_codex_case_is_non_formal_and_time_ordered():
    payload = fixture("cui-codex-case-v1")
    payload["formal_claim_permitted"] = True
    with pytest.raises(ContractValidationError, match="False was expected|validation failed"):
        validate_contract(payload)
    payload = fixture("cui-codex-case-v1")
    payload["completed_at_utc"] = "2026-08-10T23:59:59Z"
    with pytest.raises(ContractValidationError, match="precedes"):
        validate_contract(payload)


def test_codex_case_limits_and_control_guards_are_fixed():
    payload = fixture("cui-codex-case-v1")
    payload["limits"]["max_actions"] = 31
    with pytest.raises(ContractValidationError, match="30 was expected|validation failed"):
        validate_contract(payload)
    payload = fixture("cui-codex-case-v1")
    payload["no_manual_rescue"] = False
    with pytest.raises(ContractValidationError, match="True was expected|validation failed"):
        validate_contract(payload)
    payload = fixture("cui-codex-case-v1")
    payload["actor"]["harness"] = "scripted_playwright"
    with pytest.raises(ContractValidationError, match="not one of|validation failed"):
        validate_contract(payload)


def test_codex_case_permits_declared_computer_control_fallback():
    payload = fixture("cui-codex-case-v1")
    payload["actor"]["harness"] = "computer_control"
    payload["actor"]["app_version"] = "unavailable"
    validate_contract(payload)


def test_codex_case_recorder_descriptor_is_consistent_and_pathless():
    payload = fixture("cui-codex-case-v1")
    payload["recorder"]["record_sha256"] = None
    with pytest.raises(ContractValidationError, match="captured recorder requires"):
        validate_contract(payload)
    payload = fixture("cui-codex-case-v1")
    payload["recorder"]["event_count"] = 2
    with pytest.raises(ContractValidationError, match="lower than action_count"):
        validate_contract(payload)
    payload = fixture("cui-codex-case-v1")
    payload["recorder"]["raw_path"] = "results/raw.json"
    with pytest.raises(ContractValidationError, match="additional properties|raw_path"):
        validate_contract(payload)


def test_codex_case_outcome_matches_official_validator():
    payload = fixture("cui-codex-case-v1")
    payload["validator"]["reward"] = 0
    with pytest.raises(ContractValidationError, match="successful outcome requires"):
        validate_contract(payload)
    payload = fixture("cui-codex-case-v1")
    payload["outcome"] = {"status": "timeout", "failure_code": "time-limit"}
    payload["validator"] = {
        "implementation": "browsergym_official_reward",
        "reward": 0,
        "terminated": False,
        "truncated": True,
    }
    validate_contract(payload)
    payload["validator"]["truncated"] = False
    with pytest.raises(ContractValidationError, match="timeout outcome requires"):
        validate_contract(payload)


def test_codex_case_screenshot_paths_are_unique():
    payload = fixture("cui-codex-case-v1")
    payload["screenshots"].append(copy.deepcopy(payload["screenshots"][0]))
    with pytest.raises(ContractValidationError, match="duplicate screenshot paths"):
        validate_contract(payload)


def test_external_restricted_requires_third_party_scope_and_manifest():
    payload = fixture("cui-detector-dataset-v2")
    del payload["source_manifest"]
    with pytest.raises(ContractValidationError, match="required property|source_manifest"):
        validate_contract(payload)
    payload = fixture("cui-detector-dataset-v2")
    payload["records"][0]["participant_scope"] = "public_dataset"
    with pytest.raises(
        ContractValidationError, match="third_party_existing_data|validation failed"
    ):
        validate_contract(payload)


# ------------------------------------------------- file-level validation --

from computer_use_agent.contracts.validation import (  # noqa: E402
    load_json_records,
    main,
    summarize_contract_records,
    validate_file,
    validate_trace_file,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def test_detector_dataset_reports_record_count_not_object_count(tmp_path):
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(fixture("cui-detector-dataset-v2")), encoding="utf-8")
    summary = validate_file(path, schema_version="cui-detector-dataset-v2")
    assert summary["object_count"] == 1
    assert summary["record_count"] == len(fixture("cui-detector-dataset-v2")["records"])
    batch = summarize_contract_records(
        [fixture("cui-citation-assertion-v1")] * 2, schema_version="cui-citation-assertion-v1"
    )
    assert batch["object_count"] == batch["record_count"] == 2


def test_jsonl_contract_batches_and_loader_errors(tmp_path):
    batch = tmp_path / "assertions.jsonl"
    batch.write_text(json.dumps(fixture("cui-citation-assertion-v1")) + "\n\n", encoding="utf-8")
    summary = validate_file(batch, schema_version="cui-citation-assertion-v1")
    assert summary["object_count"] == 1
    for content, message in (
        ("{broken\n", "invalid JSONL line 1"),
        ("[1]\n", "not an object"),
        ("\n", "no objects"),
    ):
        batch.write_text(content, encoding="utf-8")
        with pytest.raises(ContractValidationError, match=message):
            load_json_records(batch)
    scalar = tmp_path / "scalar.json"
    scalar.write_text("3", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="object or non-empty object array"):
        load_json_records(scalar)
    with pytest.raises(ContractValidationError, match="cannot read JSONL"):
        load_json_records(tmp_path / "missing.jsonl")
    with pytest.raises(ContractValidationError, match="must not be empty"):
        validate_contract_records([], schema_version="cui-codex-case-v1")


def test_contract_validation_rejects_trace_and_unknown_schemas():
    payload = {"schema_version": "cui-trace-v2"}
    with pytest.raises(ContractValidationError, match="unsupported public schema"):
        validate_contract(payload)
    with pytest.raises(ContractValidationError, match="schema_version is required"):
        validate_contract({})
    with pytest.raises(ContractValidationError, match="does not match requested"):
        validate_contract(fixture("cui-codex-case-v1"), schema_version="cui-source-manifest-v1")
    with pytest.raises(ContractValidationError, match="one JSON object"):
        validate_contract([])  # type: ignore[arg-type]
    with pytest.raises(ContractValidationError, match="unsupported public schema"):
        validate_file(Path("unused.json"), schema_version="cui-nope-v1")
    with pytest.raises(ContractValidationError, match="--for-write"):
        validate_file(Path("unused.json"), schema_version="cui-trace-v2", for_write=True)
    with pytest.raises(ContractValidationError, match="unsupported trace schema"):
        validate_trace_file(Path("unused.json"), schema_version="cui-codex-case-v1")


def test_browser_trace_jsonl_export_is_validated_like_json(tmp_path):
    trace = json.loads((EXAMPLES / "traces" / "minimal-session-v2.json").read_text("utf-8"))
    lines = [
        {
            "record_type": "session",
            "schema_version": "cui-trace-v2",
            "metadata": trace["metadata"],
            "summary": trace["summary"],
        },
        *({"record_type": "event", **event} for event in trace["events"]),
    ]
    path = tmp_path / "trace.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    summary = validate_trace_file(path, schema_version="cui-trace-v2")
    assert summary["record_count"] == 1
    other = tmp_path / "trace.csv"
    other.write_text("x", encoding="utf-8")
    with pytest.raises(ContractValidationError, match=".json or .jsonl"):
        validate_trace_file(other, schema_version="cui-trace-v2")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="cannot read trace"):
        validate_trace_file(broken, schema_version="cui-trace-v2")


def test_trace_runtime_rules_apply_after_the_schema(tmp_path):
    trace = json.loads((EXAMPLES / "traces" / "minimal-session-v2.json").read_text("utf-8"))
    trace["summary"]["by_type"] = {"navigation": 2}
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace), encoding="utf-8")
    with pytest.raises(ContractValidationError, match="runtime validation failed"):
        validate_trace_file(path, schema_version="cui-trace-v2")


def test_module_main_exit_codes(tmp_path, capsys):
    example = EXAMPLES / "detector" / "synthetic-dataset.json"
    assert main(["--schema", "cui-detector-dataset-v2", "--input", str(example)]) == 0
    assert json.loads(capsys.readouterr().out)["record_count"] == 18
    assert main(["--schema", "cui-codex-case-v1", "--input", str(example)]) == 1
    assert "error:" in capsys.readouterr().err


def _synthetic_dataset() -> dict:
    return json.loads((EXAMPLES / "detector" / "synthetic-dataset.json").read_text("utf-8"))


def test_detector_dataset_v2_rejects_duplicates_and_a_tampered_audit(tmp_path):
    # ``analyze`` rejects these; ``validate`` must not report them as passing.
    validate_contract(_synthetic_dataset())
    duplicated = _synthetic_dataset()
    duplicated["records"].append(copy.deepcopy(duplicated["records"][0]))
    with pytest.raises(ContractValidationError, match="duplicate record_ref"):
        validate_contract(duplicated)
    tampered = _synthetic_dataset()
    tampered["audit"]["dataset_sha256"] = "0" * 64
    with pytest.raises(ContractValidationError, match="audit differs from revalidation"):
        validate_contract(tampered)
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ContractValidationError, match="audit differs from revalidation"):
        validate_file(path, schema_version="cui-detector-dataset-v2")
    # A stored audit is recomputed with the full runtime gate.
    two_tasks = _synthetic_dataset()
    two_tasks["records"] = [
        row for row in two_tasks["records"] if row["task_group"] != "synthetic-task-3"
    ]
    with pytest.raises(ContractValidationError, match="at least 3 tasks"):
        validate_contract(two_tasks)
    formal = _synthetic_dataset()
    del formal["audit"]
    for row in formal["records"]:
        row["evidence_class"] = "formal_trace"
        row["participant_scope"] = "single_self_participant"
    with pytest.raises(ContractValidationError, match="immutable-ledger provenance"):
        validate_contract(formal)


def test_jsonl_loader_splits_only_on_newlines(tmp_path):
    # U+2028/U+2029/U+0085 are legal raw characters inside JSON strings and
    # the trace reader keeps them; str.splitlines() would cut the record.
    batch = tmp_path / "records.jsonl"
    record = {"note": "pasted line one\u2028line two\u2029end\x85tail"}
    batch.write_text(json.dumps(record, ensure_ascii=False) + "\r\n", encoding="utf-8")
    assert load_json_records(batch) == [record]
