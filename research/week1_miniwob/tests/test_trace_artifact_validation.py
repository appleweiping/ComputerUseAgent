from __future__ import annotations

import copy
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from research.week1_miniwob.adapters.common import (
    AdapterError,
    validate_formal_trace,
    validate_png_screenshot,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


def valid_png_bytes(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanlines = b"".join(b"\x00" + (b"\x00" * width * 4) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class FormalArtifactValidationTests(unittest.TestCase):
    def trace(self) -> dict:
        value = json.loads(
            (REPO_ROOT / "examples" / "traces" / "minimal-session-v2.json").read_text(
                encoding="utf-8"
            )
        )
        value["metadata"]["session_id"] = "example-session-001"
        value["metadata"]["scope_origin"] = "https://example.test"
        value["events"][0]["type"] = "resize"
        value["events"][0]["is_trusted"] = True
        value["summary"]["by_type"] = {"resize": 1}
        return value

    def validate(self, value: dict) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return validate_formal_trace(
                path,
                expected_session_id="example-session-001",
                expected_event_count=len(value["events"]),
                expected_url="https://example.test/task",
                expected_viewport={"width": 1280, "height": 900},
            )

    def test_valid_trace_checks_schema_identity_timestamps_privacy_scope_and_natural_event(self) -> None:
        result = self.validate(self.trace())
        self.assertTrue(result["schema_valid"])
        self.assertTrue(result["event_ids_unique"])
        self.assertTrue(result["timestamps_monotonic"])
        self.assertTrue(result["privacy_safe"])
        self.assertEqual(result["natural_non_recorder_event_count"], 1)

    def test_natural_event_step_alignment_is_counted_without_action_content(self) -> None:
        value = self.trace()
        value["events"][0]["step_id"] = "step-001"
        result = self.validate(value)
        self.assertEqual(result["aligned_natural_event_count"], 1)
        self.assertEqual(result["unaligned_natural_event_count"], 0)
        self.assertEqual(result["step_id_count"], 1)
        self.assertTrue(result["step_id_format_valid"])
        self.assertNotIn("selector", json.dumps(result))

    def test_v2_schema_resolution_is_local_and_never_fetches_the_public_schema_id(self) -> None:
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("formal validation must not use the network"),
        ):
            result = self.validate(self.trace())
        self.assertTrue(result["schema_valid"])

    def test_trace_validation_fails_closed_for_each_integrity_class(self) -> None:
        cases = []

        schema = self.trace()
        schema["metadata"]["tab_id"] = 1
        cases.append((schema, "recorder_schema_invalid"))

        counts = self.trace()
        counts["summary"]["event_count"] = 2
        cases.append((counts, "recorder_event_count_mismatch"))

        key = self.trace()
        key["events"][0]["key_class"] = "letter"
        key["events"][0]["key"] = "private-text"
        cases.append((key, "recorder_raw_key_leak"))

        scope = self.trace()
        scope["events"][0]["url"] = "https://example.test/task?secret=1"
        cases.append((scope, "recorder_page_scope_invalid"))

        for value, code in cases:
            with self.subTest(code=code), self.assertRaises(AdapterError) as caught:
                self.validate(copy.deepcopy(value))
            self.assertEqual(caught.exception.code, code)

    def test_zero_action_failure_is_retained_but_not_analysis_eligible(self) -> None:
        value = self.trace()
        value["events"][0]["type"] = "navigation"
        value["events"][0]["is_trusted"] = None
        value["summary"]["by_type"] = {"navigation": 1}
        formal = self.validate(value)
        self.assertFalse(formal["natural_event_requirement_met"])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(AdapterError) as caught:
                validate_formal_trace(
                    path,
                    expected_session_id="example-session-001",
                    expected_event_count=1,
                    expected_url="https://example.test/task",
                    expected_viewport={"width": 1280, "height": 900},
                    require_natural_event=True,
                )
        self.assertEqual(caught.exception.code, "recorder_no_natural_agent_event")

    def test_duplicate_and_session_timeline_corruption_are_rejected(self) -> None:
        value = self.trace()
        second = copy.deepcopy(value["events"][0])
        second["epoch_ms"] -= 1
        second["monotonic_ms"] -= 1
        value["events"].append(second)
        value["metadata"]["event_count"] = 2
        value["metadata"]["event_seq_last"] = 2
        value["summary"]["event_count"] = 2
        value["summary"]["by_type"] = {"resize": 2}
        with self.assertRaises(AdapterError) as caught:
            self.validate(value)
        self.assertEqual(caught.exception.code, "recorder_duplicate_event_id")
        value["events"][1]["event_id"] = "example-event-002"
        value["events"][1]["event_seq"] = 2
        value["events"][1]["t_session_ms"] = value["events"][0]["t_session_ms"] + 1
        # Wall time and frame-local monotonic time may arrive out of order
        # across iframes.  The background-assigned V2 session timeline remains
        # the cross-frame authority.
        self.assertTrue(self.validate(value)["timestamps_monotonic"])
        value["events"][1]["t_session_ms"] = value["events"][0]["t_session_ms"]
        with self.assertRaises(AdapterError) as caught:
            self.validate(value)
        self.assertEqual(caught.exception.code, "recorder_session_timeline_invalid")

    def test_nonzero_capture_loss_counters_downgrade_the_trace(self) -> None:
        value = self.trace()
        value["metadata"]["dropped_event_count"] = 1
        result = self.validate(value)
        self.assertFalse(result["capture_lossless"])
        self.assertEqual(result["reliability_counters"]["dropped_event_count"], 1)

    def test_nested_content_bearing_payloads_are_rejected_recursively(self) -> None:
        cases = []

        input_content = self.trace()
        input_content["events"][0]["input"] = {
            "input_type": "insertText",
            "data_length": 6,
            "is_composing": False,
            "raw_text": "SECRET",
        }
        cases.append(input_content)

        form_content = self.trace()
        form_content["events"][0]["form"] = {
            "method": "post",
            "action": "https://example.test/submit",
            "control_count": 1,
            "has_submitter": True,
            "submitter_tag": "button",
            "values": ["SECRET"],
        }
        cases.append(form_content)

        clipboard_content = self.trace()
        clipboard_content["events"][0]["clipboard"] = {
            "types": ["SECRET"],
            "item_count": 1,
            "file_count": 0,
        }
        cases.append(clipboard_content)

        clipboard_nested = self.trace()
        clipboard_nested["events"][0]["clipboard"] = {
            "types": [{"value": "SECRET"}],
            "item_count": 1,
            "file_count": 0,
        }
        cases.append(clipboard_nested)

        transfer_nested = self.trace()
        transfer_nested["events"][0]["transfer"] = {
            "types": ["text/plain"],
            "item_count": 1,
            "file_count": 0,
            "effect_allowed": "copy",
            "drop_effect": {"value": "SECRET"},
        }
        cases.append(transfer_nested)

        for value in cases:
            with self.subTest(payload=value["events"][0]), self.assertRaises(AdapterError) as caught:
                self.validate(value)
            self.assertEqual(caught.exception.code, "recorder_nested_content_leak")

    def test_exact_content_free_nested_metadata_shapes_are_accepted(self) -> None:
        value = self.trace()
        event = value["events"][0]
        event["input"] = {"input_type": "insertText", "data_length": 6, "is_composing": False}
        event["clipboard"] = {"types": ["text/plain", "other"], "item_count": 1, "file_count": 0}
        event["transfer"] = {
            "types": ["files"],
            "item_count": 1,
            "file_count": 1,
            "effect_allowed": "copy",
            "drop_effect": "copy",
        }
        event["touch"] = {"active_count": 1, "changed_count": 1}
        event["form"] = {
            "method": "post",
            "action": "https://example.test/submit",
            "control_count": 2,
            "has_submitter": True,
            "submitter_tag": "button",
        }
        event["document_state"] = {"fullscreen": False, "online": True}
        self.assertTrue(self.validate(value)["privacy_safe"])

    def test_complete_decodable_png_must_match_guarded_viewport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "final.png"
            path.write_bytes(valid_png_bytes(332, 214))
            result = validate_png_screenshot(path, {"width": 332, "height": 214})
            self.assertEqual((result["width"], result["height"]), (332, 214))
            self.assertTrue(result["png_complete"])
            self.assertTrue(result["chunk_crc_valid"])
            self.assertTrue(result["image_data_decoded"])
            with self.assertRaises(AdapterError) as caught:
                validate_png_screenshot(path, {"width": 400, "height": 300})
            self.assertEqual(caught.exception.code, "final_screenshot_viewport_mismatch")

    def test_png_header_only_truncation_and_bad_crc_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "final.png"
            path.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                + (13).to_bytes(4, "big")
                + b"IHDR"
                + (332).to_bytes(4, "big")
                + (214).to_bytes(4, "big")
            )
            with self.assertRaises(AdapterError) as caught:
                validate_png_screenshot(path, {"width": 332, "height": 214})
            self.assertEqual(caught.exception.code, "final_screenshot_structure_invalid")

            corrupted = bytearray(valid_png_bytes(332, 214))
            corrupted[29] ^= 0x01  # IHDR CRC byte.
            path.write_bytes(corrupted)
            with self.assertRaises(AdapterError) as caught:
                validate_png_screenshot(path, {"width": 332, "height": 214})
            self.assertEqual(caught.exception.code, "final_screenshot_crc_invalid")


if __name__ == "__main__":
    unittest.main()
