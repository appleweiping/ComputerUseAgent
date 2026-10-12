from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from research.week1_miniwob import runner
from research.week1_miniwob.protocol import (
    ADAPTER_RESULT_SCHEMA,
    ProtocolError,
    build_matrix,
    load_manifest,
    validate_adapter_result,
)
from research.week1_miniwob.tests import provider_entitlement_fixture


ROOT = Path(__file__).resolve().parents[1]


class Week1RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.attempt = build_matrix(load_manifest(ROOT / "benchmark_manifest.json"))[0]

    def complete_result(self) -> dict:
        attempt = self.attempt
        descriptor = {
            "artifact_id": "private-test-0000000000000000",
            "retained_private": True,
            "bytes": 24,
            "sha256": "a" * 64,
        }
        dependency_descriptor = {
            "artifact_id": "private-dependency-freeze-bbbbbbbbbbbbbbbb",
            "retained_private": True,
            "bytes": 48,
            "sha256": "b" * 64,
        }
        viewport = dict(attempt["browser"]["viewport"])
        return {
            "schema_version": ADAPTER_RESULT_SCHEMA,
            "attempt_id": attempt["attempt_id"],
            "status": "success",
            "agent": {
                "framework": attempt["framework"]["id"],
                "framework_version": attempt["framework"]["version"],
                "model": attempt["model"],
                "action_count": 2,
                "action_count_observed": True,
                "action_count_censored": False,
                "action_history": [{"type": "click", "selector": "PRIVATE-SELECTOR"}],
                "framework_completed": True,
            },
            "validator": {
                "source": "browsergym_official_reward",
                "reward": 1,
                "terminated": True,
                "truncated": False,
                "task_info": {"instruction": "PRIVATE-TASK-TEXT"},
                "task_setup_artifact": descriptor,
                "task_info_artifact": descriptor,
            },
            "recorder": {
                "capture_adapter": "browser_mv3",
                "source_class": "cui_agent",
                "status": "complete",
                "session_id": "private-session-id",
                "event_count": 5,
                "trace_artifact": descriptor,
                "artifact_validation": {
                    "schema_valid": True,
                    "event_count_consistent": True,
                    "event_ids_unique": True,
                    "timestamps_monotonic": True,
                    "privacy_safe": True,
                    "page_scope_valid": True,
                    "natural_non_recorder_event_count": 3,
                    "event_type_count": 2,
                    "private_extra": "PRIVATE-TRACE-TEXT",
                },
            },
            "browser": {
                "product_version": f"Chrome/{attempt['browser']['product_version']}",
                "viewport": viewport,
                "device_pixel_ratio": 1,
                "locale": attempt["browser"]["locale"],
                "timezone": attempt["browser"]["timezone"],
                "fresh_profile": True,
                "environment_guard": {
                    "requested_locale": attempt["browser"]["locale"],
                    "requested_timezone": attempt["browser"]["timezone"],
                    "observed_locale": attempt["browser"]["locale"],
                    "observed_timezone": attempt["browser"]["timezone"],
                    "capture_stability_verified": True,
                    "capture_stability_switch_count": 3,
                    "native_window_occlusion_disabled": True,
                    "verified": True,
                    "private_path": r"Z:\private\profile",
                },
                "episode_guard": {
                    "stable": True,
                    "sticky_lifecycle_monitoring": True,
                    "pre_validator_attested": True,
                    "post_validator_attested": True,
                    "clean_disarm_attested": True,
                    "attestation_count": 2,
                    "violation_count": 0,
                    "target_id_unchanged": True,
                    "loader_id_unchanged": True,
                    "document_token_unchanged": True,
                    "time_origin_unchanged": True,
                    "episode_start_unchanged": True,
                    "target_url": "http://127.0.0.1/private",
                },
            },
            "artifacts": {
                "final_screenshot": descriptor,
                "final_screenshot_validation": {
                    "png_header_valid": True,
                    "png_complete": True,
                    "chunk_crc_valid": True,
                    "image_data_decoded": True,
                    "chunk_count": 3,
                    "width": viewport["width"],
                    "height": viewport["height"],
                    "private_extra": "PRIVATE-SCREENSHOT-TEXT",
                },
                "framework": {"status": "complete", "message": "PRIVATE-FRAMEWORK-TEXT"},
            },
            "dependencies": {
                "snapshots": [
                    {
                        "environment": "browser-use",
                        "artifact": dependency_descriptor,
                        "path": r"Z:\private\cui-week1-dependency-freeze.txt",
                    }
                ],
                "private_root": r"Z:\private",
            },
            "provenance_complete": True,
            "artifact_bundle_complete": True,
            "formal_matrix_eligible": True,
            "analysis_eligible": True,
        }

    def test_exact_attempt_selection_rejects_suffixed_ids(self) -> None:
        matrix = build_matrix(load_manifest(ROOT / "benchmark_manifest.json"))
        self.assertEqual(runner.select_attempt_id(matrix, None), matrix)
        self.assertEqual(runner.select_attempt_id(matrix, matrix[4]["attempt_id"]), [matrix[4]])
        with self.assertRaisesRegex(ProtocolError, "unknown formal attempt_id"):
            runner.select_attempt_id(matrix, matrix[4]["attempt_id"] + "-suffix")

    def test_heavy_framework_self_test_has_separate_one_time_budget(self) -> None:
        self.assertGreaterEqual(runner.ADAPTER_SELF_TEST_TIMEOUT_SECONDS, 120)
        self.assertLess(runner.ADAPTER_SELF_TEST_TIMEOUT_SECONDS, self.attempt["limits"]["timeout_seconds"])
        self.assertNotEqual(
            runner.ADAPTER_SELF_TEST_TIMEOUT_SECONDS,
            runner.ADAPTER_PROCESS_OVERHEAD_SECONDS,
        )

    def test_formal_adapter_resolution_rejects_override_but_partial_allows_it(self) -> None:
        framework = self.attempt["framework"]
        override = json.dumps(["python", "custom-adapter.py"])
        with mock.patch.dict(os.environ, {framework["adapter_command_env"]: override}, clear=False):
            command, source = runner.resolve_adapter_command(framework)
            self.assertEqual(command, ["python", "custom-adapter.py"])
            self.assertTrue(source.startswith("environment:"))
            with self.assertRaisesRegex(ProtocolError, "forbids"):
                runner.resolve_adapter_command(framework, require_repository_default=True)

        changed = dict(framework)
        changed["default_adapter_command"] = ["{python}", "other-dispatch.py", framework["id"]]
        with self.assertRaisesRegex(ProtocolError, "exact adapter argv"):
            runner.resolve_adapter_command(changed, require_repository_default=True)

    def test_source_provenance_requires_exact_commit_manifest_and_clean_formal_tree(self) -> None:
        manifest_hash = self.attempt["manifest_sha256"]
        clean_results = [
            subprocess.CompletedProcess(["git"], 0, "d" * 40 + "\n", ""),
            subprocess.CompletedProcess(["git"], 0, "", ""),
        ]
        with mock.patch.object(runner.shutil, "which", return_value="git"), mock.patch.object(
            runner.subprocess, "run", side_effect=clean_results
        ):
            provenance = runner.capture_source_provenance(manifest_hash, require_clean=True)
        self.assertEqual(
            provenance,
            {"commit": "d" * 40, "clean": True, "manifest_sha256": manifest_hash},
        )

        dirty_results = [
            subprocess.CompletedProcess(["git"], 0, "d" * 40 + "\n", ""),
            subprocess.CompletedProcess(["git"], 0, " M experiments/week1/runner.py\n", ""),
        ]
        with mock.patch.object(runner.shutil, "which", return_value="git"), mock.patch.object(
            runner.subprocess, "run", side_effect=dirty_results
        ):
            with self.assertRaisesRegex(ProtocolError, "clean source commit"):
                runner.capture_source_provenance(manifest_hash, require_clean=True)

    def test_actual_unfiltered_historical_matrix_is_retired_before_preflight(self) -> None:
        args = SimpleNamespace(
            manifest=ROOT / "benchmark_manifest.json",
            output_dir=Path("unused"),
            framework=None,
            task=None,
            repetition=None,
            run_label=None,
            dry_run=False,
            preflight=False,
            allow_unready=False,
            require_clean=False,
        )
        with mock.patch.object(runner, "parse_args", return_value=args), mock.patch.object(
            runner, "is_canonical_tracked_manifest", return_value=True
        ), mock.patch.object(runner, "run_preflight") as preflight, mock.patch("builtins.print") as printed:
            code = runner.main()
        self.assertEqual(code, 2)
        preflight.assert_not_called()
        self.assertIn("Stagehand 3.7.1", printed.call_args.args[0])
        self.assertIn("0/27", printed.call_args.args[0])

    def test_external_27_row_manifest_is_demoted_to_partial_scope(self) -> None:
        """A byte-identical external copy cannot be mistaken for formal evidence."""

        with tempfile.TemporaryDirectory() as temporary:
            external_manifest = Path(temporary) / "external-manifest.json"
            external_manifest.write_text(
                (ROOT / "benchmark_manifest.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
            args = SimpleNamespace(
                manifest=external_manifest,
                output_dir=Path(temporary) / "out",
                framework=None,
                task=None,
                repetition=None,
                run_label="external-copy",
                dry_run=True,
                preflight=False,
                allow_unready=False,
                require_clean=False,
            )
            ready = {"ready": True, "checks": [], "schema_version": "test"}
            with mock.patch.object(runner, "parse_args", return_value=args), mock.patch.object(
                runner, "run_preflight", return_value=ready
            ) as preflight, mock.patch("builtins.print") as printed:
                code = runner.main()

        self.assertEqual(code, 0)
        self.assertFalse(preflight.call_args.kwargs["require_clean"])
        self.assertFalse(preflight.call_args.kwargs["require_repository_defaults"])
        payload = json.loads(printed.call_args.args[0])
        self.assertEqual(payload["evidence_scope"], "partial-debug-evidence")
        self.assertEqual(payload["planned_attempt_count"], 27)
        self.assertTrue(all(row["attempt_id"].startswith("partial__") for row in payload["attempts"]))

    def test_public_sanitizer_keeps_only_verifiable_guard_and_artifact_summaries(self) -> None:
        raw = self.complete_result()
        validate_adapter_result(raw, self.attempt)

        public = runner.public_adapter_result(raw)

        validate_adapter_result(public, self.attempt)
        serialized = json.dumps(public)
        self.assertNotIn("PRIVATE-", serialized)
        self.assertNotIn(r"Z:\private", serialized)
        self.assertNotIn("target_url", serialized)
        self.assertNotIn("private_extra", serialized)
        self.assertTrue(public["recorder"]["artifact_validation"]["privacy_safe"])
        self.assertTrue(public["browser"]["environment_guard"]["verified"])
        self.assertTrue(public["browser"]["episode_guard"]["sticky_lifecycle_monitoring"])
        self.assertTrue(public["browser"]["episode_guard"]["post_validator_attested"])
        self.assertEqual(public["browser"]["episode_guard"]["attestation_count"], 2)
        self.assertTrue(public["artifacts"]["final_screenshot_validation"]["png_header_valid"])
        self.assertTrue(public["artifacts"]["final_screenshot_validation"]["png_complete"])
        self.assertTrue(public["artifacts"]["final_screenshot_validation"]["image_data_decoded"])
        self.assertTrue(public["formal_matrix_eligible"])
        self.assertTrue(public["validator"]["task_setup_retained_private"])
        self.assertTrue(public["validator"]["task_info_retained_private"])
        self.assertEqual(
            public["dependencies"],
            {
                "snapshots": [
                    {
                        "environment": "browser-use",
                        "artifact": {
                            "artifact_id": "private-dependency-freeze-bbbbbbbbbbbbbbbb",
                            "retained_private": True,
                            "bytes": 48,
                            "sha256": "b" * 64,
                        },
                    }
                ]
            },
        )

    def test_partial_scope_forces_public_eligibility_claims_false(self) -> None:
        raw = self.complete_result()
        # The adapter's original output is internally valid for the formal
        # protocol, which is precisely why the public scope gate is needed.
        validate_adapter_result(raw, self.attempt)

        public = runner.public_adapter_result(raw, evidence_scope="partial-debug-evidence")

        self.assertFalse(public["formal_matrix_eligible"])
        self.assertFalse(public["analysis_eligible"])
        validate_adapter_result(public, self.attempt, evidence_scope="partial-debug-evidence")

    def test_public_error_sanitizer_keeps_codes_only(self) -> None:
        raw = self.complete_result()
        raw.update(
            {
                "status": "error",
                "error": {
                    "code": "framework_error",
                    "secondary_codes": [
                        "recorder_trace_invalid",
                        "recorder_trace_invalid",
                        "PRIVATE provider status 403",
                    ],
                    "detail": "PRIVATE provider response body",
                },
                "provenance_complete": False,
                "artifact_bundle_complete": False,
                "formal_matrix_eligible": False,
                "analysis_eligible": False,
            }
        )

        public = runner.public_adapter_result(raw)

        self.assertEqual(
            public["error"],
            {
                "code": "framework_error",
                "secondary_codes": ["recorder_trace_invalid"],
            },
        )
        self.assertNotIn("PRIVATE", json.dumps(public))
        validate_adapter_result(public, self.attempt)

    def test_partial_attempt_saves_scope_gated_result_even_if_adapter_claims_true(self) -> None:
        raw = self.complete_result()
        completed = subprocess.CompletedProcess(["adapter"], 0, json.dumps(raw), "")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(runner, "_run_adapter_process", return_value=completed), mock.patch.dict(
                os.environ, {"CUI_WEEK1_PRIVATE_ROOT": str(root / "private")}, clear=False
            ):
                record = runner.execute_attempt(
                    self.attempt,
                    root / "public",
                    evidence_scope="partial-debug-evidence",
                    source_provenance={
                        "commit": "c" * 40,
                        "clean": False,
                        "manifest_sha256": self.attempt["manifest_sha256"],
                    },
                )

            self.assertEqual(record["evidence_scope"], "partial-debug-evidence")
            self.assertFalse(record["result"]["formal_matrix_eligible"])
            self.assertFalse(record["result"]["analysis_eligible"])
            validate_adapter_result(
                record["result"], self.attempt, evidence_scope="partial-debug-evidence"
            )

    def test_windows_tree_termination_targets_only_the_owned_adapter_pid(self) -> None:
        class FakeProcess:
            pid = 424242
            returncode = None

            def __init__(self) -> None:
                self.killed = False

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.returncode = -1
                return self.returncode

            def kill(self):
                self.killed = True
                self.returncode = -9

        process = FakeProcess()
        taskkill = subprocess.CompletedProcess(["taskkill"], 0, "", "")
        with mock.patch.object(runner.subprocess, "run", return_value=taskkill) as called:
            detail = runner._terminate_process_tree(process, platform_name="nt")

        called.assert_called_once_with(
            ["taskkill", "/PID", "424242", "/T", "/F"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=runner.PROCESS_TREE_SHUTDOWN_SECONDS,
            check=False,
        )
        self.assertIn("owned PID 424242", detail)
        self.assertFalse(process.killed)

    def test_adapter_watchdog_invokes_tree_cleanup_and_preserves_private_stderr(self) -> None:
        process = mock.Mock()
        process.returncode = -9
        timeout = subprocess.TimeoutExpired(["adapter"], 660, stderr="partial-private-stderr")
        process.communicate.side_effect = [timeout, ("", "final-private-stderr")]
        with mock.patch.object(runner.subprocess, "Popen", return_value=process), mock.patch.object(
            runner, "_terminate_process_tree", return_value="owned process tree terminated"
        ) as terminate:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                runner._run_adapter_process(["adapter"], input_text="{}", timeout_seconds=660)

        terminate.assert_called_once_with(process)
        self.assertIn("final-private-stderr", raised.exception.stderr)
        self.assertIn("owned process tree terminated", raised.exception.stderr)

    def test_outer_watchdog_result_is_interrupted_and_never_eligible(self) -> None:
        error = subprocess.TimeoutExpired(
            ["adapter"],
            self.attempt["limits"]["timeout_seconds"] + runner.ADAPTER_PROCESS_OVERHEAD_SECONDS,
            stderr="PRIVATE-WATCHDOG-STDERR",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_root = root / "private"
            public_root = root / "public"
            entitlement, _ = provider_entitlement_fixture()
            with mock.patch.object(runner, "_run_adapter_process", side_effect=error), mock.patch.dict(
                os.environ, {"CUI_WEEK1_PRIVATE_ROOT": str(private_root)}, clear=False
            ):
                record = runner.execute_attempt(
                    self.attempt,
                    public_root,
                    source_provenance={
                        "commit": "c" * 40,
                        "clean": True,
                        "manifest_sha256": self.attempt["manifest_sha256"],
                    },
                    provider_entitlement=entitlement,
                )

            result = record["result"]
            validate_adapter_result(result, self.attempt)
            self.assertEqual(result["status"], "timeout")
            self.assertEqual(result["error"]["code"], "adapter_outer_timeout")
            self.assertEqual(result["recorder"]["status"], "interrupted")
            self.assertFalse(result["provenance_complete"])
            self.assertFalse(result["artifact_bundle_complete"])
            self.assertFalse(result["formal_matrix_eligible"])
            self.assertFalse(result["analysis_eligible"])
            self.assertEqual(result["dependencies"], {"snapshots": []})
            self.assertEqual(record["orchestrator"]["task_timeout_seconds"], 480)
            self.assertEqual(record["orchestrator"]["process_overhead_seconds"], 180)
            self.assertEqual(record["orchestrator"]["outer_watchdog_seconds"], 660)
            self.assertEqual(
                record["orchestrator"]["source_provenance"],
                {
                    "commit": "c" * 40,
                    "clean": True,
                    "manifest_sha256": self.attempt["manifest_sha256"],
                },
            )
            private_log = next((private_root / "week1" / "orchestrator_logs").glob("*.stderr.log"))
            log_text = private_log.read_text(encoding="utf-8")
            self.assertIn("PRIVATE-WATCHDOG-STDERR", log_text)
            self.assertIn("outer watchdog", log_text)


if __name__ == "__main__":
    unittest.main()
