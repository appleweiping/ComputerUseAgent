from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from research.week1_miniwob.adapters import (
    adapter_contract,
    artifact_validation,
    browser_lifecycle,
    common,
    evidence_sanitization,
)


ROOT = Path(__file__).resolve().parents[1]


class AdapterModuleBoundaryTests(unittest.TestCase):
    def test_contract_types_and_public_validators_remain_available_from_common(self) -> None:
        self.assertIs(common.AdapterError, adapter_contract.AdapterError)
        self.assertIs(common.FrameworkRun, adapter_contract.FrameworkRun)
        self.assertIs(common.validate_formal_trace, artifact_validation.validate_formal_trace)
        self.assertIs(common.validate_png_screenshot, artifact_validation.validate_png_screenshot)
        self.assertIs(common.compact_actions, evidence_sanitization.compact_actions)
        self.assertTrue(issubclass(common.BrowserHost, browser_lifecycle.BrowserHost))

    def test_dependency_environment_monkeypatch_seam_is_forwarded(self) -> None:
        dependency_seam = mock.Mock(return_value=[])
        with (
            mock.patch.object(
                common,
                "_dependency_environments",
                dependency_seam,
            ),
            mock.patch.object(
                artifact_validation,
                "verify_dependency_freezes",
                return_value=[],
            ) as implementation,
        ):
            self.assertEqual(
                common.verify_dependency_freezes(
                    "browser-use",
                    compare_live_environment=False,
                ),
                [],
            )
        implementation.assert_called_once_with(
            "browser-use",
            compare_live_environment=False,
            dependency_environments_fn=dependency_seam,
        )

    def test_browser_host_termination_monkeypatch_seam_is_forwarded(self) -> None:
        attempt = {"attempt_id": "module-boundary-attempt"}
        process = object()
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(
                common,
                "_terminate_owned_process_tree",
            ) as terminator,
        ):
            host = common.BrowserHost(attempt, Path(temporary))
            host._terminate_process_tree(process)  # type: ignore[arg-type]
        terminator.assert_called_once_with(process)

    def test_common_split_is_material_and_production_modules_are_bounded(self) -> None:
        paths = {
            "common.py": ROOT / "adapters" / "common.py",
            "artifact_validation.py": ROOT / "adapters" / "artifact_validation.py",
            "browser_lifecycle.py": ROOT / "adapters" / "browser_lifecycle.py",
            "adapter_contract.py": ROOT / "adapters" / "adapter_contract.py",
            "evidence_sanitization.py": ROOT / "adapters" / "evidence_sanitization.py",
        }
        line_counts = {
            name: len(path.read_text(encoding="utf-8").splitlines()) for name, path in paths.items()
        }
        self.assertLessEqual(line_counts["common.py"], 1_200)
        self.assertGreater(line_counts["artifact_validation.py"], 500)
        self.assertGreater(line_counts["browser_lifecycle.py"], 500)
        for name in (
            "artifact_validation.py",
            "browser_lifecycle.py",
            "adapter_contract.py",
            "evidence_sanitization.py",
        ):
            self.assertLessEqual(line_counts[name], 900)


if __name__ == "__main__":
    unittest.main()
