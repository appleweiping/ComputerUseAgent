from __future__ import annotations

import copy
import json
import textwrap
from collections.abc import Mapping
from typing import Any

import pytest

from computer_use_agent import cli
from computer_use_agent.detector import provenance
from computer_use_agent.detector.bundle import write_bundle
from computer_use_agent.detector.contract import DATASET_SCHEMA, validate_dataset
from computer_use_agent.detector.evaluation import EvaluationConfig
from computer_use_agent.detector.pipeline import load_dataset_payload, run_pipeline
from computer_use_agent.detector.provenance import (
    GENERIC_ANALYSIS_PROVENANCE_SCHEMA,
    ProvenanceError,
    get_provenance_registration,
    get_provenance_validator,
    load_provenance_modules,
    register_provenance_validator,
    registered_source_stages,
    unregister_provenance_validator,
)
from computer_use_agent.study.policy import FormalStudyError

pytestmark = pytest.mark.usefixtures("isolated_provenance_registry")

FAST = EvaluationConfig(
    epochs=2, calibration_epochs=2, bootstrap_repetitions=1, permutation_repetitions=1
)
STAGE = "fixture-stage"


def _formal(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = copy.deepcopy(records)
    for row in rows:
        row["evidence_class"] = "formal_trace"
        row["participant_scope"] = "single_self_participant"
    return rows


def _provenance(**extra: Any) -> dict[str, Any]:
    return {"schema_version": "fixture-provenance-v1", "source_stage": STAGE, **extra}


class _Recorder:
    def __init__(self, *, reject: str | None = None) -> None:
        self.payloads: list[Mapping[str, Any]] = []
        self.reject = reject

    def __call__(self, payload: Mapping[str, Any]) -> None:
        self.payloads.append(payload)
        if self.reject:
            raise ValueError(self.reject)


class _CountingPolicy:
    def __init__(self, *, deny: bool = False) -> None:
        self.calls = 0
        self.deny = deny

    def authorize(self, *, use: str, participant_scope: str) -> None:
        self.calls += 1
        if self.deny:
            raise FormalStudyError("fixture policy denies formal human data")


def test_registry_round_trip_and_conflicts():
    validator = _Recorder()
    register_provenance_validator(STAGE, validator)
    assert get_provenance_validator(STAGE) is validator
    assert STAGE in registered_source_stages()
    # Re-registering the same callable (e.g. on module re-import) is allowed.
    register_provenance_validator(STAGE, validator)

    def other(payload: Mapping[str, Any]) -> None:
        return None

    with pytest.raises(ProvenanceError, match="already registered"):
        register_provenance_validator(STAGE, other)
    unregister_provenance_validator(STAGE)
    assert get_provenance_validator(STAGE) is None
    assert get_provenance_registration(STAGE) is None
    unregister_provenance_validator(STAGE)  # unknown stages are ignored


@pytest.mark.parametrize("stage", ["", "Upper", "has space", 7, "x" * 65])
def test_invalid_stage_names_are_rejected(stage):
    with pytest.raises(ProvenanceError, match="source_stage"):
        register_provenance_validator(stage, _Recorder())


def test_non_callable_validator_and_hooks_are_rejected():
    with pytest.raises(ProvenanceError, match="callable"):
        register_provenance_validator(STAGE, "not callable")  # type: ignore[arg-type]
    with pytest.raises(ProvenanceError, match="hooks must be callable"):
        register_provenance_validator(
            STAGE,
            _Recorder(),
            analysis_provenance="nope",  # type: ignore[arg-type]
        )


def test_load_provenance_modules_imports_and_registers(tmp_path, monkeypatch):
    package = tmp_path / "fixture_provenance_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "stage.py").write_text(
        textwrap.dedent(
            """
            from computer_use_agent.detector.provenance import register_provenance_validator

            def validate(payload):
                if payload["provenance"].get("ok") is not True:
                    raise ValueError("fixture stage rejects this dataset")

            register_provenance_validator("fixture-module-stage", validate)
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    load_provenance_modules(["fixture_provenance_pkg.stage"])
    assert get_provenance_validator("fixture-module-stage") is not None
    with pytest.raises(ProvenanceError, match="cannot import"):
        load_provenance_modules(["fixture_provenance_pkg.missing"])
    with pytest.raises(ProvenanceError, match="invalid provenance module name"):
        load_provenance_modules(["not a module"])


def test_dataset_with_unregistered_stage_fails_closed(tmp_path, synthetic_records):
    path = tmp_path / "dataset.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": DATASET_SCHEMA,
                "records": synthetic_records,
                "provenance": _provenance(),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProvenanceError, match="no provenance validator is registered"):
        load_dataset_payload(path)
    with pytest.raises(ProvenanceError, match="no provenance validator is registered"):
        run_pipeline(synthetic_records, config=FAST, dataset_provenance=_provenance())


@pytest.mark.parametrize(
    "bad",
    ["scienceboard", {"source_stage": ""}, {"source_stage": "BAD STAGE"}, {}],
)
def test_malformed_provenance_fails_closed(synthetic_records, bad):
    with pytest.raises(ValueError):
        run_pipeline(synthetic_records, config=FAST, dataset_provenance=bad)


def test_registered_validator_receives_a_detached_complete_payload(tmp_path, synthetic_records):
    validator = _Recorder()
    register_provenance_validator(STAGE, validator)
    payload = {
        "schema_version": DATASET_SCHEMA,
        "records": synthetic_records,
        "audit": validate_dataset(synthetic_records),
        "provenance": _provenance(),
    }
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    records, loaded_provenance = load_dataset_payload(path)
    assert records == synthetic_records
    assert loaded_provenance == _provenance()
    received = validator.payloads[-1]
    assert received == payload
    assert received is not payload
    validator.reject = "fixture rejection"
    with pytest.raises(ValueError, match="fixture rejection"):
        load_dataset_payload(path)


def test_formal_human_rows_need_a_registered_stage(synthetic_records):
    formal = _formal(synthetic_records)
    with pytest.raises(ValueError, match="immutable-ledger provenance"):
        run_pipeline(formal, config=FAST)
    with pytest.raises(ValueError, match="denied by default"):
        validate_dataset(formal)


@pytest.mark.parametrize("scope", ["single_self_participant", "approved_multi_participant"])
def test_stage_validator_without_policy_hook_denies_formal_rows(synthetic_records, scope):
    # Accepting provenance is not a human-research authorization: a stage
    # registered without a human_data_policy hook (the documented minimal
    # example) must not open formal human data.
    validator = _Recorder()
    register_provenance_validator(STAGE, validator)
    formal = _formal(synthetic_records)
    for row in formal:
        row["participant_scope"] = scope
    with pytest.raises(ValueError, match="denied by default"):
        run_pipeline(formal, config=FAST, dataset_provenance=_provenance())
    # The validator still ran; only the human-data authorization failed.
    assert validator.payloads


def test_stage_validator_without_policy_hook_accepts_synthetic_rows(synthetic_records):
    register_provenance_validator(STAGE, _Recorder())
    result = run_pipeline(synthetic_records, config=FAST, dataset_provenance=_provenance())
    assert result["source_provenance"]["source_stage"] == STAGE


def test_stage_policy_hook_authorizes_formal_rows_with_generic_binding(synthetic_records):
    validator = _Recorder()
    policy = _CountingPolicy()
    register_provenance_validator(STAGE, validator, human_data_policy=lambda _payload: policy)
    formal = _formal(synthetic_records)
    result = run_pipeline(formal, config=FAST, dataset_provenance=_provenance())
    payload = validator.payloads[-1]
    assert set(payload) == {"schema_version", "records", "audit", "provenance"}
    assert payload["audit"] == result["dataset"]
    binding = result["source_provenance"]
    assert binding["schema_version"] == GENERIC_ANALYSIS_PROVENANCE_SCHEMA
    assert binding["source_stage"] == STAGE
    assert binding["dataset_sha256"] == result["dataset"]["dataset_sha256"]
    assert binding["raw_sequences_embedded"] is False
    assert len(binding["provenance_sha256"]) == 64
    assert result["formal_claim_permitted"] is False
    assert result["claim_status"].startswith("Exploratory")
    assert any("single-participant" in item for item in result["limitations"])
    assert policy.calls > 0


def test_stage_hooks_control_policy_and_result_binding(tmp_path, synthetic_records):
    policy = _CountingPolicy()
    seen: list[Mapping[str, Any]] = []

    def binding(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        seen.append(payload)
        return {
            "schema_version": "fixture-binding-v1",
            "source_stage": STAGE,
            "study_ledger_integrity_sha256": "a" * 64,
            "raw_sequences_embedded": False,
        }

    register_provenance_validator(
        STAGE,
        _Recorder(),
        analysis_provenance=binding,
        human_data_policy=lambda _payload: policy,
    )
    formal = _formal(synthetic_records)
    result = run_pipeline(formal, config=FAST, dataset_provenance=_provenance())
    assert result["source_provenance"]["schema_version"] == "fixture-binding-v1"
    assert policy.calls > 3  # re-authorized inside every evaluation fold
    assert seen and seen[-1]["provenance"] == _provenance()
    write_bundle(result, tmp_path / "bundle")
    card = (tmp_path / "bundle" / "data_card.md").read_text(encoding="utf-8")
    assert "Source stage: `fixture-stage`" in card
    assert "Source ledger integrity SHA-256" in card


def test_stage_policy_denial_and_unsafe_bindings_fail_closed(synthetic_records):
    formal = _formal(synthetic_records)
    register_provenance_validator(
        STAGE, _Recorder(), human_data_policy=lambda _payload: _CountingPolicy(deny=True)
    )
    with pytest.raises(ValueError, match="not authorized"):
        run_pipeline(formal, config=FAST, dataset_provenance=_provenance())
    unregister_provenance_validator(STAGE)
    register_provenance_validator(
        STAGE,
        _Recorder(),
        analysis_provenance=lambda _payload: {
            "schema_version": "x",
            "raw_sequences_embedded": True,
        },
    )
    with pytest.raises(ProvenanceError, match="raw event sequences"):
        run_pipeline(synthetic_records, config=FAST, dataset_provenance=_provenance())
    unregister_provenance_validator(STAGE)
    register_provenance_validator(
        STAGE, _Recorder(), analysis_provenance=lambda _payload: {"no": "schema"}
    )
    with pytest.raises(ProvenanceError, match="schema_version"):
        run_pipeline(synthetic_records, config=FAST, dataset_provenance=_provenance())


def test_generic_binding_rejects_non_json_and_bad_provenance():
    with pytest.raises(ProvenanceError, match="provenance must be an object"):
        provenance.generic_analysis_provenance(
            {"provenance": None}, audit={"dataset_sha256": "0", "record_count": 0}
        )
    with pytest.raises(ProvenanceError, match="not plain JSON"):
        provenance._detached({"provenance": {"value": float("nan")}})


def test_cli_analyze_needs_the_provenance_module(tmp_path, monkeypatch, synthetic_records, capsys):
    package = tmp_path / "fixture_cli_stage.py"
    package.write_text(
        textwrap.dedent(
            """
            from computer_use_agent.detector.provenance import register_provenance_validator

            def validate(payload):
                return None

            register_provenance_validator("fixture-cli-stage", validate)
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": DATASET_SCHEMA,
                "records": synthetic_records,
                "audit": validate_dataset(synthetic_records),
                "provenance": {"source_stage": "fixture-cli-stage"},
            }
        ),
        encoding="utf-8",
    )
    common = ["--epochs", "2", "--bootstrap", "1", "--permutations", "1", "--profile", "smoke"]
    failed = cli.main(
        ["analyze", "--dataset", str(dataset), "--output-dir", str(tmp_path / "a"), *common]
    )
    assert failed == 1
    assert "no provenance validator is registered" in capsys.readouterr().err
    assert not (tmp_path / "a").exists()
    passed = cli.main(
        [
            "analyze",
            "--dataset",
            str(dataset),
            "--output-dir",
            str(tmp_path / "b"),
            "--provenance-module",
            "fixture_cli_stage",
            *common,
        ]
    )
    assert passed == 0
    summary = json.loads(capsys.readouterr().out)
    assert "result.json" in summary["hashes"]
    result = json.loads((tmp_path / "b" / "result.json").read_text(encoding="utf-8"))
    assert result["source_provenance"]["source_stage"] == "fixture-cli-stage"
