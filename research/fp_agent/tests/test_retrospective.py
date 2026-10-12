from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("numpy", reason="needs numpy from the research extra")
pytest.importorskip("sklearn", reason="needs scikit-learn from the research extra")

from computer_use_agent.detector.contract import FEATURE_ALLOWLIST
from computer_use_agent.features import FEATURE_NAMES
from research.fp_agent import retrospective_evaluation as evaluation
from research.fp_agent.paths import (
    EVIDENCE_DIR,
    OSF_PROJECT_URL,
    PACKAGE_DIR,
)
from research.fp_agent.retrospective_evaluation import _matrix, load_records, loto_folds
from research.fp_agent.retrospective_features import (
    compact_event_to_detector,
    feature_record,
    iter_raw_trials,
    trial_events,
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tiny_trial(*, task_name: str = "Forums participant-1") -> dict[str, Any]:
    frames = [
        ["mm", "discard-me", 0, 0, 0],
        ["mm", "discard-me", 3, 4, 100],
        ["kd", "discard-me", "a", False, 200],
        ["ku", "discard-me", "a", False, 250],
        ["kd", "discard-me", "Backspace", False, 300],
        ["ku", "discard-me", "Backspace", False, 350],
        ["md", "discard-me", 0, 3, 4, 400],
        ["mu", "discard-me", 0, 3, 4, 450],
    ]
    return {
        "fpjs_data": [],
        "behavioral_data": [
            {"req_headers": "private", "req_body": json.dumps({"eventFrames": frames})}
        ],
        "source": {"task_name": task_name, "trial_index": 7},
    }


def synthetic_records(
    groups_per_class: int = 6, rows_per_group: int = 2
) -> list[dict[str, Any]]:
    """Deterministic 23-feature rows: three tasks, one label per group, no cross-task groups."""

    rng = random.Random(7)
    records: list[dict[str, Any]] = []
    for task in ("Flight-booking", "Forums", "Shopping"):
        for label in (0, 1):
            for group_index in range(groups_per_class):
                group = f"{task}-{label}-{group_index}"
                for row_index in range(rows_per_group):
                    features = {
                        name: rng.gauss(0.8 * label, 1.0) for name in FEATURE_ALLOWLIST
                    }
                    records.append(
                        {
                            "family": "Human" if label == 0 else f"Agent {group_index % 7}",
                            "label": label,
                            "task": task,
                            "group": group,
                            "features": features,
                            "record_ref": f"{group}-{row_index}",
                        }
                    )
    return records


def test_tiny_trajectory_feature_semantics_and_no_leakage() -> None:
    row = feature_record("Human", tiny_trial())
    assert tuple(row["features"]) == FEATURE_NAMES
    expected = {
        "event_count": 8.0,
        "duration_ms": 450.0,
        "event_rate_hz": 17.77777778,
        "inter_event_median_ms": 50.0,
        "inter_event_p90_ms": 100.0,
        "inter_event_cv": 0.35136418,
        "inter_event_burstiness": -0.47998595,
        "time_to_first_input_ms": 0.0,
        "pointer_path_px": 5.0,
        "pointer_speed_px_s": 11.11111111,
        "pointer_straightness": 1.0,
        "pointer_turning_entropy": 0.0,
        "click_dwell_median_ms": 50.0,
        "pre_click_movement_median_px": 5.0,
        "inter_key_median_ms": 100.0,
        "key_hold_median_ms": 50.0,
        "correction_ratio": 0.5,
        "focus_transition_count": 0.0,
        "event_type_entropy": 0.96902226,
        "missing_pointer": 0.0,
        "missing_keyboard": 0.0,
        "missing_click_dwell": 0.0,
        "missing_focus": 1.0,
    }
    assert row["features"] == pytest.approx(expected)
    rendered = json.dumps(row)
    assert "discard-me" not in rendered
    assert '"a"' not in rendered
    assert "Backspace" not in rendered


def test_inter_key_is_keydown_to_keydown_not_keyup_to_keydown() -> None:
    events = trial_events(tiny_trial())
    downs = [event["t_session_ms"] for event in events if event["type"] == "keydown"]
    assert downs == [200.0, 300.0]
    assert feature_record("Human", tiny_trial())["features"]["inter_key_median_ms"] == 100.0


def test_stream_parser_yields_individual_trials(tmp_path) -> None:
    pytest.importorskip("ijson", reason="needs ijson from the research extra")
    path = tmp_path / "raw.json"
    path.write_text(json.dumps({"Human": [tiny_trial(), tiny_trial()]}), encoding="utf-8")
    assert [family for family, _ in iter_raw_trials(path)] == ["Human", "Human"]


def test_model_matrix_is_allowlist_only() -> None:
    row = feature_record("Human", tiny_trial())
    row["actor_label"] = "should-never-enter"
    matrix = _matrix([row], FEATURE_NAMES)
    assert matrix.shape == (1, 23)
    with pytest.raises(ValueError):
        _matrix([row], [*FEATURE_NAMES, "actor_label"])


def test_loto_split_is_deterministic_and_disjoint() -> None:
    records = []
    for task in ("Flight-booking", "Forums", "Shopping"):
        for label in (0, 1):
            records.append(
                {
                    "task": task,
                    "label": label,
                    "group": f"{task}-{label}",
                    "record_ref": f"{task}-{label}",
                }
            )
    first = loto_folds(records)
    second = loto_folds(records)
    assert [fold[0] for fold in first] == [fold[0] for fold in second]
    for task, train, test in first:
        assert {row["task"] for row in test} == {task}
        assert task not in {row["task"] for row in train}
        assert {row["group"] for row in train}.isdisjoint(row["group"] for row in test)


def test_loto_purges_a_group_seen_in_the_held_out_task() -> None:
    records = [
        {"task": "Flight-booking", "label": 0, "group": "shared", "record_ref": "a"},
        {"task": "Forums", "label": 0, "group": "shared", "record_ref": "b"},
        {"task": "Forums", "label": 1, "group": "forums-only", "record_ref": "c"},
        {"task": "Shopping", "label": 1, "group": "shopping-only", "record_ref": "d"},
    ]
    flight = next(fold for fold in loto_folds(records) if fold[0] == "Flight-booking")
    assert {row["record_ref"] for row in flight[1]} == {"c", "d"}
    assert {row["record_ref"] for row in flight[2]} == {"a"}
    assert {row["group"] for row in flight[1]}.isdisjoint(row["group"] for row in flight[2])


def test_compact_event_rejects_unknown_event() -> None:
    assert compact_event_to_detector(["unknown", 1], 1) is None


def test_legacy_compact_events_are_not_silently_dropped() -> None:
    assert compact_event_to_detector(["mm", 3, 4, 10], 1)["pointer"] == {"x": 3.0, "y": 4.0}
    key = compact_event_to_detector(["kd", "Backspace", 20], 2)
    assert key["type"] == "keydown"
    assert key["key_class"] == "correction"
    assert key["keyboard"]["repeat"] is False


def test_derived_cache_without_extraction_ledger_fails_closed(tmp_path) -> None:
    path = tmp_path / "features_23.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="ledger"):
        load_records(path)


def test_protocol_calibration_fields_drive_the_nested_calibration() -> None:
    records = synthetic_records()
    folds = loto_folds(records)
    for splits, bins in ((2, 5), (3, 10)):
        result = evaluation.evaluate_folds(
            records, folds, calibration_splits=splits, calibration_bins=bins
        )
        assert result["failed_fold_count"] == 0
        for fold in result["folds"]:
            assert len(fold["calibration"]["folds"]) == splits
            assert len(fold["metrics"]["calibration_curve"]) <= bins
        assert len(result["aggregate"]["calibration_curve"]) <= bins
    defaults = evaluation.Protocol()
    assert (defaults.calibration_splits, defaults.calibration_bins) == (3, 10)


def test_calibration_split_count_is_validated() -> None:
    records = synthetic_records(groups_per_class=1)
    _, train, test = loto_folds(records)[0]
    with pytest.raises(ValueError, match="at least two splits"):
        evaluation._nested_probabilities(train, test, FEATURE_ALLOWLIST, "logistic", 1, 1)
    with pytest.raises(ValueError, match="at least 5 training groups"):
        evaluation._nested_probabilities(train, test, FEATURE_ALLOWLIST, "logistic", 1, 5)


def test_permutation_workers_receive_the_protocol_calibration(monkeypatch) -> None:
    for name in (
        "_PERMUTATION_RECORDS",
        "_PERMUTATION_SEED",
        "_PERMUTATION_CALIBRATION_SPLITS",
        "_PERMUTATION_CALIBRATION_BINS",
    ):
        monkeypatch.setattr(evaluation, name, getattr(evaluation, name))  # restored afterwards
    evaluation._initialize_permutation_worker(list(synthetic_records()), 11, 2, 4)
    assert evaluation._PERMUTATION_CALIBRATION_SPLITS == 2
    assert evaluation._PERMUTATION_CALIBRATION_BINS == 4
    assert isinstance(evaluation._permutation_worker(0), float)


def test_group_bootstrap_uses_the_protocol_bins() -> None:
    records = synthetic_records()
    primary = evaluation.evaluate_folds(records, loto_folds(records), calibration_bins=4)
    bootstrap = evaluation.group_bootstrap(primary["predictions"], 5, 3, bins=4)
    assert bootstrap["repetitions"] == 5
    assert set(bootstrap["intervals_95"]) >= {"balanced_accuracy", "ece"}


def test_completed_result_is_bound_to_the_locked_full_protocol() -> None:
    result_path = EVIDENCE_DIR / "fp_agent_retrospective.json"
    status = json.loads(
        (EVIDENCE_DIR / "fp_agent_retrospective_status.json").read_text(encoding="utf-8")
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert status == {
        "status": "complete",
        "output": result_path.name,
        "output_sha256": file_sha256(result_path),
    }
    assert result["protocol"]["bootstrap_repetitions"] == 2000
    assert result["protocol"]["calibration_splits"] == evaluation.DEFAULT_CALIBRATION_SPLITS
    assert result["protocol"]["calibration_bins"] == evaluation.DEFAULT_CALIBRATION_BINS
    assert result["permutation"]["requested"] == 1000
    assert result["permutation"]["successful"] == 1000
    assert result["permutation"]["failed"] == 0
    for section in (
        "primary_3_fold_loto_logistic",
        "secondary_3_fold_loto_xgboost",
        "seven_fold_agent_family_logistic",
    ):
        assert result[section]["failed_fold_count"] == 0
        for fold in result[section]["folds"]:
            assert fold["split_audit"]["group_overlap"] == 0
            assert len(fold["calibration"]["folds"]) == evaluation.DEFAULT_CALIBRATION_SPLITS
            assert all(inner["group_overlap"] == 0 for inner in fold["calibration"]["folds"])
    assert result["split_audit"]["outer_policy"] == (
        "held-out task plus participant/trial-group purge"
    )
    assert result["split_audit"]["multi_task_groups"] > 0
    assert all(fold["group_overlap"] == 0 for fold in result["split_audit"]["task_folds"])
    assert result["permutation"]["unit"] == "participant_or_trial_group"
    assert result["released_80_20_reproduction"]["dataset"]["source"].startswith(OSF_PROJECT_URL)
    environment = result["execution_environment"]
    assert environment["python"] == "3.12.3"
    assert environment["isolated_python"] is True
    assert environment["packages"] == {
        "ijson": "3.4.0.post0",
        "numpy": "2.4.4",
        "scikit-learn": "1.8.0",
        "scipy": "1.18.0",
        "xgboost": "3.2.0",
    }
    assert environment["requirements_lock_sha256"] == file_sha256(PACKAGE_DIR / "requirements.lock")
