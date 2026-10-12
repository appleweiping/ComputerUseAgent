from __future__ import annotations

import json
import unittest
from pathlib import Path

from research.week1_miniwob.adapters.common import EXPECTED_DISTRIBUTIONS
from research.week1_miniwob.adapters import dispatch, setup_environments


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "research" / "week1_miniwob" / "benchmark_manifest.json"
BROWSER_USE_REQUIREMENTS = (
    ROOT / "research" / "week1_miniwob" / "adapters" / "requirements-browser-use.txt"
)
ACTIVE_NODE_PACKAGE = ROOT / "research" / "week1_miniwob" / "adapters" / "node" / "package.json"
ACTIVE_NODE_LOCK = ROOT / "research" / "week1_miniwob" / "adapters" / "node" / "package-lock.json"
ARCHIVED_WEEK1_ROOT = (
    ROOT / "research" / "week1_miniwob" / "adapters" / "archive" / "stagehand-3.7.1"
)


class DependencySecurityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.frameworks = {row["id"]: row for row in manifest["frameworks"]}

    def test_browser_use_keeps_framework_pin_and_uses_fixed_settings_release(self) -> None:
        requirements = {
            row.strip()
            for row in BROWSER_USE_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if row.strip() and not row.lstrip().startswith(("#", "-r "))
        }
        self.assertIn("browser-use==0.11.5", requirements)
        self.assertIn("pydantic-settings==2.14.2", requirements)
        self.assertEqual(EXPECTED_DISTRIBUTIONS["browser-use"], "0.11.5")
        self.assertEqual(EXPECTED_DISTRIBUTIONS["pydantic-settings"], "2.14.2")

        security = self.frameworks["browser-use"]["formal_security"]
        self.assertTrue(security["allowed"])
        self.assertEqual(security["blocking_advisories"], [])
        self.assertIn("GHSA-4xgf-cpjx-pc3j", security["resolved_advisories"])

    def test_stagehand_is_retired_from_every_active_path(self) -> None:
        active = json.loads(ACTIVE_NODE_PACKAGE.read_text(encoding="utf-8"))
        self.assertNotIn("@browserbasehq/stagehand", active["dependencies"])
        active_lock = json.loads(ACTIVE_NODE_LOCK.read_text(encoding="utf-8"))
        self.assertNotIn(
            "@browserbasehq/stagehand",
            active_lock["packages"][""]["dependencies"],
        )
        self.assertFalse(
            any("stagehand" in path.lower() for path in active_lock["packages"])
        )
        self.assertNotIn("stagehand", dispatch.ADAPTERS)
        self.assertNotIn("stagehand", dispatch.RUNTIME_ENVIRONMENTS)
        self.assertNotIn("stagehand", setup_environments.FRAMEWORK_ENVIRONMENTS)
        # Only the archive README was ever published (the lock and bridge were
        # absent from the source snapshot); no runnable Stagehand code ships.
        archived = sorted(path.name for path in ARCHIVED_WEEK1_ROOT.iterdir())
        self.assertEqual(archived, ["README.md"])
        readme = (ARCHIVED_WEEK1_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("not an active install target", readme)
        self.assertIn("28d70a21401de20411e8a4da0fdf1fce3fbe5dc3a1c1e4deddc2d796697fac00", readme)

    def test_frozen_manifest_keeps_the_historical_stagehand_security_gate(self) -> None:
        security = self.frameworks["stagehand"]["formal_security"]
        self.assertFalse(security["allowed"])
        self.assertIn("GHSA-866g-f22w-33x8", security["blocking_advisories"])
        self.assertIn("GHSA-vxpw-j846-p89q", security["blocking_advisories"])
        evidence = security["resolution_evidence"]
        self.assertEqual(evidence["npm_audit_production_counts"]["total"], 18)
        self.assertEqual(
            evidence["github_actions_node20_console_counts"],
            {"low": 18, "moderate": 1, "high": 1, "critical": 0, "total": 20},
        )
        self.assertEqual(
            evidence["github_actions_audit_evidence"]["production_lock_sha256"][
                "experiments/week1/adapters/node/package-lock.json"
            ],
            "28d70a21401de20411e8a4da0fdf1fce3fbe5dc3a1c1e4deddc2d796697fac00",
        )
        self.assertEqual(evidence["npm_audit_fix_compatible_changes"], 0)
        self.assertEqual(evidence["provider_utils_advisory_patched_versions"], [])
        self.assertEqual(evidence["pinned_runtime_no_model_cdp_probe"]["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
