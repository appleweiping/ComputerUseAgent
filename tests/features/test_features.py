from __future__ import annotations

import math
import unittest

from computer_use_agent.features import (
    FEATURE_NAMES,
    LEAKAGE_FIELDS,
    extract_features,
    validate_feature_vector,
)


def make_event(event_id: str, event_type: str, timestamp: float, **extra: object) -> dict:
    event = {
        "event_id": event_id,
        "type": event_type,
        "monotonic_ms": timestamp,
        "epoch_ms": 10_000 + timestamp,
        "pointer": None,
        "keyboard": None,
        "key_class": None,
    }
    event.update(extra)
    return event


class FeatureExtractionTests(unittest.TestCase):
    def test_zero_duration_and_missing_modalities_are_finite(self) -> None:
        result = extract_features([make_event("e1", "focusin", 0)])
        features = result["features"]
        self.assertEqual(features["event_rate_hz"], 0)
        self.assertEqual(features["missing_pointer"], 1)
        self.assertEqual(features["missing_keyboard"], 1)
        self.assertTrue(all(math.isfinite(value) for value in features.values()))

    def test_pointer_keyboard_click_and_correction_features(self) -> None:
        events = [
            make_event("e1", "pointermove", 0, pointer={"x": 0, "y": 0, "button": -1}),
            make_event("e2", "pointermove", 100, pointer={"x": 3, "y": 4, "button": -1}),
            make_event("e3", "pointerdown", 120, pointer={"x": 3, "y": 4, "button": 1}),
            make_event("e4", "pointerup", 180, pointer={"x": 3, "y": 4, "button": 1}),
            make_event("e5", "keydown", 200, key_class="printable", keyboard={"repeat": False}),
            make_event("e6", "keyup", 240, key_class="printable", keyboard={"hold_ms": 40}),
            make_event("e7", "keydown", 300, key_class="correction", keyboard={"repeat": False}),
            make_event("e8", "keyup", 330, key_class="correction", keyboard={"hold_ms": 30}),
            make_event("e9", "focusout", 400),
        ]
        result = extract_features(events)
        features = result["features"]
        self.assertGreaterEqual(features["pointer_path_px"], 5)
        self.assertEqual(features["click_dwell_median_ms"], 60)
        self.assertEqual(features["key_hold_median_ms"], 35)
        self.assertEqual(features["correction_ratio"], 0.5)
        self.assertEqual(result["quality"]["unmatched_key_down_count"], 0)

    def test_full_duration_and_time_to_first_input_use_session_origin(self) -> None:
        events = [
            make_event("e1", "focusin", 100),
            make_event(
                "e2",
                "keydown",
                500,
                key_class="editing_command",
                keyboard={"repeat": False},
            ),
            make_event(
                "e3",
                "keyup",
                540,
                key_class="editing_command",
                keyboard={"repeat": False, "hold_ms": 40},
            ),
        ]
        result = extract_features(events, trace_origin_ms=0, session_duration_ms=1000)
        self.assertEqual(result["features"]["time_to_first_input_ms"], 500)
        self.assertEqual(result["features"]["duration_ms"], 1000)
        self.assertEqual(result["features"]["event_rate_hz"], 3)
        self.assertEqual(result["features"]["correction_ratio"], 0)

    def test_correction_ratio_counts_only_destructive_edit_keys(self) -> None:
        events = [
            make_event(
                "e1",
                "keydown",
                10,
                key_class="editing_command",
                keyboard={"repeat": False},
            ),
            make_event("e2", "keydown", 20, key_class="correction", keyboard={"repeat": False}),
        ]
        self.assertEqual(extract_features(events)["features"]["correction_ratio"], 0.5)

    def test_browser_backspace_and_x11_correction_have_equal_semantics(self) -> None:
        browser = [
            make_event(
                "browser-down",
                "keydown",
                10,
                key="Backspace",
                key_class="control",
                keyboard={"repeat": False, "location": 0, "is_composing": False},
            )
        ]
        x11 = [
            make_event(
                "x11-down",
                "keydown",
                10,
                key_class="correction",
                keyboard={"repeat": False},
            )
        ]
        self.assertEqual(
            extract_features(browser)["features"]["correction_ratio"],
            extract_features(x11)["features"]["correction_ratio"],
        )
        self.assertEqual(extract_features(browser)["features"]["correction_ratio"], 1.0)

    def test_non_monotonic_and_unmatched_pairs_are_retained_as_quality_flags(
        self,
    ) -> None:
        events = [
            make_event("e1", "pointerup", 20, pointer={"x": 0, "y": 0, "button": 1}),
            make_event("e2", "keyup", 10, key_class="unknown", keyboard={}),
        ]
        result = extract_features(events)
        self.assertEqual(result["quality"]["non_monotonic_transition_count"], 1)
        self.assertEqual(result["quality"]["unmatched_pointer_up_count"], 1)
        self.assertEqual(result["quality"]["unmatched_key_up_count"], 1)

    def test_feature_schema_has_no_known_leakage_fields(self) -> None:
        self.assertFalse(set(FEATURE_NAMES) & LEAKAGE_FIELDS)
        vector = dict.fromkeys(FEATURE_NAMES, 0.0)
        validate_feature_vector(vector)
        vector["task_id"] = 1.0
        with self.assertRaises(ValueError):
            validate_feature_vector(vector)

    def test_v2_session_clock_survives_document_clock_reset(self) -> None:
        first = make_event("e1", "pointermove", 900, pointer={"x": 0, "y": 0, "button": -1})
        second = make_event("e2", "pointermove", 2, pointer={"x": 3, "y": 4, "button": -1})
        first.update(
            {
                "schema_version": "cui-trace-v2",
                "event_seq": 1,
                "document_epoch": 0,
                "t_session_ms": 100,
                "t_document_ms": 900,
                "step_id": None,
            }
        )
        second.update(
            {
                "schema_version": "cui-trace-v2",
                "event_seq": 2,
                "document_epoch": 1,
                "t_session_ms": 200,
                "t_document_ms": 2,
                "step_id": None,
            }
        )
        result = extract_features([first, second])
        self.assertEqual(result["features"]["duration_ms"], 200)
        self.assertEqual(result["features"]["pointer_path_px"], 5)
        self.assertEqual(result["quality"]["non_monotonic_transition_count"], 0)

    def test_raw_v2_envelope_fields_are_explicitly_leakage_guarded(self) -> None:
        for field in (
            "event_seq",
            "document_epoch",
            "t_session_ms",
            "t_document_ms",
            "step_id",
            "capture_adapter",
            "application_id",
        ):
            self.assertIn(field, LEAKAGE_FIELDS)


if __name__ == "__main__":
    unittest.main()
