from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("requests", reason="needs requests from the research extra")

from research.auxiliary_data.paths import EVIDENCE_DIR
from research.auxiliary_data.tracks import (
    _timestamp_audit,
    evaluate_molmoweb_domain_held_out,
    molmoweb_status,
    parse_molmoweb_trajectory,
)


def trajectory(*, timestamp: float | None = None, action: str = "click") -> str:
    step = {
        "other_obs": {"url": "https://www.example.com/path"},
        "action": {
            "action_str": f"{action}(x=1)",
            "action_output": {"action_name": action, "action": {"x": 1}},
        },
    }
    if timestamp is not None:
        step["action_timestamp"] = timestamp
    return json.dumps({"1": step})


def test_trajectory_parser_discards_timestamp_values() -> None:
    parsed = parse_molmoweb_trajectory(trajectory(timestamp=1_769_115_342.489504))
    assert parsed["action_sequence"] == "click"
    assert parsed["domain"] == "example.com"
    assert parsed["timestamp_fields"] == ["action_timestamp"]
    assert "1769115342" not in json.dumps(parsed)


def test_trajectory_parser_verifies_absence_when_no_timestamp_key() -> None:
    parsed = parse_molmoweb_trajectory(trajectory())
    assert parsed["timestamp_fields"] == []


def _audit(human_fields: list[str], synthetic_fields: list[str]) -> dict[str, Any]:
    audit: dict[str, Any] = {
        "human": _timestamp_audit([{"timestamp_fields": human_fields}]),
        "synthetic": _timestamp_audit([{"timestamp_fields": synthetic_fields}]),
    }
    audit["both_sources_verified_timestamp_free"] = bool(
        audit["human"]["released_rows_verified_timestamp_free"]
        and audit["synthetic"]["released_rows_verified_timestamp_free"]
    )
    return audit


def test_status_follows_the_timestamp_audit_outcome() -> None:
    assert molmoweb_status(_audit([], [])) == "complete"
    assert molmoweb_status(_audit([], ["action_timestamp"])) == (
        "complete_with_timestamp_absence_check_failed"
    )
    assert molmoweb_status(_audit(["action_timestamp"], [])) == (
        "complete_with_timestamp_absence_check_failed"
    )
    assert molmoweb_status({}) == "complete_with_timestamp_absence_check_failed"


def test_archived_status_is_consistent_with_its_audit() -> None:
    archived = json.loads((EVIDENCE_DIR / "molmoweb_metrics.json").read_text(encoding="utf-8"))
    assert archived["status"] == molmoweb_status(archived["timestamp_audit"])


def test_domain_held_out_models_have_zero_domain_overlap() -> None:
    pytest.importorskip("sklearn", reason="needs scikit-learn from the research extra")
    rows = []
    for domain_index in range(12):
        domain = f"domain-{domain_index}.example"
        for repetition in range(3):
            rows.append(
                {
                    "label": "human",
                    "domain": domain,
                    "action_sequence": "click scroll click" if repetition else "click scroll",
                }
            )
            rows.append(
                {
                    "label": "synthetic",
                    "domain": domain,
                    "action_sequence": "goto type submit" if repetition else "goto type",
                }
            )
    result = evaluate_molmoweb_domain_held_out(rows)
    assert result["domain_overlap_in_every_fold"] == 0
    assert all(fold["domain_overlap_count"] == 0 for fold in result["folds"])
    assert result["action_sequence_model"]["metrics"]["balanced_accuracy"] > 0.9
    assert 0.0 <= result["domain_only_baseline"]["metrics"]["balanced_accuracy"] <= 1.0
