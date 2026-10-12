from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from computer_use_agent.capture.x11.recorder import build_session_metadata
from computer_use_agent.detector.evaluation import EvaluationConfig
from computer_use_agent.detector.pipeline import load_dataset_payload, run_pipeline
from computer_use_agent.detector.provenance import get_provenance_validator
from computer_use_agent.study.ledger import ledger_payload
from computer_use_agent.study.policy import snapshot_sha256
from computer_use_agent.trace.io import TraceWriter
from research.scienceboard.artifacts import validate_session_evidence
from research.scienceboard.detector_bridge import (
    BRIDGE_SCHEMA,
    SOURCE_STAGE,
    analysis_provenance,
    build_detector_dataset,
    validate_bridge_dataset,
    validate_provenance_payload,
)
from research.scienceboard.host_worker import TRANSITIONS, WorkerStateMachine
from research.scienceboard.orchestrate import (
    FORMAL_STUDY_CONFIG,
    RELEASE_CAPSULE,
    build_schedule,
    canonical_json_sha256,
    load_manifest,
    sha256,
)
from research.scienceboard.private_storage import PRIVATE_ROOT_ENV


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tasks" / "manifest.json"
COMMIT = "c8d5010bdba32afc5dfb16472873bed148c39a45"


def _chunk(name: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(name + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", checksum)


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    pixels = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(pixels))
        + _chunk(b"IEND", b"")
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


class DetectorBridgeTests(unittest.TestCase):
    def _formal_batch(self, batch: Path, determination: Path) -> None:
        manifest = load_manifest(MANIFEST_PATH)
        schedule = build_schedule(manifest)
        config = json.loads(FORMAL_STUDY_CONFIG.read_text(encoding="utf-8"))
        config["human_research_policy"].update(
            {
                "determination_status": "recorded",
                "determination_scope": "not_human_research",
                "determination_record_sha256": hashlib.sha256(
                    determination.read_bytes()
                ).hexdigest(),
                "self_human_formal_research_allowed": True,
                "other_participants_formal_research_allowed": False,
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
            protocol_sha256=sha256(MANIFEST_PATH),
            planned_ids=[cell["session_id"] for cell in schedule],
        )
        batch.mkdir(parents=True)
        _write_json(batch / "study_ledger.json", ledger)
        provenance = {
            "orchestrator_repository_commit": "a" * 40,
            "scienceboard_commit": COMMIT,
            "driver_config_sha256": "b" * 64,
            "guest_capability_report_sha256": "c" * 64,
            "task_matrix_sha256": sha256(MANIFEST_PATH),
            "task_matrix_canonical_sha256": canonical_json_sha256(MANIFEST_PATH),
            "vm_zip_sha256": "f" * 64,
            "snapshot": manifest["snapshot"],
            "agent_model": manifest["agent_model"],
            "selection_status": manifest["selection_status"],
        }
        task_by_id = {item["id"]: item for item in manifest["tasks"]}
        screenshot = _png(1920, 1080)
        for cell in schedule:
            session = batch / cell["session_id"]
            session.mkdir()
            task = task_by_id[cell["task_id"]]
            shutil.copy2(ROOT / "tasks" / task["local_file"], session / "task.json")
            _write_json(
                session / "worker_spec.json",
                {
                    "schema_version": "scienceboard-host-worker-spec-v3",
                    "session_id": cell["session_id"],
                    "task_id": cell["task_id"],
                    "task_path": str(session / "task.json"),
                    "task_binding": task,
                    "session_dir": str(session),
                    "scienceboard_root": str(batch / "ScienceBoard"),
                    "actor": cell["actor"],
                    "scienceboard_commit": COMMIT,
                    "task_canonical_sha256": task["canonical_payload_sha256"],
                    "model": manifest["agent_model"],
                    "worker_source_sha256": "a" * 64,
                    "vm_path": str(batch / "Ubuntu.vmx"),
                    "snapshot": manifest["snapshot"],
                    "community": cell["scienceboard_community"],
                    "reasoning_effort": "medium",
                    "openai_chat_completions_url": "https://api.openai.com/v1/chat/completions",
                    "action_limit": manifest["agent_action_limit"],
                    "timeout_seconds": (
                        manifest["human_timeout_seconds"]
                        if cell["actor"] == "human"
                        else manifest["agent_timeout_seconds"]
                    ),
                    "protocol_version": manifest["protocol_version"],
                    "executor_version": f"scienceboard@{COMMIT}",
                    "guest_id": manifest["guest_binding"]["guest_id"],
                    "guest_os_id": manifest["guest_binding"]["guest_os_id"],
                    "guest_os_version": manifest["guest_binding"]["guest_os_version"],
                    "screen_width": 1920,
                    "screen_height": 1080,
                    "guest_data_root": "/home/user/.local/share/computer-use-agent/sessions",
                    "guest_control_root": "/home/user/.local/share/computer-use-agent/control",
                    "guest_bundle_root": str(ROOT),
                    "guest_bundle_hashes": {},
                    "manual_stop_file": str(session / ".manual-stop.json"),
                    "self_consent_confirmed": cell["actor"] == "human",
                },
            )
            machine = WorkerStateMachine(
                session_id=cell["session_id"],
                nonce="n" * 48,
                endpoint={"host": "127.0.0.1", "port": 12345},
                pid=123,
                prepared_evidence={"fixture": True},
            )
            for command, _ in TRANSITIONS.values():
                machine.transition(
                    nonce="n" * 48,
                    request_id=f"{cell['session_id']}:{command}",
                    command=command,
                    expected_state_hash=str(machine.state["state_hash"]),
                    action=lambda command=command: {"command": command},
                )
            _write_json(session / "worker_state.json", machine.state)
            if cell["actor"] == "human":
                _write_json(
                    session / ".manual-stop.json",
                    {
                        "schema_version": "scienceboard-manual-stop-v1",
                        "session_id": cell["session_id"],
                        "stop_type": "TIMEOUT",
                        "stop_args": [],
                        "confirmed_at": "2026-08-08T00:00:00+00:00",
                    },
                )
            metadata = build_session_metadata(
                argparse.Namespace(
                    actor=cell["actor"],
                    session_id=cell["session_id"],
                    participant_code="self-human-001",
                    pair_id=None,
                    task_id=cell["task_id"],
                    protocol_version=manifest["protocol_version"],
                    executor_version=f"scienceboard@{COMMIT}",
                    consent_confirmed=cell["actor"] == "human",
                    controller_confirmed=cell["actor"] != "human",
                    guest_id=manifest["guest_binding"]["guest_id"],
                    guest_os_id=manifest["guest_binding"]["guest_os_id"],
                    guest_os_version=manifest["guest_binding"]["guest_os_version"],
                    screen_width=1920,
                    screen_height=1080,
                    scienceboard_commit=COMMIT,
                )
            )
            writer = TraceWriter(session / "trace.jsonl", metadata)
            order = int(cell["order"])
            writer.append(
                {
                    "schema_version": "cui-trace-v1",
                    "event_id": f"event-{order:02d}",
                    "type": "pointermove",
                    "epoch_ms": 1_000.0 + order,
                    "monotonic_ms": 0.1,
                    "viewport": {
                        "width": 1920,
                        "height": 1080,
                        "device_pixel_ratio": 1,
                    },
                    "target": {
                        "role": "desktop_application",
                        "application_id": cell["application"],
                    },
                    "key_class": None,
                    "keyboard": None,
                    "pointer": {
                        "x": float(order),
                        "y": float(order + 1),
                        "button": -1,
                        "pointer_type": "mouse",
                    },
                    "wheel": None,
                }
            )
            writer.close(
                status="complete",
                stopped_at="2026-08-08T00:00:01+00:00",
            )
            (session / "final_state.png").write_bytes(screenshot)
            (session / "run.log").write_text(
                f"fixture formal outcome {cell['session_id']}\n", encoding="utf-8"
            )
            evaluator_contract = {
                "KAlgebra": (
                    "sci/KAlgebra/task.py",
                    "40f3a0e5e0ef649099ab7796cd91eef6df326cc34a29d128557371c33d22806f",
                ),
                "Celestia": (
                    "sci/Celestia/task.py",
                    "50fbe14ea0129b8620c4898e4356df266ae68a8112d687b3912fa40465fc72fb",
                ),
                "GrassGIS": (
                    "sci/GrassGIS/task.py",
                    "606f5876496ce4f17352d3ae5d8a83df9ebe9bf471633290cbec764c085ea973",
                ),
            }
            evaluator_module, evaluator_hash = evaluator_contract[task["application"]]
            official = session / "official"
            official.mkdir()
            official_result = official / "result.out"
            official_trajectory = official / "traj.jsonl"
            official_result.write_text("0", encoding="utf-8")
            official_trajectory.write_text("", encoding="utf-8")
            _write_json(
                session / "validator_result.json",
                {
                    "schema_version": "scienceboard-validator-result-v2",
                    "session_id": cell["session_id"],
                    "task_id": cell["task_id"],
                    "passed": False,
                    "evaluated_at": "2026-08-08T00:00:01+00:00",
                    "stop": {"type": "TIMEOUT", "args": []},
                    "evaluator": {
                        "implementation": "ScienceBoard official Task.eval",
                        "scienceboard_commit": COMMIT,
                        "task_canonical_sha256": task["canonical_payload_sha256"],
                        "module": evaluator_module,
                        "class": f"sci.{task['application']}.task.VMTask",
                        "function": "eval",
                        "source_sha256": evaluator_hash,
                    },
                    "official_evidence": {
                        "result": {
                            "path": "official/result.out",
                            "sha256": hashlib.sha256(official_result.read_bytes()).hexdigest(),
                            "value": "0",
                        },
                        "trajectory": {
                            "path": "official/traj.jsonl",
                            "sha256": hashlib.sha256(official_trajectory.read_bytes()).hexdigest(),
                            "size_bytes": 0,
                            "origin": "ScienceBoard Log.save produced no steps; explicit empty trajectory",
                        },
                    },
                },
            )
            evidence = validate_session_evidence(
                session,
                expected=cell,
                manifest=manifest,
            )
            run = {
                "schema_version": "scienceboard-session-run-v2",
                "protocol_version": manifest["protocol_version"],
                "session_id": cell["session_id"],
                "task_id": cell["task_id"],
                "actor": cell["actor"],
                "actor_type": cell["actor_type"],
                "source_class": cell["source_class"],
                "scienceboard_community": cell["scienceboard_community"],
                "repetition": cell["repetition"],
                "schedule_order": cell["order"],
                "source_provenance": provenance,
                "status": "task_failed",
                "evidence_valid": True,
                "validator_passed": False,
                "trace_event_count": evidence["trace_event_count"],
                "trace_duration_ms": evidence["trace_duration_ms"],
                "trace_quality": evidence["trace_quality"],
                "trace_empty": evidence["trace_empty"],
                "trace_missing_modalities": evidence["trace_missing_modalities"],
                "artifacts": evidence["artifacts"],
            }
            _write_json(session / "run_manifest.json", run)
        _write_json(
            batch / "batch_manifest.json",
            {
                "schema_version": "scienceboard-batch-fixture-v1",
                "formal_matrix_evidence_complete": True,
                "source_provenance": provenance,
            },
        )

    def test_bridge_registers_its_provenance_validator(self) -> None:
        self.assertIs(get_provenance_validator(SOURCE_STAGE), validate_provenance_payload)
        with self.assertRaisesRegex(ValueError, "complete dataset payload"):
            validate_provenance_payload({"provenance": {"source_stage": SOURCE_STAGE}})

    def test_exact_batch_builds_bound_path_free_dataset_and_tampering_fails(self) -> None:
        reference = json.loads(FORMAL_STUDY_CONFIG.read_text(encoding="utf-8"))[
            "human_research_policy"
        ]["determination_record_reference"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            determination = root / "determination-record.txt"
            determination.write_text("reviewed fixture determination\n", encoding="utf-8")
            batch = root / "formal-batch"
            self._formal_batch(batch, determination)
            with mock.patch.dict(
                os.environ,
                {
                    PRIVATE_ROOT_ENV: str(root),
                    reference.removeprefix("env:"): str(determination),
                },
            ):
                payload = build_detector_dataset(batch)
                audit = validate_bridge_dataset(payload)
                dataset_path = root / "detector" / "dataset.json"
                dataset_path.parent.mkdir()
                _write_json(dataset_path, payload)
                loaded, loaded_provenance = load_dataset_payload(dataset_path)

                self.assertEqual(payload["provenance"]["schema_version"], BRIDGE_SCHEMA)
                self.assertEqual(audit["record_count"], 27)
                self.assertEqual(audit["class_counts"], {"agent": 18, "human": 9})
                self.assertEqual(len(loaded), 27)
                self.assertEqual(loaded_provenance, payload["provenance"])
                assert loaded_provenance is not None
                result = run_pipeline(
                    loaded,
                    config=EvaluationConfig(
                        epochs=2,
                        calibration_epochs=2,
                        bootstrap_repetitions=1,
                        permutation_repetitions=1,
                    ),
                    dataset_provenance=loaded_provenance,
                )
                self.assertEqual(
                    result["source_provenance"]["schema_version"],
                    "scienceboard-detector-analysis-source-v1",
                )
                self.assertFalse(result["formal_claim_permitted"])
                self.assertEqual(result["schema_version"], "cui-detector-analysis-v2")
                self.assertEqual(
                    list(result["descriptive"]["by_task"]),
                    ["task-01", "task-02", "task-03"],
                )
                self.assertFalse(
                    result["trajectory_artifact"]["privacy"][
                        "raw_event_sequences_embedded"
                    ]
                )
                self.assertNotIn(
                    load_manifest(MANIFEST_PATH)["tasks"][0]["id"],
                    json.dumps(result["descriptive"], sort_keys=True),
                )
                serialized = json.dumps(payload, sort_keys=True)
                self.assertNotIn(str(root), serialized)
                self.assertNotIn("trace.jsonl", serialized)
                self.assertNotIn("run.log", serialized)
                self.assertNotIn("final_state.png", serialized)
                self.assertNotIn('"events"', serialized)
                result_binding = analysis_provenance(payload["provenance"])
                result_serialized = json.dumps(result_binding, sort_keys=True)
                self.assertNotIn("record_bindings", result_binding)
                self.assertIn("record_bindings_sha256", result_binding)
                self.assertNotIn(
                    payload["records"][0]["source_sha256"], result_serialized
                )
                self.assertFalse(result_binding["raw_sequences_embedded"])

                tampered = copy.deepcopy(payload)
                tampered["records"][0]["source_sha256"] = "0" * 64
                with self.assertRaisesRegex(ValueError, "dataset hash|trace hash"):
                    validate_bridge_dataset(tampered)

                denied = copy.deepcopy(payload)
                denied_binding = denied["provenance"]["study_ledger"]["binding"]
                denied_config = denied_binding["formal_config_snapshot"]
                denied_config["human_research_policy"]["detector_training_allowed"] = False
                denied_binding["formal_config_sha256"] = snapshot_sha256(denied_config)
                denied["provenance"]["study_ledger"] = ledger_payload(
                    stage="scienceboard",
                    binding=denied_binding,
                    protocol_sha256=sha256(MANIFEST_PATH),
                    planned_ids=[
                        cell["session_id"]
                        for cell in build_schedule(load_manifest(MANIFEST_PATH))
                    ],
                )
                with self.assertRaisesRegex(ValueError, "does not allow detector training"):
                    validate_bridge_dataset(denied)

                (batch / loaded_provenance["record_bindings"][0]["session_id"] / "run.log").write_text(
                    "tampered after manifest\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, "changed"):
                    build_detector_dataset(batch)


if __name__ == "__main__":
    unittest.main()
