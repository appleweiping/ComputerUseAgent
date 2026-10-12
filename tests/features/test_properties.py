from __future__ import annotations

import math
import unittest

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from computer_use_agent.features import extract_features
from computer_use_agent.trace.privacy import (
    ALLOWED_EVENT_TYPES,
    FORBIDDEN_EVENT_FIELDS,
    assert_privacy_safe_event,
    classify_keysym,
    sanitize_application_id,
)

DETERMINISTIC = settings(max_examples=60, derandomize=True, deadline=None)


class PrivacyPropertyTests(unittest.TestCase):
    def test_every_forbidden_field_is_rejected_at_nested_depth(self) -> None:
        for field in FORBIDDEN_EVENT_FIELDS:
            event = {"type": "pointermove", "nested": [{"deeper": {field.upper(): "x"}}]}
            with self.subTest(field=field), self.assertRaises(ValueError):
                assert_privacy_safe_event(event)

    def test_all_keysym_classes_and_application_canonicalization(self) -> None:
        cases = {
            None: "unknown",
            "F12": "function",
            "Control_L": "modifier",
            "BackSpace": "correction",
            "Return": "editing_command",
            "Page_Down": "navigation",
            "x": "printable",
            "KP_1": "printable",
            "Escape": "system",
            "semicolon": "printable",
            "DefinitelyNotAKey": "unknown",
        }
        self.assertEqual({name: classify_keysym(name) for name in cases}, cases)
        self.assertEqual(sanitize_application_id("org.kde.kalgebra"), "KAlgebra")
        self.assertEqual(sanitize_application_id("celestia"), "Celestia")
        self.assertEqual(sanitize_application_id("wxgui"), "GrassGIS")
        self.assertEqual(sanitize_application_id("gnome-terminal"), "GnomeTerminal")

    @DETERMINISTIC
    @given(st.text(min_size=1, max_size=100))
    def test_unknown_application_ids_are_stable_non_content_digests(self, raw: str) -> None:
        result = sanitize_application_id("unmapped-prefix-" + raw)
        # Arbitrary Unicode may normalize into one of the explicitly reviewed
        # application aliases (for example ``GNOME:TERMINAL``). Those are not
        # members of the unknown-input domain this property is asserting.
        assume(result.startswith("other_"))
        self.assertRegex(result, r"^other_[0-9a-f]{12}$")
        self.assertEqual(result, sanitize_application_id("unmapped-prefix-" + raw))

    def test_invalid_type_and_key_class_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            assert_privacy_safe_event({"type": "clipboard"})
        with self.assertRaises(ValueError):
            assert_privacy_safe_event({"type": "keydown", "key_class": "typed-content"})
        for event_type in ALLOWED_EVENT_TYPES:
            assert_privacy_safe_event({"type": event_type})


class FeaturePropertyTests(unittest.TestCase):
    @DETERMINISTIC
    @given(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=100_000),
                st.integers(min_value=-10_000, max_value=10_000),
                st.integers(min_value=-10_000, max_value=10_000),
            ),
            min_size=1,
            max_size=40,
        )
    )
    def test_pointer_features_are_finite_for_arbitrary_valid_paths(
        self, samples: list[tuple[int, int, int]]
    ) -> None:
        ordered = sorted(samples)
        events = [
            {
                "event_id": f"event-{index}",
                "type": "pointermove",
                "monotonic_ms": float(timestamp),
                "epoch_ms": 1_000_000.0 + timestamp,
                "pointer": {"x": x, "y": y, "button": -1},
                "keyboard": None,
                "key_class": None,
            }
            for index, (timestamp, x, y) in enumerate(ordered)
        ]
        result = extract_features(events, session_duration_ms=float(ordered[-1][0]))
        self.assertTrue(all(math.isfinite(value) for value in result["features"].values()))
        self.assertGreaterEqual(result["features"]["pointer_path_px"], 0)
        self.assertGreaterEqual(result["features"]["pointer_straightness"], 0)
        self.assertLessEqual(result["features"]["pointer_straightness"], 1.00000001)

    def test_explicit_and_inferred_key_hold_paths_match(self) -> None:
        base = {
            "epoch_ms": 1_000,
            "pointer": None,
            "key_class": "printable",
        }
        events = [
            {
                **base,
                "event_id": "d1",
                "type": "keydown",
                "monotonic_ms": 10,
                "keyboard": {"repeat": False},
            },
            {**base, "event_id": "u1", "type": "keyup", "monotonic_ms": 40, "keyboard": {}},
            {
                **base,
                "event_id": "d2",
                "type": "keydown",
                "monotonic_ms": 50,
                "keyboard": {"repeat": False},
            },
            {
                **base,
                "event_id": "u2",
                "type": "keyup",
                "monotonic_ms": 80,
                "keyboard": {"hold_ms": 30},
            },
        ]
        result = extract_features(events)
        self.assertEqual(result["features"]["key_hold_median_ms"], 30)
        self.assertEqual(result["quality"]["unmatched_key_down_count"], 0)


if __name__ == "__main__":
    unittest.main()
