"""Two-phase X11 calibration certificate tests (XTest plus direct self-human)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from computer_use_agent.capture.x11.calibration import (
    REQUIRED_INPUT_TYPES,
    dry_run_payload,
    evaluate_calibration,
    main,
)
from computer_use_agent.trace.io import TraceWriter
from tests.capture.x11_fixtures import event, metadata


class CalibrationTests(unittest.TestCase):
    def _write_phase(self, directory: Path, name: str) -> tuple[str, int]:
        path = directory / f"{name}.jsonl"
        actor = "xtest" if name == "xtest" else "human"
        writer = TraceWriter(path, metadata(actor, f"calibration-{name}", calibration=True))
        for index, event_type in enumerate(
            ["pointermove", "pointerdown", "pointerup", "keydown", "keyup"], 1
        ):
            writer.append(event(f"{name}-{index}", event_type, float(index)))
        writer.close(status="complete", stopped_at="2026-08-08T00:00:00+00:00")
        return path.name, writer.event_count

    def test_two_phase_calibration_and_post_stop_control(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            xtest_name, xtest_count = self._write_phase(directory, "xtest")
            human_name, human_count = self._write_phase(directory, "human")
            evidence = {
                "schema_version": "scienceboard-x11-calibration-evidence-v1",
                "xtest": {
                    "trace": xtest_name,
                    "event_count_at_stop": xtest_count,
                    "event_count_after_post_stop_probe": xtest_count,
                },
                "self_human": {
                    "trace": human_name,
                    "event_count_at_stop": human_count,
                    "event_count_after_post_stop_probe": human_count,
                },
            }
            evidence_path = directory / "evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            certificate = evaluate_calibration(evidence_path)
            self.assertTrue(certificate["passed"])
            self.assertEqual(
                [phase["name"] for phase in certificate["phases"]],
                ["xtest", "self_human"],
            )
            self.assertTrue(all(certificate["cross_phase_checks"].values()))

    def test_calibration_rejects_reusing_one_trace_for_both_phases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            trace_name, count = self._write_phase(directory, "xtest")
            evidence = {
                "schema_version": "scienceboard-x11-calibration-evidence-v1",
                "xtest": {
                    "trace": trace_name,
                    "event_count_at_stop": count,
                    "event_count_after_post_stop_probe": count,
                },
                "self_human": {
                    "trace": trace_name,
                    "event_count_at_stop": count,
                    "event_count_after_post_stop_probe": count,
                },
            }
            path = directory / "evidence.json"
            path.write_text(json.dumps(evidence), encoding="utf-8")
            certificate = evaluate_calibration(path)
            self.assertFalse(certificate["passed"])
            self.assertFalse(certificate["cross_phase_checks"]["distinct_trace_paths"])

    def _evidence(self, directory: Path, xtest: tuple[str, int], human: tuple[str, int]) -> Path:
        evidence = {
            "schema_version": "scienceboard-x11-calibration-evidence-v1",
            "xtest": {
                "trace": xtest[0],
                "event_count_at_stop": xtest[1],
                "event_count_after_post_stop_probe": xtest[1],
            },
            "self_human": {
                "trace": human[0],
                "event_count_at_stop": human[1],
                "event_count_after_post_stop_probe": human[1],
            },
        }
        path = directory / "evidence.json"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        return path

    def test_post_stop_event_growth_fails_the_negative_control(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            xtest = self._write_phase(directory, "xtest")
            human = self._write_phase(directory, "human")
            path = self._evidence(directory, xtest, human)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["xtest"]["event_count_after_post_stop_probe"] = xtest[1] + 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            certificate = evaluate_calibration(path)
        self.assertFalse(certificate["passed"])
        xtest_phase = certificate["phases"][0]
        self.assertFalse(xtest_phase["checks"]["post_stop_negative_control"])

    def test_certificate_does_not_publish_private_trace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = self._evidence(
                directory,
                self._write_phase(directory, "xtest"),
                self._write_phase(directory, "human"),
            )
            certificate = evaluate_calibration(path)
        self.assertTrue(certificate["passed"])
        self.assertNotIn(str(directory), json.dumps(certificate))
        self.assertTrue(all("trace_path" not in phase for phase in certificate["phases"]))

    def test_dry_run_plan_lists_required_types_and_no_focus_claim(self) -> None:
        plan = dry_run_payload()
        self.assertEqual(plan["required_event_types_per_phase"], sorted(REQUIRED_INPUT_TYPES))
        self.assertNotIn("focus", json.dumps(plan).lower())
        self.assertEqual(main(["--dry-run"]), 0)
        self.assertEqual(main([]), 2)


if __name__ == "__main__":
    unittest.main()
