"""Trace core tests: privacy boundary, JSONL I/O, v1 migration and validation.

Split from the former ``test_trace_and_calibration.py``; the calibration tests
live in ``tests/capture/test_calibration.py``.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from computer_use_agent.capture.x11.recorder import build_session_metadata
from computer_use_agent.trace.io import (
    TraceQuotaExceeded,
    TraceWriter,
    assert_v2_stream_integrity,
    load_trace,
    migrate_trace_v1_to_v2,
    recover_stale_partials,
    validate_event_stream,
)
from computer_use_agent.trace.privacy import (
    assert_privacy_safe_event,
    classify_keysym,
    sanitize_application_id,
)
from computer_use_agent.trace.validation import validate_browser_trace
from tests.capture.x11_fixtures import COMMIT, event, metadata
from tests.fixtures.synthetic import late_start_browser_trace

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = REPO_ROOT / "src" / "computer_use_agent" / "schemas"


def browser_example(version: int = 2) -> dict[str, Any]:
    name = "minimal-session-v2.json" if version == 2 else "minimal-session.json"
    payload: dict[str, Any] = json.loads(
        (REPO_ROOT / "examples" / "traces" / name).read_text(encoding="utf-8")
    )
    return payload


class PrivacyTests(unittest.TestCase):
    def test_json_schema_declares_closed_event_records(self) -> None:
        schema_path = SCHEMA_ROOT / "x11-trace-v2.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        event_schema = schema["$defs"]["event"]
        self.assertFalse(event_schema["additionalProperties"])
        self.assertIn("monotonic_ms", event_schema["required"])
        self.assertTrue(event_schema["allOf"])
        self.assertTrue(schema["$defs"]["session"]["properties"]["metadata"]["allOf"])

    def test_browser_v2_schema_is_plain_validator_and_offline_self_contained(self) -> None:
        schema_path = SCHEMA_ROOT / "cui-trace-v2.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        references: list[str] = []

        def collect(value: object) -> None:
            if isinstance(value, dict):
                if isinstance(value.get("$ref"), str):
                    references.append(value["$ref"])
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(schema)
        self.assertTrue(references)
        self.assertTrue(all(reference.startswith("#/") for reference in references))
        Draft202012Validator(schema).validate(browser_example(2))

    def test_key_classification_never_returns_content(self) -> None:
        self.assertEqual(classify_keysym("a"), "printable")
        self.assertEqual(classify_keysym("BackSpace"), "correction")
        self.assertEqual(classify_keysym("Return"), "editing_command")
        self.assertEqual(classify_keysym("Control_L"), "modifier")
        self.assertEqual(classify_keysym("F12"), "function")

    def test_content_bearing_fields_are_rejected_recursively(self) -> None:
        unsafe = event("e1", "keydown", 1)
        unsafe["keyboard"]["keysym"] = "a"
        with self.assertRaisesRegex(ValueError, "Privacy-forbidden"):
            assert_privacy_safe_event(unsafe)

    def test_application_id_is_bounded_and_title_like_punctuation_removed(self) -> None:
        sanitized = sanitize_application_id("KAlgebra – secret worksheet / answer")
        self.assertNotIn(" ", sanitized)
        self.assertNotIn("secret", sanitized)
        self.assertEqual(sanitized, "KAlgebra")
        self.assertLessEqual(len(sanitized), 64)


class TraceTests(unittest.TestCase):
    def metadata(self) -> dict[str, Any]:
        return metadata("human", "test-session")

    def test_append_only_trace_round_trip_and_closed_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            writer = TraceWriter(path, self.metadata())
            writer.append(event("event-1", "keydown", 1))
            writer.close(status="complete", stopped_at="2026-08-08T00:00:00+00:00")
            with self.assertRaises(RuntimeError):
                writer.append(event("event-2", "keyup", 2))
            metadata, events, terminal = load_trace(path)
            self.assertEqual(metadata["session_id"], "test-session")
            self.assertEqual(metadata["schema_version"], "cui-trace-v2")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_seq"], 1)
            self.assertIn("t_session_ms", events[0])
            self.assertEqual(terminal["status"], "complete")
            self.assertGreater(terminal["duration_ms"], 0)

    def test_finalize_promotes_one_atomic_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            writer = TraceWriter(path, self.metadata())
            self.assertFalse(path.exists())
            self.assertTrue(writer.partial_path.exists())
            writer.append(event("event-1", "keydown", 1))
            writer.close(status="complete", stopped_at="2026-08-08T00:00:00+00:00")
            self.assertTrue(path.exists())
            self.assertFalse(writer.partial_path.exists())

    def test_trace_quota_rejects_event_but_retains_terminal_counter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            writer = TraceWriter(path, self.metadata(), max_trace_bytes=1)
            with self.assertRaises(TraceQuotaExceeded):
                writer.append(event("event-1", "keydown", 1))
            writer.close(
                status="interrupted",
                stopped_at="2026-08-08T00:00:00+00:00",
                capture_error="TraceQuotaExceeded",
            )
            _, events, terminal = load_trace(path)
        self.assertEqual(events, [])
        self.assertEqual(terminal["quota_rejection_count"], 1)
        self.assertEqual(terminal["status"], "interrupted")

    def test_stale_partial_is_recovered_as_explicit_interrupted_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            writer = TraceWriter(path, self.metadata())
            writer.append(event("event-1", "keydown", 1))
            writer._handle.flush()  # simulate a process death after a durable event
            writer._handle.close()
            partial = writer.partial_path
            self.assertTrue(partial.exists())

            recovery = recover_stale_partials(path)
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0]["status"], "recovered_interrupted")
            self.assertFalse(partial.exists())
            recovered = Path(temp) / str(recovery[0]["recovered_name"])
            _, events, terminal = load_trace(recovered)

        self.assertEqual(len(events), 1)
        self.assertEqual(terminal["status"], "interrupted")
        self.assertEqual(terminal["capture_error"], "process_crash_recovered")
        self.assertEqual(terminal["event_seq_last"], 1)

    def test_v1_migration_is_deterministic_and_detects_document_reset(self) -> None:
        source_metadata = dict(self.metadata())
        source_metadata["schema_version"] = "cui-trace-v1"
        source_metadata.pop("timing_model")
        source_metadata.pop("document_epoch_count")
        source_events = [
            event("event-1", "pointermove", 10),
            event("event-2", "pointermove", 2),
        ]
        first = migrate_trace_v1_to_v2(source_metadata, source_events, {})
        second = migrate_trace_v1_to_v2(source_metadata, source_events, {})
        self.assertEqual(first, second)
        self.assertEqual([item["event_seq"] for item in first[1]], [1, 2])
        self.assertEqual([item["document_epoch"] for item in first[1]], [0, 1])
        self.assertGreater(first[1][1]["t_session_ms"], first[1][0]["t_session_ms"])
        assert_v2_stream_integrity(first[1])

    def test_v2_integrity_rejects_sequence_and_clock_corruption(self) -> None:
        source_metadata = dict(self.metadata())
        source_metadata["schema_version"] = "cui-trace-v1"
        source_metadata.pop("timing_model")
        source_metadata.pop("document_epoch_count")
        _, events, _ = migrate_trace_v1_to_v2(
            source_metadata,
            [event("event-1", "pointermove", 1), event("event-2", "pointermove", 2)],
            {},
        )
        events[1]["event_seq"] = 4
        events[1]["t_session_ms"] = events[0]["t_session_ms"]
        with self.assertRaisesRegex(ValueError, "Invalid TraceEnvelopeV2"):
            assert_v2_stream_integrity(events)

    def test_shared_reader_accepts_browser_jsonl_and_migrates_v1(self) -> None:
        source = browser_example(1)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "browser.jsonl"
            records = [
                {
                    "record_type": "session",
                    "schema_version": "cui-trace-v1",
                    "session": source["metadata"],
                    "summary": source["summary"],
                },
                {"record_type": "event", **source["events"][0]},
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            loaded_metadata, loaded_events, terminal = load_trace(path)
        self.assertEqual(loaded_metadata["schema_version"], "cui-trace-v2")
        self.assertEqual(loaded_events[0]["event_seq"], 1)
        self.assertEqual(terminal["status"], "complete")

    def test_browser_json_and_jsonl_have_the_same_full_v2_terminal(self) -> None:
        source = browser_example(2)
        with tempfile.TemporaryDirectory() as temp:
            json_path = Path(temp) / "browser.json"
            jsonl_path = Path(temp) / "browser.jsonl"
            json_path.write_text(json.dumps(source), encoding="utf-8")
            records = [
                {
                    "record_type": "session",
                    "schema_version": "cui-trace-v2",
                    "session": source["metadata"],
                    "summary": source["summary"],
                },
                *({"record_type": "event", **event_payload} for event_payload in source["events"]),
            ]
            jsonl_path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            _, _, json_terminal = load_trace(json_path)
            _, _, jsonl_terminal = load_trace(jsonl_path)

        self.assertEqual(json_terminal, jsonl_terminal)
        self.assertEqual(
            set(json_terminal),
            {
                "status",
                "stopped_at",
                "event_count",
                "capture_error",
                "duration_ms",
                "event_seq_last",
                "document_epoch_count",
                "dropped_event_count",
                "rejected_event_count",
                "flush_failure_count",
                "quota_rejection_count",
            },
        )
        self.assertEqual(json_terminal["event_seq_last"], 1)
        self.assertEqual(json_terminal["document_epoch_count"], 1)
        self.assertEqual(json_terminal["dropped_event_count"], 0)

    def test_browser_terminal_duration_is_measured_from_session_start(self) -> None:
        source = late_start_browser_trace()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "browser.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            _, events, terminal = load_trace(path)
        # summary.duration_ms keeps the extension's first-to-last span; the
        # terminal uses the session clock, like X11 traces.
        self.assertEqual(source["summary"]["duration_ms"], 6078.002)
        self.assertEqual(terminal["duration_ms"], 6085.002)
        self.assertEqual(terminal["duration_ms"], max(e["t_session_ms"] for e in events))

    def test_browser_reader_rejects_unknown_private_and_truncated_content(self) -> None:
        corruptions = []
        unknown = browser_example(2)
        unknown["events"][0]["unexpected"] = 1
        corruptions.append((unknown, "unexpected"))
        private = browser_example(2)
        private["events"][0]["input"] = {
            "input_type": "insertText",
            "data_length": 1,
            "is_composing": False,
            "value": "secret",
        }
        corruptions.append((private, "Privacy-forbidden"))
        truncated = browser_example(2)
        truncated["metadata"]["event_count"] = 2
        truncated["metadata"]["event_seq_last"] = 2
        corruptions.append((truncated, "event_count"))
        for payload, message in corruptions:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "browser.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    load_trace(path)

    def test_browser_jsonl_rejects_truncation_and_private_fields(self) -> None:
        for mutation, message in (("truncate", "event_count"), ("private", "Privacy-forbidden")):
            source = browser_example(2)
            event_record = {"record_type": "event", **source["events"][0]}
            if mutation == "truncate":
                event_records: list[dict[str, Any]] = []
            else:
                event_record["value"] = "secret"
                event_records = [event_record]
            records = [
                {
                    "record_type": "session",
                    "schema_version": "cui-trace-v2",
                    "session": source["metadata"],
                    "summary": source["summary"],
                },
                *event_records,
            ]
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "browser.jsonl"
                path.write_text(
                    "".join(json.dumps(record) + "\n" for record in records),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, message):
                    load_trace(path)

    def test_missing_x11_terminal_is_interrupted_even_with_recording_header(self) -> None:
        source_metadata = self.metadata()
        source_metadata["schema_version"] = "cui-trace-v1"
        source_metadata.pop("timing_model")
        source_metadata.pop("document_epoch_count")
        migrated_metadata, migrated_events, _ = migrate_trace_v1_to_v2(
            source_metadata, [event("event-1", "pointermove", 1)], {}
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "truncated-x11.jsonl"
            records = [
                {"record_type": "session", "metadata": migrated_metadata},
                {"record_type": "event", **migrated_events[0]},
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            _, _, terminal = load_trace(path)
        self.assertEqual(terminal["status"], "interrupted")
        self.assertIsNone(terminal["event_count"])

    def test_v1_migration_rejects_unknown_or_raw_content_and_normalizes_human(self) -> None:
        unsafe = browser_example(1)
        unsafe["events"][0]["value"] = "secret"
        with self.assertRaisesRegex(ValueError, "unexpected|Privacy-forbidden"):
            migrate_trace_v1_to_v2(unsafe["metadata"], unsafe["events"], {})

        raw_key = browser_example(1)
        raw_key["events"][0].update(
            {
                "type": "keydown",
                "key": "a",
                "key_class": "letter",
                "keyboard": {"repeat": False, "location": 0, "is_composing": False},
            }
        )
        with self.assertRaisesRegex(ValueError, "raw browser key"):
            migrate_trace_v1_to_v2(raw_key["metadata"], raw_key["events"], {})

        human = browser_example(1)
        human["metadata"].update({"actor_type": "human", "source_class": "human_self"})
        normalized, _, _ = migrate_trace_v1_to_v2(human["metadata"], human["events"], {})
        self.assertEqual(normalized["source_class"], "self_human")

        human_v2 = browser_example(2)
        human_v2["metadata"].update({"actor_type": "human", "source_class": "human_self"})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "human-browser.json"
            path.write_text(json.dumps(human_v2), encoding="utf-8")
            loaded_metadata, _, loaded_terminal = load_trace(path)
        self.assertEqual(loaded_metadata["source_class"], "self_human")
        self.assertEqual(loaded_terminal["status"], "complete")
        validate_browser_trace(loaded_metadata, human_v2["summary"], human_v2["events"])
        shared_schema = json.loads(
            (SCHEMA_ROOT / "cui-trace-v2.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(shared_schema).validate(
            {
                "metadata": loaded_metadata,
                "summary": human_v2["summary"],
                "events": human_v2["events"],
            }
        )

    def test_retained_standalone_v1_migrates_to_closed_v2_without_relabeling(self) -> None:
        path = REPO_ROOT / "extension" / "test" / "fixtures" / "agent_demo_trace.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        migrated_metadata, migrated_events, terminal = load_trace(path)
        self.assertEqual(migrated_metadata["schema_version"], "cui-trace-v2")
        self.assertEqual(migrated_metadata["capture_adapter"], "browser_standalone_mirror")
        self.assertIn("not extension storage", migrated_metadata["notes"])
        self.assertNotIn("evidence_scope", migrated_metadata)
        self.assertEqual(len(migrated_events), 50)
        self.assertEqual(terminal["status"], "complete")

        schema = json.loads((SCHEMA_ROOT / "cui-trace-v2.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(
            {
                "metadata": migrated_metadata,
                "summary": original["summary"],
                "events": migrated_events,
            }
        )

    def test_x11_validation_rejection_is_counted_in_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            writer = TraceWriter(path, self.metadata())
            rejected = event("event-1", "keydown", 1)
            rejected["keyboard"]["keysym"] = "a"
            with self.assertRaisesRegex(ValueError, "Privacy-forbidden"):
                writer.append(rejected)
            self.assertEqual(writer.rejected_event_count, 1)
            writer.close(status="interrupted", stopped_at="2026-08-08T00:00:00+00:00")
            _, events, terminal = load_trace(path)
        self.assertEqual(events, [])
        self.assertEqual(terminal["rejected_event_count"], 1)

    def test_quality_covers_duplicate_and_non_monotonic_timestamps(self) -> None:
        events = [
            event("e1", "keydown", 2),
            event("e2", "keyup", 2),
            event("e3", "pointermove", 1),
        ]
        quality = validate_event_stream(events)
        self.assertEqual(quality["duplicate_timestamp_count"], 1)
        self.assertEqual(quality["non_monotonic_transition_count"], 1)

    def test_human_metadata_requires_no_personal_name(self) -> None:
        args = argparse.Namespace(
            actor="human",
            session_id="session-1",
            participant_code="self-human-001",
            pair_id=None,
            task_id="task-1",
            protocol_version="protocol-1",
            executor_version="manual-v1",
            consent_confirmed=True,
            controller_confirmed=False,
            guest_id="scienceboard-vm-sci-bench-v1",
            guest_os_id="ubuntu",
            guest_os_version="22.04",
            screen_width=1920,
            screen_height=1080,
            scienceboard_commit=COMMIT,
        )
        metadata = build_session_metadata(args)
        self.assertEqual(metadata["capture_adapter"], "x11_vm")
        self.assertEqual(metadata["participant_code"], "self-human-001")
        name_like = {"name", "participant_name", "full_name", "email", "user", "username"}
        self.assertFalse(name_like & {key.lower() for key in metadata})

    def test_runtime_rejects_actor_source_mismatch_and_event_modality_mismatch(
        self,
    ) -> None:
        wrong = self.metadata()
        wrong["source_class"] = "reasoning_agent"
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(ValueError, "actor/source"),
        ):
            TraceWriter(Path(temp) / "bad.jsonl", wrong)
        bad_event = event("bad", "keydown", 1)
        bad_event["pointer"] = {"x": 1, "y": 1, "button": 1, "pointer_type": "mouse"}
        with tempfile.TemporaryDirectory() as temp:
            writer = TraceWriter(Path(temp) / "trace.jsonl", self.metadata())
            with self.assertRaisesRegex(ValueError, "cannot carry pointer"):
                writer.append(bad_event)
            writer.close(status="interrupted", stopped_at="2026-08-08T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
