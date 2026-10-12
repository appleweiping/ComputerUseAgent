from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any

from research.scienceboard.artifacts import (
    EvidenceValidationError,
    validate_session_evidence,
    verify_stored_artifact_hashes,
)
from research.scienceboard.host_worker import TRANSITIONS, WorkerStateMachine
from computer_use_agent.trace.io import TraceWriter
from computer_use_agent.capture.x11.recorder import build_session_metadata


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "c8d5010bdba32afc5dfb16472873bed148c39a45"


def _chunk(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)


def png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    pixels = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(pixels)) + _chunk(b"IEND", b"")


class ArtifactGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((ROOT / "tasks/manifest.json").read_text(encoding="utf-8"))
        self.task = self.manifest["tasks"][0]
        self.expected = {
            "session_id": "sb-kalgebra-a01-aio-r01", "actor": "aio", "actor_type": "agent",
            "source_class": "reasoning_agent", "task_id": self.task["id"], "repetition": 1,
        }

    def _bundle(self, directory: Path, *, with_event: bool = True) -> dict[str, Any]:
        shutil.copy2(ROOT / "tasks" / self.task["local_file"], directory / "task.json")
        (directory / "worker_spec.json").write_text(
            json.dumps(
                {
                    "schema_version": "scienceboard-host-worker-spec-v3",
                    "session_id": self.expected["session_id"],
                    "task_id": self.task["id"],
                    "task_path": str(directory / "task.json"),
                    "task_binding": self.task,
                    "session_dir": str(directory),
                    "scienceboard_root": str(directory / "ScienceBoard"),
                    "actor": "aio",
                    "scienceboard_commit": COMMIT,
                    "task_canonical_sha256": self.task["canonical_payload_sha256"],
                    "model": "gpt-5.5",
                    "worker_source_sha256": "a" * 64,
                    "vm_path": str(directory / "Ubuntu.vmx"),
                    "snapshot": "sci_bench",
                    "community": "AllInOne",
                    "reasoning_effort": "medium",
                    "openai_chat_completions_url": "https://api.openai.com/v1/chat/completions",
                    "action_limit": 5,
                    "timeout_seconds": 480,
                    "protocol_version": self.manifest["protocol_version"],
                    "executor_version": f"scienceboard@{COMMIT}",
                    "guest_id": "scienceboard-vm-sci-bench-v1",
                    "guest_os_id": "ubuntu",
                    "guest_os_version": "22.04",
                    "screen_width": 1920,
                    "screen_height": 1080,
                    "guest_data_root": "/home/user/.local/share/computer-use-agent/sessions",
                    "guest_control_root": "/home/user/.local/share/computer-use-agent/control",
                    "guest_bundle_root": str(ROOT),
                    "guest_bundle_hashes": {},
                    "manual_stop_file": str(directory / ".manual-stop.json"),
                    "self_consent_confirmed": False,
                }
            ),
            encoding="utf-8",
        )
        machine = WorkerStateMachine(
            session_id=self.expected["session_id"],
            nonce="n" * 48,
            endpoint={"host": "127.0.0.1", "port": 12345},
            pid=123,
            prepared_evidence={"fixture": True},
        )
        for command, _ in TRANSITIONS.values():
            machine.transition(
                nonce="n" * 48,
                request_id=f"fixture:{command}",
                command=command,
                expected_state_hash=str(machine.state["state_hash"]),
                action=lambda command=command: {"command": command},
            )
        (directory / "worker_state.json").write_text(
            json.dumps(machine.state), encoding="utf-8"
        )
        args = argparse.Namespace(
            actor="aio", session_id=self.expected["session_id"], participant_code="unused",
            pair_id=None, task_id=self.task["id"], protocol_version=self.manifest["protocol_version"],
            executor_version=f"scienceboard@{COMMIT}", consent_confirmed=False,
            controller_confirmed=True, guest_id="scienceboard-vm-sci-bench-v1",
            guest_os_id="ubuntu", guest_os_version="22.04", screen_width=1920,
            screen_height=1080, scienceboard_commit=COMMIT,
        )
        writer = TraceWriter(directory / "trace.jsonl", build_session_metadata(args))
        if with_event:
            writer.append({
                "schema_version": "cui-trace-v1", "event_id": "event-001", "type": "pointermove",
                "epoch_ms": 1000.1, "monotonic_ms": 0.1,
                "viewport": {"width": 1920, "height": 1080, "device_pixel_ratio": 1},
                "target": {"role": "desktop_application", "application_id": "KAlgebra"},
                "key_class": None, "keyboard": None,
                "pointer": {"x": 1.0, "y": 2.0, "button": -1, "pointer_type": "mouse"},
                "wheel": None,
            })
        writer.close(status="complete", stopped_at="2026-08-08T00:00:01+00:00")
        (directory / "final_state.png").write_bytes(png(1920, 1080))
        (directory / "run.log").write_text("formal fixture log\n", encoding="utf-8")
        official = directory / "official"
        official.mkdir()
        result_path = official / "result.out"
        trajectory_path = official / "traj.jsonl"
        result_path.write_text("0", encoding="utf-8")
        trajectory_path.write_text("", encoding="utf-8")
        (directory / "validator_result.json").write_text(json.dumps({
            "schema_version": "scienceboard-validator-result-v2",
            "session_id": self.expected["session_id"], "task_id": self.task["id"],
            "passed": False, "evaluated_at": "2026-08-08T00:00:01+00:00",
            "stop": {"type": "TIMEOUT", "args": []},
            "evaluator": {
                "implementation": "ScienceBoard official Task.eval",
                "scienceboard_commit": COMMIT,
                "task_canonical_sha256": self.task["canonical_payload_sha256"],
                "module": "sci/KAlgebra/task.py",
                "class": "sci.KAlgebra.task.VMTask",
                "function": "eval",
                "source_sha256": "40f3a0e5e0ef649099ab7796cd91eef6df326cc34a29d128557371c33d22806f",
            },
            "official_evidence": {
                "result": {
                    "path": "official/result.out",
                    "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                    "value": "0",
                },
                "trajectory": {
                    "path": "official/traj.jsonl",
                    "sha256": hashlib.sha256(trajectory_path.read_bytes()).hexdigest(),
                    "size_bytes": 0,
                    "origin": "ScienceBoard Log.save produced no steps; explicit empty trajectory",
                },
            },
        }), encoding="utf-8")
        return validate_session_evidence(directory, expected=self.expected, manifest=self.manifest)

    def test_valid_failed_task_is_complete_evidence_and_hashes_are_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            result = self._bundle(directory)
            self.assertFalse(result["validator_passed"])
            run_manifest = {"artifacts": result["artifacts"]}
            verify_stored_artifact_hashes(directory, run_manifest)
            (directory / "run.log").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceValidationError, "changed"):
                verify_stored_artifact_hashes(directory, run_manifest)

    def test_invalid_png_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self._bundle(directory)
            (directory / "final_state.png").write_bytes(b"not-a-png")
            with self.assertRaisesRegex(EvidenceValidationError, "PNG signature"):
                validate_session_evidence(directory, expected=self.expected, manifest=self.manifest)

    def test_unofficial_validator_provenance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self._bundle(directory)
            path = directory / "validator_result.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["evaluator"]["implementation"] = "custom heuristic"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(EvidenceValidationError, "official"):
                validate_session_evidence(directory, expected=self.expected, manifest=self.manifest)

    def test_zero_event_failed_attempt_is_complete_and_missing_modalities_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self._bundle(Path(temp), with_event=False)
            self.assertEqual(result["trace_event_count"], 0)
            self.assertTrue(result["trace_empty"])
            self.assertEqual(
                result["trace_missing_modalities"],
                ["focus", "keyboard", "pointer", "wheel"],
            )

    def test_tampered_official_result_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self._bundle(directory)
            (directory / "official/result.out").write_text("1", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceValidationError, "result.out"):
                validate_session_evidence(
                    directory, expected=self.expected, manifest=self.manifest
                )

    def test_tampered_worker_phase_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self._bundle(directory)
            path = directory / "worker_state.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["phase"] = "evaluated"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(EvidenceValidationError, "worker state"):
                validate_session_evidence(
                    directory, expected=self.expected, manifest=self.manifest
                )

    def test_worker_spec_secret_field_is_rejected_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self._bundle(directory)
            path = directory / "worker_spec.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["runtime"] = {"api_key": "must-never-be-stored"}
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(EvidenceValidationError, "closed v3 contract"):
                validate_session_evidence(
                    directory, expected=self.expected, manifest=self.manifest
                )


if __name__ == "__main__":
    unittest.main()
