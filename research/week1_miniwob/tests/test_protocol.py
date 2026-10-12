from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from research.week1_miniwob.protocol import (
    ADAPTER_RESULT_SCHEMA,
    ProtocolError,
    aggregate_records,
    build_matrix,
    formal_adapter_argv,
    load_manifest,
    validate_adapter_result,
)
from research.week1_miniwob.runner import summary_exit_code
from research.week1_miniwob.tests import provider_entitlement_fixture


ROOT = Path(__file__).resolve().parents[1]


class Week1ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_manifest(ROOT / "benchmark_manifest.json")
        cls.matrix = build_matrix(cls.manifest)

    def test_matrix_is_exactly_three_by_three_by_three(self) -> None:
        self.assertEqual(len(self.matrix), 27)
        self.assertEqual(len({row["attempt_id"] for row in self.matrix}), 27)
        self.assertEqual({row["framework"]["id"] for row in self.matrix}, {"browser-use", "skyvern", "stagehand"})
        self.assertEqual(
            {row["task"]["environment_id"] for row in self.matrix},
            {
                "browsergym/miniwob.click-checkboxes",
                "browsergym/miniwob.enter-text",
                "browsergym/miniwob.choose-list",
            },
        )
        configuration = self.manifest["model_configuration"]
        self.assertEqual(configuration["browser-use"]["temperature"], 0)
        self.assertEqual(configuration["browser-use"]["model_seed"], "attempt_seed")
        self.assertEqual(configuration["skyvern"]["temperature"], 1)
        self.assertIsNone(configuration["skyvern"]["model_seed"])
        self.assertEqual(configuration["skyvern"]["max_completion_tokens"], 8192)
        self.assertEqual(configuration["stagehand"]["requested_temperature"], 0)
        self.assertIsNone(configuration["stagehand"]["temperature"])
        self.assertIsNone(configuration["stagehand"]["model_seed"])
        security = {row["id"]: row["formal_security"] for row in self.manifest["frameworks"]}
        self.assertTrue(security["browser-use"]["allowed"])
        self.assertTrue(security["skyvern"]["allowed"])
        self.assertFalse(security["stagehand"]["allowed"])
        self.assertIn("GHSA-vxpw-j846-p89q", security["stagehand"]["blocking_advisories"])

    def test_framework_security_gate_is_explicit_and_fail_closed(self) -> None:
        missing = copy.deepcopy(self.manifest)
        del missing["frameworks"][0]["formal_security"]
        with self.assertRaisesRegex(ProtocolError, "formal_security"):
            build_matrix(missing)

        contradictory = copy.deepcopy(self.manifest)
        contradictory["frameworks"][2]["formal_security"]["allowed"] = True
        with self.assertRaisesRegex(ProtocolError, "security-allowed"):
            build_matrix(contradictory)

    def valid_result(self, attempt: dict) -> dict:
        descriptor = {
            "artifact_id": "private-test-0000000000000000",
            "retained_private": True,
            "bytes": 1,
            "sha256": "0" * 64,
        }
        dependency_descriptor = {
            "artifact_id": "private-dependency-freeze-1111111111111111",
            "retained_private": True,
            "bytes": 24,
            "sha256": "1" * 64,
        }
        dependency_environments = {
            "browser-use": ["browser-use"],
            "skyvern": ["orchestrator", "skyvern-local"],
            "stagehand": ["stagehand"],
        }[attempt["framework"]["id"]]
        return {
            "schema_version": ADAPTER_RESULT_SCHEMA,
            "attempt_id": attempt["attempt_id"],
            "status": "success",
            "agent": {
                "framework": attempt["framework"]["id"],
                "framework_version": attempt["framework"]["version"],
                "model": attempt["model"],
                "action_count": 4,
            },
            "validator": {
                "source": "browsergym_official_reward",
                "reward": 1,
                "terminated": True,
                "truncated": False,
                "task_setup_artifact": descriptor,
                "task_info_artifact": descriptor,
            },
            "recorder": {
                "capture_adapter": "browser_mv3",
                "source_class": "cui_agent",
                "status": "complete",
                "session_id": "session-001",
                "event_count": 7,
                "trace_artifact": descriptor,
                "artifact_validation": {
                    "schema_valid": True,
                    "event_count_consistent": True,
                    "event_ids_unique": True,
                    "timestamps_monotonic": True,
                    "privacy_safe": True,
                    "page_scope_valid": True,
                    "natural_non_recorder_event_count": 4,
                    "event_type_count": 3,
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
                    "width": attempt["browser"]["viewport"]["width"],
                    "height": attempt["browser"]["viewport"]["height"],
                },
            },
            "browser": {
                "product_version": f"Chrome/{attempt['browser']['product_version']}",
                "viewport": attempt["browser"]["viewport"],
                "device_pixel_ratio": 1,
                "locale": attempt["browser"]["locale"],
                "timezone": attempt["browser"]["timezone"],
                "environment_guard": {
                    "requested_locale": attempt["browser"]["locale"],
                    "requested_timezone": attempt["browser"]["timezone"],
                    "observed_locale": attempt["browser"]["locale"],
                    "observed_timezone": attempt["browser"]["timezone"],
                    "capture_stability_verified": True,
                    "capture_stability_switch_count": 3,
                    "native_window_occlusion_disabled": True,
                    "verified": True,
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
                },
                "fresh_profile": True,
            },
            "dependencies": {
                "snapshots": [
                    {"environment": environment, "artifact": dependency_descriptor}
                    for environment in dependency_environments
                ]
            },
            "provenance_complete": True,
            "artifact_bundle_complete": True,
            "formal_matrix_eligible": True,
            "analysis_eligible": True,
        }

    def formal_orchestrator(self, attempt: dict) -> dict:
        entitlement, started_at = provider_entitlement_fixture()
        return {
            "started_at": started_at,
            "provider_entitlement": entitlement,
            "adapter_command_source": "repository-default",
            "adapter_argv": formal_adapter_argv(attempt["framework"]["id"]),
            "source_provenance": {
                "commit": "c" * 40,
                "clean": True,
                "manifest_sha256": attempt["manifest_sha256"],
            },
        }

    def test_adapter_result_requires_official_reward_and_recorder(self) -> None:
        attempt = self.matrix[0]
        validate_adapter_result(self.valid_result(attempt), attempt)
        leaked = self.valid_result(attempt)
        leaked["validator"]["source"] = "agent_self_report"
        with self.assertRaisesRegex(ProtocolError, "official reward"):
            validate_adapter_result(leaked, attempt)
        no_trace = self.valid_result(attempt)
        no_trace["recorder"]["status"] = "incomplete"
        with self.assertRaisesRegex(ProtocolError, "complete recorder"):
            validate_adapter_result(no_trace, attempt)

    def test_success_cannot_be_claimed_for_zero_reward(self) -> None:
        attempt = self.matrix[0]
        result = self.valid_result(attempt)
        result["validator"]["reward"] = 0
        with self.assertRaisesRegex(ProtocolError, "success requires official reward 1"):
            validate_adapter_result(result, attempt)

    def test_error_contract_rejects_messages_and_ambiguous_secondary_codes(self) -> None:
        attempt = self.matrix[0]
        result = self.valid_result(attempt)
        result.update(
            {
                "status": "error",
                "error": {
                    "code": "framework_error",
                    "secondary_codes": ["recorder_trace_invalid"],
                },
                "provenance_complete": False,
                "artifact_bundle_complete": False,
                "formal_matrix_eligible": False,
                "analysis_eligible": False,
            }
        )
        validate_adapter_result(result, attempt)

        for invalid_error in (
            {"code": "framework_error", "detail": "private provider response"},
            {"code": "framework_error", "secondary_codes": ["framework_error"]},
            {
                "code": "framework_error",
                "secondary_codes": ["recorder_trace_invalid", "recorder_trace_invalid"],
            },
            {"code": "Framework Error"},
        ):
            with self.subTest(invalid_error=invalid_error):
                invalid = copy.deepcopy(result)
                invalid["error"] = invalid_error
                with self.assertRaises(ProtocolError):
                    validate_adapter_result(invalid, attempt)

    def test_provenance_claim_fails_closed_on_trace_screenshot_and_episode_evidence(self) -> None:
        attempt = self.matrix[0]
        mutations = (
            ("trace schema", ("recorder", "artifact_validation", "schema_valid"), False, "recorder evidence"),
            (
                "screenshot dimensions",
                ("artifacts", "final_screenshot_validation", "width"),
                999,
                "dimension-validated",
            ),
            (
                "screenshot truncated",
                ("artifacts", "final_screenshot_validation", "png_complete"),
                False,
                "dimension-validated",
            ),
            ("sticky episode", ("browser", "episode_guard", "loader_id_unchanged"), False, "guarded episode"),
            (
                "post-validator attestation",
                ("browser", "episode_guard", "post_validator_attested"),
                False,
                "guarded episode",
            ),
            ("locale", ("browser", "environment_guard", "observed_locale"), "fr-FR", "Chrome/environment"),
        )
        for label, path, value, message in mutations:
            with self.subTest(label=label):
                result = self.valid_result(attempt)
                target = result
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaisesRegex(ProtocolError, message):
                    validate_adapter_result(result, attempt)

    def test_eligibility_claims_are_explicit_and_logically_nested(self) -> None:
        attempt = self.matrix[0]
        missing = self.valid_result(attempt)
        missing.pop("analysis_eligible")
        with self.assertRaisesRegex(ProtocolError, "explicit boolean"):
            validate_adapter_result(missing, attempt)

        invalid = self.valid_result(attempt)
        invalid["artifact_bundle_complete"] = False
        invalid["formal_matrix_eligible"] = True
        with self.assertRaisesRegex(ProtocolError, "complete artifact bundle"):
            validate_adapter_result(invalid, attempt)

        missing_private_setup = self.valid_result(attempt)
        missing_private_setup["validator"].pop("task_setup_artifact")
        with self.assertRaisesRegex(ProtocolError, "task-setup and validator-info"):
            validate_adapter_result(missing_private_setup, attempt)

        missing_dependency_snapshot = self.valid_result(attempt)
        missing_dependency_snapshot["dependencies"]["snapshots"] = []
        with self.assertRaisesRegex(ProtocolError, "exact framework dependency snapshot set"):
            validate_adapter_result(missing_dependency_snapshot, attempt)

        wrong_dependency_artifact = self.valid_result(attempt)
        wrong_dependency_artifact["dependencies"]["snapshots"][0]["artifact"] = (
            wrong_dependency_artifact["recorder"]["trace_artifact"]
        )
        with self.assertRaisesRegex(ProtocolError, "dependency snapshot requires"):
            validate_adapter_result(wrong_dependency_artifact, attempt)

        error_without_bundle = self.valid_result(attempt)
        error_without_bundle["status"] = "error"
        error_without_bundle["validator"]["reward"] = None
        error_without_bundle["provenance_complete"] = False
        error_without_bundle["artifact_bundle_complete"] = False
        error_without_bundle["formal_matrix_eligible"] = False
        error_without_bundle["analysis_eligible"] = False
        error_without_bundle["dependencies"]["snapshots"] = []
        error_without_bundle["error"] = {"code": "framework_error"}
        validate_adapter_result(error_without_bundle, attempt)

    def test_complete_zero_action_error_stays_in_formal_denominator(self) -> None:
        """A failed planned attempt is evidence even when the agent emitted no input."""

        attempt = self.matrix[0]
        result = self.valid_result(attempt)
        result["status"] = "error"
        result["error"] = {"code": "framework_error"}
        result["agent"]["action_count"] = 0
        result["validator"].update({"reward": 0, "terminated": False, "truncated": True})
        result["recorder"]["artifact_validation"][
            "natural_non_recorder_event_count"
        ] = 0
        result["recorder"]["artifact_validation"]["event_type_count"] = 1
        result["formal_matrix_eligible"] = True
        result["analysis_eligible"] = False

        validate_adapter_result(result, attempt)

        records = []
        for planned in self.matrix:
            planned_result = result if planned is attempt else self.valid_result(planned)
            records.append(
                {
                    "schema_version": "cui-week1-attempt-record-v1",
                    "evidence_scope": "formal-full-matrix",
                    "attempt": planned,
                    "orchestrator": self.formal_orchestrator(planned),
                    "result": planned_result,
                }
            )
        summary = aggregate_records(records, self.matrix)
        self.assertEqual(summary["artifact_bundle_complete_attempts"], 27)
        self.assertEqual(summary["formal_matrix_eligible_attempts"], 27)
        self.assertEqual(summary["analysis_eligible_attempts"], 26)
        self.assertEqual(summary["status_counts"], {"error": 1, "success": 26})
        self.assertTrue(summary["all_27_formal_matrix_eligible"])
        self.assertFalse(summary["all_27_analysis_eligible"])

    def test_aggregation_separates_artifact_completeness_from_success(self) -> None:
        records = []
        for attempt in self.matrix:
            result = self.valid_result(attempt)
            if attempt["repetition"] == 3:
                result["status"] = "failure"
                result["validator"]["reward"] = 0
            records.append(
                {
                    "schema_version": "cui-week1-attempt-record-v1",
                    "evidence_scope": "formal-full-matrix",
                    "attempt": attempt,
                    "orchestrator": self.formal_orchestrator(attempt),
                    "result": result,
                }
            )
        summary = aggregate_records(records, self.matrix)
        self.assertTrue(summary["matrix_artifacts_complete"])
        self.assertTrue(summary["record_contracts_valid"])
        self.assertTrue(summary["source_provenance_consistent"])
        self.assertEqual(summary["source_provenance"]["commit"], "c" * 40)
        self.assertEqual(summary["artifact_bundle_complete_attempts"], 27)
        self.assertEqual(summary["formal_matrix_eligible_attempts"], 27)
        self.assertEqual(summary["analysis_eligible_attempts"], 27)
        self.assertTrue(summary["all_27_artifact_bundles_complete"])
        self.assertTrue(summary["all_27_formal_matrix_eligible"])
        self.assertTrue(summary["all_27_analysis_eligible"])
        self.assertTrue(summary["operationally_complete"])
        self.assertEqual(summary["official_reward_successes"], 18)
        self.assertEqual(summary["status_counts"], {"failure": 9, "success": 18})
        self.assertEqual(summary_exit_code(summary), 0)

        timed_out = copy.deepcopy(records)
        timed_out[0]["result"]["status"] = "timeout"
        timed_out[0]["result"]["validator"]["reward"] = None
        timed_out[0]["result"]["validator"]["truncated"] = True
        timeout_summary = aggregate_records(timed_out, self.matrix)
        self.assertTrue(timeout_summary["matrix_artifacts_complete"])
        self.assertFalse(timeout_summary["operationally_complete"])
        self.assertEqual(summary_exit_code(timeout_summary), 1)

    def test_aggregation_flags_payload_hash_scope_and_protocol_mismatches(self) -> None:
        records = []
        for attempt in self.matrix:
            records.append(
                {
                    "schema_version": "cui-week1-attempt-record-v1",
                    "evidence_scope": "formal-full-matrix",
                    "attempt": attempt,
                    "orchestrator": self.formal_orchestrator(attempt),
                    "result": self.valid_result(attempt),
                }
            )
        changed = copy.deepcopy(records)
        changed[0]["attempt"]["manifest_sha256"] = "f" * 64
        changed[1]["evidence_scope"] = "partial-debug-evidence"
        changed[2]["result"]["analysis_eligible"] = "yes"
        changed[3]["orchestrator"]["source_provenance"]["clean"] = False
        changed[4]["orchestrator"]["adapter_command_source"] = "environment:CUI_WEEK1_TEST_ADAPTER"
        changed[5]["orchestrator"]["adapter_argv"] = ["{python}", "other.py", "browser-use"]

        summary = aggregate_records(changed, self.matrix)

        self.assertTrue(summary["matrix_artifacts_complete"])
        self.assertFalse(summary["record_contracts_valid"])
        self.assertIn(changed[0]["attempt"]["attempt_id"], summary["manifest_hash_mismatch_ids"])
        self.assertIn(changed[0]["attempt"]["attempt_id"], summary["attempt_payload_mismatch_ids"])
        self.assertIn(changed[1]["attempt"]["attempt_id"], summary["evidence_scope_mismatch_ids"])
        self.assertIn(changed[2]["attempt"]["attempt_id"], summary["protocol_invalid_attempt_ids"])
        self.assertIn(
            changed[3]["attempt"]["attempt_id"],
            summary["source_provenance_invalid_attempt_ids"],
        )
        self.assertIn(
            changed[4]["attempt"]["attempt_id"],
            summary["adapter_source_invalid_attempt_ids"],
        )
        self.assertIn(
            changed[5]["attempt"]["attempt_id"],
            summary["adapter_argv_invalid_attempt_ids"],
        )
        self.assertEqual(summary["formal_matrix_eligible_attempts"], 21)
        self.assertEqual(summary["analysis_eligible_attempts"], 21)
        self.assertFalse(summary["all_27_formal_matrix_eligible"])
        self.assertFalse(summary["operationally_complete"])

    def test_formal_aggregate_rejects_attempt_outside_entitlement_window(self) -> None:
        records = [
            {
                "schema_version": "cui-week1-attempt-record-v1",
                "evidence_scope": "formal-full-matrix",
                "attempt": attempt,
                "orchestrator": self.formal_orchestrator(attempt),
                "result": self.valid_result(attempt),
            }
            for attempt in self.matrix
        ]
        records[0]["orchestrator"]["started_at"] = "2100-01-01T00:00:00Z"

        summary = aggregate_records(records, self.matrix)

        self.assertFalse(summary["record_contracts_valid"])
        self.assertFalse(summary["provider_entitlement_consistent"])
        self.assertEqual(
            summary["provider_entitlement_invalid_attempt_ids"],
            [records[0]["attempt"]["attempt_id"]],
        )
        self.assertEqual(summary["formal_matrix_eligible_attempts"], 26)

    def test_partial_scope_cannot_inflate_formal_eligibility_aggregation(self) -> None:
        """Adversarial partial records may claim eligibility but never count."""

        attempt = self.matrix[0]
        record = {
            "schema_version": "cui-week1-attempt-record-v1",
            "evidence_scope": "partial-debug-evidence",
            "attempt": attempt,
            "orchestrator": self.formal_orchestrator(attempt),
            # This deliberately models an untrusted adapter that made both
            # claims before the runner's public-result scope gate.
            "result": self.valid_result(attempt),
        }
        summary = aggregate_records([record], [attempt])
        self.assertIn(attempt["attempt_id"], summary["protocol_invalid_attempt_ids"])
        self.assertEqual(summary["formal_matrix_eligible_attempts"], 0)
        self.assertEqual(summary["analysis_eligible_attempts"], 0)
        self.assertFalse(summary["selected_formal_matrix_eligible"])
        self.assertFalse(summary["selected_analysis_eligible"])
        self.assertFalse(summary["all_27_formal_matrix_eligible"])

    def test_manifest_rejects_task_substitution(self) -> None:
        changed = copy.deepcopy(self.manifest)
        changed["tasks"][0]["environment_id"] = "browsergym/openended"
        path = ROOT / "benchmark_manifest.json"
        with self.assertRaisesRegex(ProtocolError, "task environment IDs"):
            from research.week1_miniwob.protocol import validate_manifest
            validate_manifest(changed)


if __name__ == "__main__":
    unittest.main()
