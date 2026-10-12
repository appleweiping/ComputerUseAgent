"""Pure-function tests for the leave-one-agent-family-out probe.

Importing the module needs neither numpy, scikit-learn nor xgboost, so the
four data-handling tests run without the ``research`` extra. The metric test
needs numpy and scikit-learn and skips cleanly without them; nothing here needs
xgboost (see test_optional_dependencies.py).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pytest

from research.fp_agent import leave_one_family_out as loafo


def record(*, family: str, task: str, group: str | None, value: float) -> loafo.Record:
    browser = (value,) * 418
    behavioral = (value + 0.5,) * 50
    return loafo.Record(
        family=family,
        task=task,
        group=group,
        browser=browser,
        behavioral=behavioral,
        combined_digest=loafo._vector_digest(browser, behavioral),
    )


class LeaveOneFamilyOutTests(unittest.TestCase):
    def test_write_json_uses_deterministic_lf_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            loafo.write_json(path, {"z": 1, "label": "测试", "a": [2, 3]})
            content = path.read_bytes()

        self.assertNotIn(b"\r\n", content)
        self.assertTrue(content.endswith(b"\n"))
        self.assertEqual(
            json.loads(content.decode("utf-8")),
            {"a": [2, 3], "label": "测试", "z": 1},
        )

    def test_human_split_is_group_disjoint_and_deterministic(self):
        rows = []
        tasks = ("Flight-booking", "Forums", "Shopping")
        for index in range(10):
            for task_index, task in enumerate(tasks):
                rows.append(
                    record(
                        family="Human",
                        task=task,
                        group=f"participant-{index}",
                        value=float(index * 10 + task_index),
                    )
                )

        train_a, test_a, summary_a = loafo.split_humans(
            rows, seed=20260808, test_group_fraction=0.2
        )
        train_b, test_b, summary_b = loafo.split_humans(
            list(reversed(rows)), seed=20260808, test_group_fraction=0.2
        )

        self.assertFalse({x.group for x in train_a} & {x.group for x in test_a})
        self.assertEqual({x.group for x in train_a}, {x.group for x in train_b})
        self.assertEqual({x.group for x in test_a}, {x.group for x in test_b})
        self.assertEqual(summary_a, summary_b)
        self.assertEqual(summary_a["group_overlap"], 0)
        self.assertEqual(set(summary_a["task_counts_test"]), set(tasks))

    def test_load_records_collapses_exact_within_family_duplicates(self):
        def source(family: str, task_name: str, trial: int) -> str:
            return json.dumps(
                {
                    "website_version": "fixture",
                    "class_label": family,
                    "task_name": task_name,
                    "trial_index": trial,
                    "start_time": "",
                    "end_time": "",
                    "source_file": "fixture",
                },
                separators=(",", ":"),
            )

        def payload(value: float) -> dict[str, list[float]]:
            return {"fpjs": [value] * 418, "behavioral": [value + 0.5] * 50}

        fixture = {
            "Agent A": {
                source("Agent A", "Shopping task - fixture", 1): payload(1.0),
                source("Agent A", "Forums task - fixture", 2): payload(1.0),
            },
            "Agent B": {source("Agent B", "Flight-booking task - fixture", 1): payload(2.0)},
            "Human": {
                source("Human", "Shopping p1 entry 1", 1): payload(3.0),
                source("Human", "Forums p2 entry 1", 2): payload(4.0),
                source("Human", "Flight-booking p3 entry 1", 3): payload(5.0),
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "features.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            records, quality = loafo.load_records(path)

        self.assertEqual(len(records["Agent A"]), 1)
        self.assertEqual(quality["exact_combined_duplicates_removed_by_family"]["Agent A"], 1)
        self.assertEqual(quality["cross_family_exact_combined_duplicate_vectors"], 0)
        self.assertEqual(quality["nonfinite_feature_values"], 0)

    def test_fold_metrics_uses_human_zero_agent_one_orientation(self):
        np = pytest.importorskip("numpy", reason="needs numpy from the research extra")
        pytest.importorskip("sklearn", reason="needs scikit-learn from the research extra")
        truth = np.asarray([0, 0, 1, 1])
        prediction = np.asarray([0, 1, 0, 1])
        probability = np.asarray([0.1, 0.8, 0.2, 0.9])
        metrics = loafo._fold_metrics(truth, prediction, probability)

        self.assertEqual(metrics["confusion_matrix_human_agent"], [[1, 1], [1, 1]])
        self.assertEqual(metrics["human_specificity"], 0.5)
        self.assertEqual(metrics["heldout_agent_recall"], 0.5)
        self.assertEqual(metrics["balanced_accuracy"], 0.5)

    def test_balanced_weights_assign_equal_total_weight_to_each_class(self):
        labels = [0, 0, 1, 1, 1, 1]
        weights = loafo._balanced_sample_weights(labels)
        self.assertAlmostEqual(sum(w for w, y in zip(weights, labels, strict=True) if y == 0), 3.0)
        self.assertAlmostEqual(sum(w for w, y in zip(weights, labels, strict=True) if y == 1), 3.0)
        self.assertAlmostEqual(sum(weights) / len(weights), 1.0)
        with self.assertRaises(ValueError):
            loafo._balanced_sample_weights([1, 1, 1])
        with self.assertRaises(ValueError):
            loafo._balanced_sample_weights([0, 2])


if __name__ == "__main__":
    unittest.main()
