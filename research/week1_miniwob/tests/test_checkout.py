"""The frozen harness maps legacy paths, pins recorder 0.4.0 and never writes evidence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from research.week1_miniwob import checkout, runner
from research.week1_miniwob.adapters import common
from research.week1_miniwob.protocol import ProtocolError, build_matrix, load_manifest

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]


def _args(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "manifest": ROOT / "benchmark_manifest.json",
        "output_dir": None,
        "framework": "browser-use",
        "task": "click-checkboxes",
        "repetition": 1,
        "run_label": "unit",
        "dry_run": False,
        "preflight": False,
        "allow_unready": False,
        "require_clean": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class LegacyPathTests(unittest.TestCase):
    def test_recorded_paths_map_onto_this_checkout(self) -> None:
        self.assertEqual(checkout.translate_legacy_path("recorder"), "extension/app")
        self.assertEqual(
            checkout.translate_legacy_path("experiments/week1/adapters/dispatch.py"),
            "research/week1_miniwob/adapters/dispatch.py",
        )
        self.assertEqual(
            checkout.translate_legacy_path("experiments\\week1\\benchmark_manifest.json"),
            "research/week1_miniwob/benchmark_manifest.json",
        )
        self.assertEqual(checkout.translate_legacy_path("extension/app"), "extension/app")

    def test_absolute_and_parent_paths_are_rejected(self) -> None:
        for value in ("", "/etc/passwd", "C:/private/x", "c:\\x", "experiments/week1/../../x"):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                checkout.translate_legacy_path(value)

    def test_frozen_manifest_adapter_commands_resolve_to_files(self) -> None:
        manifest = load_manifest(ROOT / "benchmark_manifest.json")
        for framework in manifest["frameworks"]:
            recorded = framework["default_adapter_command"]
            self.assertEqual(recorded[1], "experiments/week1/adapters/dispatch.py")
            command = runner.expand_default_command(recorded)
            self.assertEqual(command[1], "research/week1_miniwob/adapters/dispatch.py")
            self.assertTrue((REPO_ROOT / command[1]).is_file())

    def test_manifest_bytes_and_matrix_hash_are_unchanged(self) -> None:
        # The manifest is frozen protocol data; translation never edits it.
        rows = build_matrix(load_manifest(ROOT / "benchmark_manifest.json"))
        self.assertEqual(
            rows[0]["manifest_sha256"],
            "51e921767860934ab44a831c42a9c3f05f725cb9791582abf0bf5074bd292000",
        )


class FrozenRecorderPinTests(unittest.TestCase):
    def check_with_extension(self, version: str | None) -> dict[str, str]:
        manifest = load_manifest(ROOT / "benchmark_manifest.json")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            if version is not None:
                app = root / "extension" / "app"
                app.mkdir(parents=True)
                (app / "manifest.json").write_text(
                    json.dumps({"manifest_version": 3, "version": version}), encoding="utf-8"
                )
            with mock.patch.object(checkout, "REPO_ROOT", root):
                return checkout.recorder_extension_check(manifest)

    def test_only_recorder_0_4_0_passes(self) -> None:
        self.assertEqual(self.check_with_extension("0.4.0")["status"], "pass")
        newer = self.check_with_extension("0.5.0")
        self.assertEqual(newer["status"], "fail")
        self.assertIn("requires recorder 0.4.0", newer["detail"])
        self.assertEqual(self.check_with_extension(None)["status"], "fail")

    def test_adapter_contract_pins_recorder_0_4_0(self) -> None:
        self.assertEqual(common.EXPECTED_RECORDER_VERSION, "0.4.0")
        self.assertEqual(common.EXTENSION_ROOT, REPO_ROOT / "extension" / "app")


class OutputBoundaryTests(unittest.TestCase):
    def test_evidence_tree_is_never_an_output_target(self) -> None:
        for target in (
            checkout.EVIDENCE_ROOT,
            checkout.EVIDENCE_ROOT / "week1_browsergym_formal",
            checkout.EVIDENCE_ROOT / "week1_miniwob_prefix_failures" / "x",
        ):
            with self.subTest(target=target), self.assertRaises(ProtocolError):
                checkout.require_output_outside_evidence(target)
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                checkout.require_output_outside_evidence(Path(temporary) / "out"),
                (Path(temporary) / "out").resolve(),
            )

    def test_live_runs_require_an_explicit_output_dir_and_write_nothing(self) -> None:
        for output_dir, message in (
            (None, "--output-dir is required"),
            (checkout.EVIDENCE_ROOT / "week1_browsergym_partial", "research/evidence"),
        ):
            with self.subTest(output_dir=output_dir):
                with mock.patch.object(runner, "parse_args", return_value=_args(output_dir=output_dir)), mock.patch.object(
                    runner, "run_preflight", return_value={"ready": True, "checks": []}
                ), mock.patch.object(runner, "execute_attempt") as execute, mock.patch(
                    "builtins.print"
                ) as printed:
                    self.assertEqual(runner.main(), 2)
                execute.assert_not_called()
                self.assertIn(message, printed.call_args.args[0])
        self.assertFalse((checkout.EVIDENCE_ROOT / "week1_browsergym_partial").exists())

    def test_formal_dry_run_reports_an_unverifiable_binding_instead_of_failing(self) -> None:
        args = _args(framework=None, task=None, repetition=None, run_label=None, dry_run=True)
        with mock.patch.object(runner, "parse_args", return_value=args), mock.patch.object(
            runner, "is_canonical_tracked_manifest", return_value=True
        ), mock.patch.object(
            runner, "run_preflight", return_value={"ready": False, "checks": []}
        ), mock.patch.object(
            runner, "load_binding", side_effect=runner.FormalStudyError("capsule input unavailable")
        ), mock.patch("builtins.print") as printed:
            self.assertEqual(runner.main(), 0)
        payload = json.loads(printed.call_args.args[0])
        self.assertIsNone(payload["formal_study_binding"])
        self.assertIn("capsule input unavailable", payload["formal_study_binding_error"])
        self.assertEqual(payload["planned_attempt_count"], 27)
        self.assertFalse(payload["writes_performed"])

    def test_formal_preflight_still_fails_closed_on_an_unverifiable_binding(self) -> None:
        args = _args(framework=None, task=None, repetition=None, run_label=None, preflight=True)
        with mock.patch.object(runner, "parse_args", return_value=args), mock.patch.object(
            runner, "is_canonical_tracked_manifest", return_value=True
        ), mock.patch.object(
            runner, "run_preflight", return_value={"ready": True, "checks": []}
        ), mock.patch.object(
            runner, "load_binding", side_effect=runner.FormalStudyError("capsule input unavailable")
        ), mock.patch("builtins.print"):
            self.assertEqual(runner.main(), 2)


class RetiredFrameworkTests(unittest.TestCase):
    def test_diagnostics_reject_a_framework_without_an_active_adapter(self) -> None:
        payload = common.common_diagnostics("stagehand")
        self.assertFalse(payload["ready"])
        first = payload["checks"][0]
        self.assertEqual((first["id"], first["status"]), ("framework-active", "fail"))
        self.assertIn("no active adapter", first["detail"])

    def test_active_frameworks_match_the_dispatcher(self) -> None:
        from research.week1_miniwob.adapters import dispatch

        self.assertEqual(set(common.ACTIVE_FRAMEWORKS), set(dispatch.ADAPTERS))


class DispatchTests(unittest.TestCase):
    def test_adapters_run_as_modules_from_the_repository_root(self) -> None:
        import importlib.util
        import subprocess
        import sys

        from research.week1_miniwob.adapters import dispatch

        for module in dispatch.ADAPTERS.values():
            self.assertIsNotNone(importlib.util.find_spec(module), module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = dispatch.environment_python(root, "browser-use")
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            completed = subprocess.CompletedProcess([], 0, '{"ready": false}', "")
            with mock.patch.dict(
                "os.environ", {"CUI_WEEK1_ADAPTER_ENV_ROOT": str(root)}, clear=False
            ), mock.patch.object(sys, "argv", ["dispatch.py", "browser-use", "--self-test"]), mock.patch.object(
                dispatch.subprocess, "run", return_value=completed
            ) as run, mock.patch("sys.stdout"):
                self.assertEqual(dispatch.main(), 0)
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [str(python), "-m", "research.week1_miniwob.adapters.browser_use_adapter", "--self-test"],
        )
        self.assertEqual(run.call_args.kwargs["cwd"], dispatch.REPO_ROOT)
        self.assertFalse(run.call_args.kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
