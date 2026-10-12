from __future__ import annotations

import json
from pathlib import Path

from research.fp_agent import validate_config as validate_retrospective

HISTORICAL_SHA256 = "1a69495e8f4202002cd08cf25b140df183916566964ee729f6bc7588653ca6ee"


def _candidate(tmp_path, monkeypatch, mutate) -> Path:
    payload = json.loads(validate_retrospective.CONFIG.read_text(encoding="utf-8"))
    mutate(payload)
    candidate = tmp_path / "retrospective-study.json"
    candidate.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(validate_retrospective, "CONFIG", candidate)
    return candidate


def test_canonical_retrospective_config_is_valid():
    assert validate_retrospective.validate() == []
    assert validate_retrospective.main([]) == 0


def test_retrospective_validator_rejects_formal_claim_mutation(monkeypatch, tmp_path):
    candidate = _candidate(
        tmp_path, monkeypatch, lambda payload: payload.update(formal_claim_permitted=True)
    )
    assert "never permit a formal claim" in " ".join(validate_retrospective.validate(candidate))


def test_retrospective_validator_binds_unchanged_formal_bytes(monkeypatch, tmp_path):
    candidate = _candidate(
        tmp_path,
        monkeypatch,
        lambda payload: payload["historical_formal_config"].update(sha256="0" * 64),
    )
    assert "historical formal config bytes changed" in validate_retrospective.validate(candidate)


def test_retrospective_validator_pins_the_source_commit(monkeypatch, tmp_path):
    candidate = _candidate(
        tmp_path,
        monkeypatch,
        lambda payload: payload["historical_formal_config"].update(source_commit="0" * 40),
    )
    assert "historical formal config source commit changed" in validate_retrospective.validate(
        candidate
    )


def test_historical_binding_survives_an_edited_live_formal_config(monkeypatch, tmp_path):
    edited = tmp_path / "formal-study.json"
    edited.write_text('{"schema_version": "edited"}\n', encoding="utf-8")
    monkeypatch.setattr(validate_retrospective, "FORMAL_CONFIG", edited)
    assert validate_retrospective.historical_formal_binding(HISTORICAL_SHA256) == (
        "archived_capsule_record"
    )
    assert validate_retrospective.validate() == []


def test_historical_binding_fails_closed_without_bytes_or_archive(monkeypatch, tmp_path):
    edited = tmp_path / "formal-study.json"
    edited.write_text('{"schema_version": "edited"}\n', encoding="utf-8")
    monkeypatch.setattr(validate_retrospective, "FORMAL_CONFIG", edited)
    monkeypatch.setattr(validate_retrospective, "ARCHIVED_CAPSULE", tmp_path / "absent.json")
    assert validate_retrospective.historical_formal_binding(HISTORICAL_SHA256) is None
    assert "historical formal config bytes changed" in validate_retrospective.validate()


def test_live_formal_bytes_bind_directly(monkeypatch, tmp_path):
    live = tmp_path / "formal-study.json"
    live.write_text("frozen\n", encoding="utf-8")
    monkeypatch.setattr(validate_retrospective, "FORMAL_CONFIG", live)
    digest = validate_retrospective._sha256(live)
    assert validate_retrospective.historical_formal_binding(digest) == "live_config_bytes"
