from __future__ import annotations

import json

import pytest

from computer_use_agent.detector.contract import validate_dataset
from computer_use_agent.detector.extract import (
    extract_manifest,
    main,
    trace_to_record,
    write_dataset,
)
from computer_use_agent.study.policy import FormalStudyError
from tests.fixtures.synthetic import late_start_browser_trace, write_trace_manifest


def test_trace_extraction_retains_hash_not_path_or_session(monkeypatch, tmp_path):
    trace = tmp_path / "private-human-trace.json"
    trace.write_text("private bytes", encoding="utf-8")

    def fake_load(_path):
        return (
            {
                "actor_type": "human",
                "task_id": "task-one",
                "session_id": "sensitive-session-id",
                "monotonic_origin_ms": 0.0,
            },
            [
                {
                    "event_id": "a",
                    "type": "keydown",
                    "t_session_ms": 10.0,
                    "key_class": "character",
                },
                {"event_id": "b", "type": "keyup", "t_session_ms": 30.0, "key_class": "character"},
            ],
            {"duration_ms": 50.0, "status": "complete"},
        )

    monkeypatch.setattr("computer_use_agent.detector.extract.load_trace", fake_load)
    record = trace_to_record(
        trace,
        label="human",
        task_group="task-one",
        evidence_class="synthetic_test_fixture",
        participant_scope="synthetic_no_participant",
    )
    serialized = json.dumps(record)
    assert str(trace) not in serialized
    assert "sensitive-session-id" not in serialized
    assert record["source_sha256"]
    assert record["features"]["event_count"] == 2.0


def test_trace_extraction_cross_checks_label(monkeypatch, tmp_path):
    trace = tmp_path / "trace.json"
    trace.write_text("bytes", encoding="utf-8")
    monkeypatch.setattr(
        "computer_use_agent.detector.extract.load_trace",
        lambda _path: (
            {"actor_type": "agent", "task_id": "task-one"},
            [],
            {"duration_ms": 0.0},
        ),
    )
    with pytest.raises(ValueError, match="label disagrees"):
        trace_to_record(
            trace,
            label="human",
            task_group="task-one",
            evidence_class="synthetic_test_fixture",
            participant_scope="synthetic_no_participant",
        )
    monkeypatch.setattr(
        "computer_use_agent.detector.extract.load_trace",
        lambda _path: (
            {"actor_type": "human", "task_id": "task-one"},
            [],
            {"duration_ms": 0.0},
        ),
    )
    with pytest.raises(ValueError, match="denied by default"):
        trace_to_record(
            trace,
            label="human",
            task_group="task-one",
            evidence_class="formal_trace",
            participant_scope="single_self_participant",
        )


def test_generic_manifest_cannot_self_assert_formal_provenance(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("not reached", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "cui-detector-extraction-manifest-v1",
                "traces": [
                    {
                        "trace": trace.name,
                        "label": "human",
                        "task_group": "task-one",
                        "agent_family": None,
                        "evidence_class": "formal_trace",
                        "participant_scope": "single_self_participant",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="immutable-ledger bridge"):
        extract_manifest(manifest)


class _AllowAll:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def authorize(self, *, use: str, participant_scope: str) -> None:
        self.calls.append((use, participant_scope))


class _DenyAll:
    def authorize(self, *, use: str, participant_scope: str) -> None:
        raise FormalStudyError("fixture policy denies")


def _formal_human(monkeypatch, tmp_path):
    trace = tmp_path / "trace.json"
    trace.write_text("bytes", encoding="utf-8")
    monkeypatch.setattr(
        "computer_use_agent.detector.extract.load_trace",
        lambda _path: ({"actor_type": "human", "task_id": "task-one"}, [], {"duration_ms": 0.0}),
    )
    return trace


def test_injected_policy_authorizes_or_denies_formal_human_traces(monkeypatch, tmp_path):
    trace = _formal_human(monkeypatch, tmp_path)
    policy = _AllowAll()
    record = trace_to_record(
        trace,
        label="human",
        task_group="task-one",
        evidence_class="formal_trace",
        participant_scope="single_self_participant",
        human_data_policy=policy,
    )
    assert record["evidence_class"] == "formal_trace"
    assert policy.calls == [("detector_training", "single_self_participant")]
    with pytest.raises(ValueError, match="not authorized: fixture policy denies"):
        trace_to_record(
            trace,
            label="human",
            task_group="task-one",
            evidence_class="formal_trace",
            participant_scope="single_self_participant",
            human_data_policy=_DenyAll(),
        )
    with pytest.raises(ValueError, match="either human_data_policy or human_research_config"):
        trace_to_record(
            trace,
            label="human",
            task_group="task-one",
            evidence_class="formal_trace",
            participant_scope="single_self_participant",
            human_data_policy=policy,
            human_research_config={"human_research_policy": {}},
        )


def test_manifest_extraction_end_to_end_is_path_free_and_atomic(tmp_path):
    manifest, traces = write_trace_manifest(tmp_path / "private")
    dataset = extract_manifest(manifest)
    assert dataset["audit"] == validate_dataset(dataset["records"])
    assert dataset["audit"]["record_count"] == len(traces) == 18
    serialized = json.dumps(dataset)
    assert str(tmp_path) not in serialized
    assert "synthetic-session-" not in serialized
    output = tmp_path / "out" / "dataset.json"
    output.parent.mkdir()
    output.write_text("previous", encoding="utf-8")
    assert main(["--manifest", str(manifest), "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == dataset
    assert [path.name for path in output.parent.iterdir()] == ["dataset.json"]


def test_failed_atomic_write_leaves_previous_dataset(tmp_path, monkeypatch):
    output = tmp_path / "dataset.json"
    output.write_text("previous", encoding="utf-8")

    def broken_replace(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("computer_use_agent.detector.extract.os.replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        write_dataset({"schema_version": "x", "records": []}, output)
    assert output.read_text(encoding="utf-8") == "previous"
    assert [path.name for path in tmp_path.iterdir()] == ["dataset.json"]


def test_extract_cli_reports_errors_without_traceback(tmp_path, capsys):
    missing = tmp_path / "missing.json"
    assert main(["--manifest", str(missing), "--output", str(tmp_path / "out.json")]) == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("suffix", [".json", ".jsonl"])
def test_browser_trace_whose_first_event_is_after_session_start_extracts(tmp_path, suffix):
    # Real extension exports start recording before the first event, so the
    # summary span (last - first) is shorter than the session clock reaches.
    trace = late_start_browser_trace()
    path = tmp_path / f"late-start{suffix}"
    if suffix == ".json":
        path.write_text(json.dumps(trace), encoding="utf-8")
    else:
        rows = [
            {
                "record_type": "session",
                "schema_version": "cui-trace-v2",
                "session": trace["metadata"],
                "summary": trace["summary"],
            },
            *({"record_type": "event", **event} for event in trace["events"]),
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    record = trace_to_record(
        path,
        label="agent",
        task_group=trace["metadata"]["task_id"],
        agent_family="synthetic-agent",
        evidence_class="synthetic_test_fixture",
        participant_scope="synthetic_no_participant",
    )
    assert record["features"]["duration_ms"] == 6085.002
    assert record["features"]["event_count"] == 2.0
