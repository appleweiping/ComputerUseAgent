from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from computer_use_agent.study.ledger import ledger_payload
from computer_use_agent.study.policy import (
    FormalStudyError,
    require_human_research_use,
    snapshot_sha256,
)
from research.scienceboard import PROTOCOL_VERSION
from research.scienceboard.orchestrate import (
    FORMAL_STUDY_CONFIG,
    RELEASE_CAPSULE,
    _private_output,
    _render_argv,
    build_schedule,
    check_config_matrix,
    config_manifest_path,
    default_output_root,
    filtered_schedule,
    load_manifest,
    read_formal_config,
    select_session_id,
    translate_legacy_path,
)
from research.scienceboard.preflight import (
    TASK_SELECTION_CONFIRMED_STATUS,
    _driver_is_valid,
    _task_selection_gate,
    run_preflight,
)
from research.scienceboard.private_storage import PRIVATE_ROOT_ENV, PrivateStorageError
from research.scienceboard.publication import PUBLIC_SCHEMA, validate_public_summary
from research.scienceboard.vm_provenance import certify

MANIFEST = Path(__file__).resolve().parents[1] / "tasks" / "manifest.json"
REPO = Path(__file__).resolve().parents[3]


def _public_summary(schedule: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    config = json.loads(FORMAL_STUDY_CONFIG.read_text(encoding="utf-8"))
    config["human_research_policy"].update(
        {
            "determination_status": "recorded",
            "determination_scope": "not_human_research",
            "determination_record_sha256": "e" * 64,
            "self_human_formal_research_allowed": True,
            "detector_training_allowed": True,
        }
    )
    capsule = json.loads(RELEASE_CAPSULE.read_text(encoding="utf-8"))
    capsule["sealed"] = True
    binding = {
        "formal_config": "research/configs/formal-study.json",
        "formal_config_sha256": snapshot_sha256(config),
        "release_capsule": "research/archive/release-capsule-2026-08.json",
        "release_capsule_sha256": snapshot_sha256(capsule),
        "formal_config_snapshot": config,
        "release_capsule_snapshot": capsule,
    }
    ledger = ledger_payload(
        stage="scienceboard",
        binding=binding,
        protocol_sha256=hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        planned_ids=[cell["session_id"] for cell in schedule],
    )
    descriptors = [
        {
            "session_id": cell["session_id"],
            "actor": cell["actor"],
            "task_id": cell["task_id"],
            "repetition": cell["repetition"],
            "status": "task_failed",
            "validator_passed": False,
            "trace_event_count": 1,
            "trace_duration_ms": 1.0,
            "bundle_root_sha256": "f" * 64,
        }
        for cell in sorted(schedule, key=lambda item: item["session_id"])
    ]
    return {
        "schema_version": PUBLIC_SCHEMA,
        "planned_sessions": 27,
        "artifact_bundle_complete_sessions": 27,
        "formal_matrix_evidence_complete": True,
        "by_actor": {"aio": 9, "human": 9, "seeact": 9},
        "task_outcomes": {"validator_failed": 27, "validator_passed": 0},
        "session_descriptors": descriptors,
        "study_ledger": ledger,
        "source_provenance": {
            "orchestrator_repository_commit": "a" * 40,
            "scienceboard_commit": "c8d5010bdba32afc5dfb16472873bed148c39a45",
            "driver_config_sha256": "b" * 64,
            "guest_capability_report_sha256": "c" * 64,
            "task_matrix_sha256": "d" * 64,
            "task_matrix_canonical_sha256": "e" * 64,
            "vm_zip_sha256": "f" * 64,
            "snapshot": "sci_bench",
            "agent_model": "gpt-5.5",
            "selection_status": manifest["selection_status"],
        },
        "source_batch_manifest_sha256": "a" * 64,
        "human_research_attestation": {
            "determination_scope": "not_human_research",
            "determination_record_sha256": "e" * 64,
            "verification_method": "runtime_sha256_match_before_publication",
            "private_record_published": False,
        },
        "human_raw_traces_published": False,
        "claim_class": "single_participant_pipeline_probe",
    }


class OrchestrationTests(unittest.TestCase):
    def test_task_selection_gate_requires_exact_confirmation(self) -> None:
        for observed in (
            None,
            "",
            "researcher-selected, pending independent confirmation",
            "independently confirmed",
            "Researcher-selected, independently confirmed",
            TASK_SELECTION_CONFIRMED_STATUS + " ",
            True,
        ):
            with self.subTest(observed=observed):
                passed, detail = _task_selection_gate(
                    {} if observed is None else {"selection_status": observed}
                )
                self.assertFalse(passed)
                self.assertIn("no confirmed task selection", detail)

        passed, detail = _task_selection_gate(
            {"selection_status": TASK_SELECTION_CONFIRMED_STATUS}
        )
        self.assertTrue(passed)
        self.assertIn(TASK_SELECTION_CONFIRMED_STATUS, detail)

    def test_canonical_pending_selection_is_a_blocking_preflight_gate(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "research.scienceboard.preflight._discover_vmrun",
                return_value=None,
            ),
        ):
            report = run_preflight(MANIFEST)
        selection = next(
            row for row in report["checks"] if row["name"] == "task_selection_confirmed"
        )
        self.assertFalse(selection["passed"])
        self.assertTrue(selection["blocking"])
        self.assertIn("pending independent confirmation", selection["detail"])
        private = next(row for row in report["checks"] if row["name"] == "private_root")
        self.assertFalse(private["passed"])
        self.assertIn(PRIVATE_ROOT_ENV, private["detail"])
        self.assertFalse(report["ready_for_formal_run"])

    def test_schedule_is_deterministic_complete_and_unique(self) -> None:
        manifest = load_manifest(MANIFEST)
        self.assertEqual(manifest["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(manifest["schema_version"], "scienceboard-task-matrix-v2")
        self.assertEqual(
            manifest["required_artifacts_per_session"],
            [
                "trace.jsonl",
                "task.json",
                "worker_spec.json",
                "worker_state.json",
                "final_state.png",
                "validator_result.json",
                "official/result.out",
                "official/traj.jsonl",
                "run.log",
                "run_manifest.json",
            ],
        )
        self.assertEqual(
            manifest["required_artifacts_by_actor"],
            {"human": [".manual-stop.json"], "aio": [], "seeact": []},
        )
        first = build_schedule(manifest)
        second = build_schedule(manifest)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 27)
        self.assertEqual(len({cell["session_id"] for cell in first}), 27)
        self.assertEqual({cell["order"] for cell in first}, set(range(1, 28)))

        public = _public_summary(first, manifest)
        self.assertEqual(validate_public_summary(public), {"aio": 9, "human": 9, "seeact": 9})
        public["session_descriptors"][0]["bundle_root_sha256"] = "not-a-hash"
        with self.assertRaisesRegex(ValueError, "bundle root"):
            validate_public_summary(public)

    def test_zero_event_sessions_are_publishable_but_negative_counts_are_not(self) -> None:
        manifest = load_manifest(MANIFEST)
        public = _public_summary(build_schedule(manifest), manifest)
        empty = copy.deepcopy(public)
        empty["session_descriptors"][0]["trace_event_count"] = 0
        self.assertEqual(validate_public_summary(empty), {"aio": 9, "human": 9, "seeact": 9})
        for count, duration in ((-1, 1.0), (True, 1.0), (1, 0.0)):
            with self.subTest(count=count, duration=duration):
                broken = copy.deepcopy(public)
                broken["session_descriptors"][0]["trace_event_count"] = count
                broken["session_descriptors"][0]["trace_duration_ms"] = duration
                with self.assertRaisesRegex(ValueError, "trace completeness"):
                    validate_public_summary(broken)

    def test_session_selection_is_exact_and_rejects_prefixes(self) -> None:
        schedule = [{"session_id": "sb-one"}, {"session_id": "sb-two"}]
        self.assertEqual(select_session_id(schedule, None), schedule)
        self.assertEqual(select_session_id(schedule, "sb-two"), [schedule[1]])
        with self.assertRaisesRegex(ValueError, "Unknown formal"):
            select_session_id(schedule, "sb")

    def test_actor_filter_retains_three_tasks_by_three_repetitions(self) -> None:
        schedule = build_schedule(load_manifest(MANIFEST))
        human = filtered_schedule(schedule, actor="human", task_id=None)
        self.assertEqual(len(human), 9)
        self.assertEqual({cell["actor"] for cell in human}, {"human"})
        config = read_formal_config(FORMAL_STUDY_CONFIG)
        with self.assertRaisesRegex(FormalStudyError, "determination is not recorded"):
            require_human_research_use(config, use="scienceboard_formal")

    def test_config_binds_the_canonical_manifest_and_matrix(self) -> None:
        config = read_formal_config(FORMAL_STUDY_CONFIG)
        manifest = load_manifest(MANIFEST)
        self.assertEqual(config_manifest_path(config, FORMAL_STUDY_CONFIG), MANIFEST.resolve())
        check_config_matrix(config, manifest)
        legacy = copy.deepcopy(config)
        legacy["scienceboard"]["manifest"] = "experiments/scienceboard/tasks/manifest.json"
        self.assertEqual(config_manifest_path(legacy, FORMAL_STUDY_CONFIG), MANIFEST.resolve())
        self.assertEqual(
            translate_legacy_path("experiments/scienceboard/tasks/manifest.json"),
            "research/scienceboard/tasks/manifest.json",
        )
        foreign = copy.deepcopy(config)
        foreign["scienceboard"]["manifest"] = "../week1_miniwob/benchmark_manifest.json"
        with self.assertRaises(FormalStudyError):
            config_manifest_path(foreign, FORMAL_STUDY_CONFIG)
        widened = copy.deepcopy(config)
        widened["scienceboard"]["matrix"]["actors"] = ["human", "aio", "other"]
        with self.assertRaisesRegex(FormalStudyError, "matrix differs"):
            check_config_matrix(widened, manifest)

    def test_raw_output_requires_the_private_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            private = Path(temp).resolve() / "private"
            with mock.patch.dict(os.environ, {PRIVATE_ROOT_ENV: str(private)}):
                accepted = _private_output(private / "scienceboard" / "raw")
                self.assertEqual(accepted, private / "scienceboard" / "raw")
                self.assertEqual(
                    _private_output(Path("scienceboard/raw")), private / "scienceboard" / "raw"
                )
                self.assertEqual(default_output_root(), private / "scienceboard" / "formal")
                for rejected in (
                    REPO / "research" / "evidence" / "scienceboard" / "raw",
                    REPO / "private_data" / "scienceboard" / "raw",
                    Path(temp).resolve() / "elsewhere" / "raw",
                ):
                    with self.subTest(rejected=str(rejected)):
                        with self.assertRaises(PrivateStorageError):
                            _private_output(rejected)
            with mock.patch.dict(os.environ, {PRIVATE_ROOT_ENV: str(REPO / "private_data")}):
                with self.assertRaises(PrivateStorageError):
                    _private_output(REPO / "private_data" / "scienceboard")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(PRIVATE_ROOT_ENV, None)
                with self.assertRaisesRegex(PrivateStorageError, PRIVATE_ROOT_ENV):
                    _private_output(private / "scienceboard")
                with self.assertRaisesRegex(PrivateStorageError, PRIVATE_ROOT_ENV):
                    default_output_root()

    def test_driver_substitution_is_argv_only_and_unknown_tokens_fail(self) -> None:
        rendered = _render_argv(["tool", "--task", "{task_id}"], {"task_id": "a-01"})
        self.assertEqual(rendered, ["tool", "--task", "a-01"])
        with self.assertRaises(ValueError):
            _render_argv(["{missing}"], {})

    def test_distributed_example_driver_is_deliberately_blocked(self) -> None:
        valid, detail = _driver_is_valid(MANIFEST.parents[1] / "driver.example.json")
        self.assertFalse(valid)
        self.assertIn("unresolved", detail)

    def test_resolved_v3_driver_is_valid_and_guest_agent_placeholder_is_rejected(self) -> None:
        source = MANIFEST.parents[1] / "driver.example.json"
        manifest = load_manifest(MANIFEST)
        payload = json.loads(source.read_text(encoding="utf-8"))
        for specification in payload["commands"].values():
            specification["argv"] = [
                sys.executable if value == "REPLACE_WITH_PINNED_HOST_PYTHON" else value
                for value in specification["argv"]
            ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "driver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            valid, detail = _driver_is_valid(path, manifest)
            self.assertTrue(valid, detail)
            payload["commands"]["run_aio"]["argv"].extend(
                ["--forbidden-guest-launcher", "{aio_launcher}"]
            )
            path.write_text(json.dumps(payload), encoding="utf-8")
            valid, detail = _driver_is_valid(path, manifest)
            self.assertFalse(valid)
            self.assertIn("forbidden guest-side", detail)

    def test_vm_certificate_hashes_without_recording_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "VM.zip"
            archive.write_bytes(b"fixture-vm")
            expected = hashlib.sha256(b"fixture-vm").hexdigest()
            certificate = certify(archive, expected_sha256=expected)
            self.assertTrue(certificate["passed"])
            self.assertNotIn(str(archive.parent), json.dumps(certificate))

    def test_legacy_browser_pilot_is_explicitly_relabelled(self) -> None:
        correction = json.loads(
            (
                REPO / "research/evidence/scripted_simulator_control_v0.2.1/correction_manifest.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            correction["corrected_interpretation"]["source_class"], "scripted_automation_control"
        )
        self.assertEqual(correction["replacement_formal_protocol"]["collected_sessions"], 0)
        self.assertEqual(len(correction["runs"]), 9)
        for item in correction["runs"]:
            task_id, repetition = item["run_id"].rsplit("-rep-", 1)
            run_result = (
                REPO
                / "research/evidence/scripted_simulator_control_v0.2.1/runs"
                / task_id
                / f"rep-{repetition}"
                / "run_result.json"
            )
            self.assertTrue(run_result.is_file(), item["run_id"])
            payload = json.loads(run_result.read_text(encoding="utf-8"))
            self.assertEqual(payload["executor"], item["executor"])
            trace = REPO / "research/evidence/scripted_simulator_control_v0.2.1" / item["trace_path"]
            self.assertEqual(hashlib.sha256(trace.read_bytes()).hexdigest(), item["trace_sha256"])


class CommandLineTests(unittest.TestCase):
    """The documented read-only commands work on a clean clone."""

    def _run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CUA_", "SCIENCEBOARD_", "VM_PATH", "VMRUN_PATH"))
        }
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-B", "-m", *argv],
            cwd=REPO,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            check=False,
        )

    def test_dry_run_prints_27_cells_without_private_root_or_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing_capsule = Path(temp) / "no-capsule.json"
            completed = self._run(
                "research.scienceboard.orchestrate", "--dry-run", "--capsule", str(missing_capsule)
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        plan = json.loads(completed.stdout)
        self.assertEqual(plan["execution_status"], "dry_run")
        self.assertIs(plan["writes_performed"], False)
        self.assertEqual(plan["session_count"], 27)
        self.assertEqual(len(plan["schedule"]), 27)
        self.assertFalse(plan["formal_run_ready"])
        self.assertEqual(plan["formal_study_binding"]["status"], "not_evaluated_in_dry_run")

    def test_actor_filtered_dry_run_and_rejected_external_manifest(self) -> None:
        completed = self._run("research.scienceboard.orchestrate", "--actor", "aio", "--dry-run")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["session_count"], 9)
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / "manifest.json"
            copied.write_bytes(MANIFEST.read_bytes())
            rejected = self._run(
                "research.scienceboard.orchestrate", "--dry-run", "--manifest", str(copied)
            )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("external or copied", rejected.stderr)

    def test_formal_run_fails_closed_without_a_sealed_capsule(self) -> None:
        completed = self._run("research.scienceboard.orchestrate", "--confirm-formal-run")
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(json.loads(completed.stderr)["ready"])

    def test_preflight_allow_unready_exits_zero(self) -> None:
        completed = self._run("research.scienceboard.preflight", "--json", "--allow-unready")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["ready_for_formal_run"])
        self.assertIs(report["secret_values_logged"], False)
        blocked = self._run("research.scienceboard.preflight", "--json")
        self.assertEqual(blocked.returncode, 2)


if __name__ == "__main__":
    unittest.main()
