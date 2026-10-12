from __future__ import annotations

import itertools
import json

import pytest

from computer_use_agent.detector.reporting import (
    build_trajectory_projection,
    detector_descriptive_summary,
    trajectory_artifact_binding,
)
from computer_use_agent.viewer.privacy import (
    assert_privacy_safe_payload,
    sanitize_loaded_trace,
    sanitize_result,
)
from tests.fixtures.synthetic import make_records


def test_trace_projection_drops_raw_payload_and_redacts_failure():
    raw_secret = "typed-private-value"
    trace = sanitize_loaded_trace(
        display_name="paired human",
        metadata={
            "actor_type": "human",
            "task_id": "private-task-name",
            "capture_adapter": "browser_mv3",
            "session_id": "private-session-uuid",
            "participant_code": "person-1",
        },
        events=[
            {
                "type": "keydown",
                "t_session_ms": 10.0,
                "document_epoch": 0,
                "key": raw_secret,
                "code": "KeyQ",
                "url": "https://private.example/query?secret=yes",
            },
            {
                "type": "pointermove",
                "t_session_ms": 30.0,
                "document_epoch": 1,
                "pointer": {"x": 987, "y": 654},
                "selector": "#private",
            },
        ],
        terminal={
            "status": "capture_error",
            "duration_ms": 50.0,
            "capture_error": "failed at C:\\Users\\person\\secret.txt for person@example.org",
        },
        validator={
            "passed": False,
            "failure_reason": "See https://private.example/result and abcdefghijklmnopqrstuvwxyz123456",
        },
    )
    state = {
        "schema_version": "cui-local-view-v1",
        "privacy_mode": "projected",
        "traces": [trace],
        "detector_result": None,
    }
    assert_privacy_safe_payload(state)
    serialized = json.dumps(state)
    for secret in (
        raw_secret,
        "private-task-name",
        "private-session-uuid",
        "person-1",
        "987",
        "654",
        "private.example",
        "person@example.org",
    ):
        assert secret not in serialized
    assert trace["timeline"] == [
        {
            "index": 1,
            "relative_ms": 0.0,
            "type": "keydown",
            "modality": "keyboard",
            "document_epoch": 0,
        },
        {
            "index": 2,
            "relative_ms": 20.0,
            "type": "pointermove",
            "modality": "pointer",
            "document_epoch": 1,
        },
    ]


def test_detector_projection_omits_predictions_and_coefficients():
    descriptive = detector_descriptive_summary(make_records())
    trajectory = trajectory_artifact_binding(build_trajectory_projection(descriptive))
    payload = {
        "schema_version": "cui-detector-analysis-v2",
        "claim_status": "Synthetic pipeline check only",
        "formal_claim_permitted": False,
        "descriptive": descriptive,
        "trajectory_artifact": trajectory,
        "evaluation": {
            "primary_loto": {
                "aggregate": {
                    "count": 18,
                    "accuracy": 0.75,
                    "balanced_accuracy": 0.74,
                    "brier_score": 0.2,
                    "predictions": [{"session_id": "secret"}],
                }
            },
            "task_identification_negative_control": {
                "accuracy": 0.6,
                "chance_accuracy": 0.333,
            },
            "label_permutation": {
                "repetitions": 100,
                "p_value_one_sided": 0.04,
            },
            "task_cluster_bootstrap": {
                "intervals_95": {
                    "balanced_accuracy": {"lower": 0.5, "upper": 0.9},
                    "session_id": {"lower": "secret", "upper": "secret"},
                }
            },
        },
        "explanation": {"coefficient_summary": [{"feature": "x", "value": 9}]},
    }
    projected = sanitize_result(payload)
    assert projected is not None
    serialized = json.dumps(projected)
    assert "session_id" not in serialized
    assert "coefficient" not in serialized
    assert projected["bootstrap_intervals_95"]["balanced_accuracy"] == {
        "lower": 0.5,
        "upper": 0.9,
    }
    assert projected["task_descriptive"] == descriptive
    assert projected["trajectory_artifact"] == trajectory
    assert trajectory["privacy"]["raw_event_sequences_embedded"] is False


def test_large_timeline_is_deterministically_bounded_without_changing_counts():
    events = [
        {
            "type": "pointermove" if index else "focusin",
            "t_session_ms": float(index),
            "document_epoch": 0,
        }
        for index in range(3000)
    ]
    trace = sanitize_loaded_trace(
        display_name="large fixture",
        metadata={"actor_type": "agent", "capture_adapter": "browser_mv3"},
        events=events,
        terminal={"status": "complete", "duration_ms": 3000.0},
    )
    assert trace["event_count"] == 3000
    assert trace["timeline_display_count"] <= 2500
    assert trace["timeline_display_count"] == len(trace["timeline"])
    assert trace["timeline_sampling"] == "uniform_plus_first_event_type"
    assert {event["type"] for event in trace["timeline"]} == {
        "focusin",
        "pointermove",
    }
    assert trace["timeline"][0]["index"] == 1
    assert trace["timeline"][-1]["index"] == trace["event_count"]


