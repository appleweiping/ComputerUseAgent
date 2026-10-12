from __future__ import annotations

import unittest
from typing import Any

from research.scienceboard.analysis import (
    analyze_records,
    classification_metrics,
    dry_run_payload,
    grouped_cross_validation,
)
from computer_use_agent.features import FEATURE_NAMES


TASKS = (
    "scienceboard-vm-kalgebra-a-01-v0.1",
    "scienceboard-vm-celestia-a-01-v0.1",
    "scienceboard-vm-grassgis-a-01-v0.1",
)


def record(actor: str, task: str, repetition: int) -> dict[str, Any]:
    actor_signal = {"human": -3.0, "aio": 0.0, "seeact": 3.0}[actor]
    task_signal = {TASKS[0]: -2.0, TASKS[1]: 0.0, TASKS[2]: 2.0}[task]
    features = {name: 0.0 for name in FEATURE_NAMES}
    features["event_rate_hz"] = actor_signal + repetition * 0.01
    features["pointer_speed_px_s"] = actor_signal * 2 + repetition * 0.01
    features["event_count"] = task_signal + repetition * 0.01
    return {
        "session_id": f"{actor}-{task}-{repetition}",
        "actor": actor,
        "task_id": task,
        "repetition": repetition,
        "features": features,
        "quality": {},
        "evidence_valid": True,
        "protocol_version": "scienceboard-matched-traces-v2",
        "provenance_fingerprint": "fixture-shared-provenance",
    }


class AnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            record(actor, task, repetition)
            for repetition in (1, 2, 3)
            for task in TASKS
            for actor in ("human", "aio", "seeact")
        ]

    def test_leave_one_task_out_has_three_disjoint_folds(self) -> None:
        result = grouped_cross_validation(
            self.records,
            group_field="task_id",
            label=lambda item: item["actor"],
        )
        self.assertEqual(result["fold_count"], 3)
        self.assertEqual(sum(fold["test_count"] for fold in result["folds"]), 27)
        self.assertGreaterEqual(result["aggregate"]["accuracy"], 0.9)

    def test_full_analysis_includes_binary_three_class_and_task_control(self) -> None:
        report = analyze_records(self.records)
        self.assertEqual(report["sample_count"], 27)
        self.assertIn("human_vs_agent_leave_one_task_out", report)
        self.assertIn("human_vs_aio_vs_seeact_leave_one_task_out", report)
        self.assertIn("task_identification_leave_one_repetition_out_negative_control", report)
        self.assertIn("Single-participant", report["claim_boundary"])
        self.assertTrue(report["formal_matrix_valid"])
        self.assertEqual(report["analysis_status"], "formal_single_participant_pipeline_probe")
        task = report["descriptive"]["by_task"][TASKS[0]]
        human_rate = task["actors"]["human"]["event_rate_hz"]
        self.assertEqual(human_rate, {"mean": -2.98, "median": -2.98, "n": 3})
        self.assertIsInstance(
            task["human_minus_agent_hedges_g"]["event_rate_hz"], float
        )
        self.assertFalse(dry_run_payload()["writes_performed"])

    def test_default_rejects_26_cells_and_override_is_explicitly_nonformal(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly 27"):
            analyze_records(self.records[:-1])
        report = analyze_records(self.records[:-1], allow_incomplete_diagnostic=True)
        self.assertFalse(report["formal_matrix_valid"])
        self.assertEqual(report["analysis_status"], "non_formal_diagnostic_allow_incomplete")
        self.assertIn("INCOMPLETE MATRIX", report["diagnostic_warning"])

    def test_duplicate_cell_never_passes_formal_gate(self) -> None:
        duplicate = dict(self.records[-1])
        duplicate["session_id"] = "another-session-same-cell"
        with self.assertRaisesRegex(ValueError, "unique cells"):
            analyze_records([*self.records, duplicate])

    def test_metrics_are_balanced_and_explicit(self) -> None:
        metrics = classification_metrics(["a", "a", "b", "b"], ["a", "b", "b", "b"])
        self.assertEqual(metrics["accuracy"], 0.75)
        self.assertEqual(metrics["balanced_accuracy"], 0.75)
        self.assertEqual(metrics["confusion_matrix"]["labels"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
