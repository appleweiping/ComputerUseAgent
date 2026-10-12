from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from research.week1_miniwob import evidence_projection, runner


ROOT = Path(__file__).resolve().parents[1]


class RunnerModuleBoundaryTests(unittest.TestCase):
    def test_public_projection_remains_available_from_runner(self) -> None:
        self.assertIs(runner.public_adapter_result, evidence_projection.public_adapter_result)
        self.assertIs(
            runner._public_framework_evidence,
            evidence_projection._public_framework_evidence,
        )

    def test_process_wrapper_forwards_runner_owned_context_and_patch_point(self) -> None:
        completed = subprocess.CompletedProcess(["adapter"], 0, "{}", "")
        terminator = mock.Mock(return_value="terminated")
        with mock.patch.object(runner, "_terminate_process_tree", terminator), mock.patch.object(
            runner,
            "_run_adapter_process_impl",
            return_value=completed,
        ) as implementation:
            observed = runner._run_adapter_process(
                ["adapter"],
                input_text="{}",
                timeout_seconds=7,
            )

        self.assertIs(observed, completed)
        implementation.assert_called_once_with(
            ["adapter"],
            input_text="{}",
            timeout_seconds=7,
            terminate_process_tree_fn=terminator,
            shutdown_seconds=runner.PROCESS_TREE_SHUTDOWN_SECONDS,
            cwd=runner.REPO_ROOT,
        )

    def test_runner_split_is_material(self) -> None:
        runner_lines = (ROOT / "runner.py").read_text(encoding="utf-8").splitlines()
        evidence_lines = (ROOT / "evidence_projection.py").read_text(
            encoding="utf-8"
        ).splitlines()
        process_lines = (ROOT / "runner_process.py").read_text(encoding="utf-8").splitlines()
        self.assertLess(len(runner_lines), 1_200)
        self.assertGreater(len(evidence_lines), 300)
        self.assertGreater(len(process_lines), 120)


if __name__ == "__main__":
    unittest.main()