@pytest.mark.parametrize("type_count", [1, 40, 400])
def test_sampling_keeps_the_session_end_and_every_first_event_type(type_count):
    # Many event types whose first occurrence falls off the uniform grid used
    # to push the selection over the cap and cut the end of the timeline.
    total = 10_000
    events = []
    for index in range(total):
        kind = f"type{index % type_count}" if index < type_count * 37 else "pointermove"
        events.append({"type": kind, "t_session_ms": float(index), "document_epoch": 0})
    trace = sanitize_loaded_trace(
        display_name="many types",
        metadata={"actor_type": "agent", "capture_adapter": "browser_mv3"},
        events=events,
        terminal={"status": "complete", "duration_ms": float(total - 1)},
    )
    timeline = trace["timeline"]
    assert len(timeline) <= 2500
    assert timeline[-1]["index"] == total
    assert timeline[-1]["relative_ms"] == trace["duration_ms"]
    assert {event["type"] for event in timeline} == {event["type"] for event in events}
    indices = [event["index"] for event in timeline]
    assert indices == sorted(set(indices))
    # The sample still covers the session evenly: no gap is much wider than
    # the uniform step.
    gaps = [after - before for before, after in itertools.pairwise(indices)]
    assert max(gaps) <= 2 * total / (2500 - type_count)


def test_task_aliases_match_the_detector_format():
    from computer_use_agent.viewer.privacy import viewer_task_aliases

    aliases = viewer_task_aliases(["b-task", "a-task", "b-task", "c-task"])
    assert aliases == {"a-task": "task-01", "b-task": "task-02", "c-task": "task-03"}
    descriptive = detector_descriptive_summary(make_records())
    assert list(descriptive["by_task"]) == ["task-01", "task-02", "task-03"]


def test_build_view_state_from_example_traces_and_a_real_result(tmp_path):
    from pathlib import Path

    import pytest

    from computer_use_agent.detector.bundle import write_bundle
    from computer_use_agent.detector.evaluation import EvaluationConfig
    from computer_use_agent.detector.pipeline import run_pipeline
    from computer_use_agent.viewer.privacy import build_view_state

    examples = Path(__file__).resolve().parents[2] / "examples" / "traces"
    result = run_pipeline(
        make_records(),
        config=EvaluationConfig(
            epochs=2, calibration_epochs=2, bootstrap_repetitions=2, permutation_repetitions=1
        ),
    )
    write_bundle(result, tmp_path / "bundle")
    validator = tmp_path / "validator.json"
    validator.write_text(json.dumps({"outcome": {"passed": True}}), encoding="utf-8")
    state = build_view_state(
        [("v2", examples / "minimal-session-v2.json"), ("v1", examples / "minimal-session.json")],
        validators={"v2": validator},
        result_path=tmp_path / "bundle" / "result.json",
    )
    assert state["schema_version"] == "cui-local-view-v1"
    task_refs = {trace["name"]: trace["task_ref"] for trace in state["traces"]}
    assert set(task_refs.values()) <= {"task-01", "task-02"}
    assert state["traces"][0]["validator"]["passed"] is True
    projected = state["detector_result"]
    assert set(projected["bootstrap_intervals_95"]) >= {"balanced_accuracy", "roc_auc"}
    assert list(projected["task_descriptive"]["by_task"]) == ["task-01", "task-02", "task-03"]
    assert projected["formal_claim_permitted"] is False
    serialized = json.dumps(state)
    assert "example-session" not in serialized and "miniwob" not in serialized
    with pytest.raises(ValueError, match="at least one"):
        build_view_state([])
    with pytest.raises(ValueError, match="eight"):
        build_view_state([(f"t{i}", examples / "minimal-session.json") for i in range(9)])
    with pytest.raises(ValueError, match="unique"):
        build_view_state(
            [
                ("same", examples / "minimal-session.json"),
                ("same", examples / "minimal-session.json"),
            ]
        )
    not_object = tmp_path / "list.json"
    not_object.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        build_view_state([("v1", examples / "minimal-session.json")], result_path=not_object)


def test_result_projection_rejects_other_schemas():
    import pytest

    assert sanitize_result(None) is None
    with pytest.raises(ValueError, match="only the detector analysis result"):
        sanitize_result({"schema_version": "other"})
