"""The host deploys exactly one hash-bound zipapp into the guest and runs it by path."""

from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from computer_use_agent.capture.x11.bundle import BUNDLE_NAME
from computer_use_agent.capture.x11.bundle import build as build_guest_bundle
from research.scienceboard.official_runtime import GUEST_BUNDLE_INSTALLER, OfficialRuntime
from research.scienceboard.worker_contract import (
    DEFAULT_GUEST_CONTROL_ROOT,
    DEFAULT_GUEST_DATA_ROOT,
    GUEST_BUNDLE_NAME,
    GUEST_PYTHON,
    OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
    WORKER_SPEC_FIELDS,
    WorkerProtocolError,
)

SESSION_ID = "sb-kalgebra-a01-human-r01"


class FakeGuestFiles:
    """Text-only guest file API, like ScienceBoard's VManager."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def write_file(self, path: str, content: str) -> bool:
        self.files[path] = content
        return True

    def read_file(self, path: str) -> str | None:
        return self.files.get(path)


class FakeTask:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def _execute(self, *, command: list[str], shell: bool) -> bool:
        assert shell is False
        self.commands.append(list(command))
        return True


class GuestDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        session = self.root / "session"
        session.mkdir()
        (session / "task.json").write_text("{}\n", encoding="utf-8")
        self.bundle = build_guest_bundle(session / "guest_bundle" / BUNDLE_NAME)
        spec: dict[str, object] = {field: None for field in WORKER_SPEC_FIELDS}
        spec.update(
            {
                "schema_version": "scienceboard-host-worker-spec-v3",
                "session_id": SESSION_ID,
                "task_id": "scienceboard-vm-kalgebra-a-01-v0.1",
                "task_path": str(session / "task.json"),
                "session_dir": str(session),
                "scienceboard_root": str(self.root / "ScienceBoard"),
                "actor": "human",
                "openai_chat_completions_url": OFFICIAL_OPENAI_CHAT_COMPLETIONS_URL,
                "protocol_version": "scienceboard-matched-traces-v2",
                "executor_version": "self-human-protocol-v3",
                "guest_id": "scienceboard-vm-sci-bench-v1",
                "guest_os_id": "ubuntu",
                "guest_os_version": "22.04",
                "screen_width": 1920,
                "screen_height": 1080,
                "scienceboard_commit": "c8d5010bdba32afc5dfb16472873bed148c39a45",
                "timeout_seconds": 300,
                "guest_data_root": DEFAULT_GUEST_DATA_ROOT,
                "guest_control_root": DEFAULT_GUEST_CONTROL_ROOT,
                "guest_bundle_root": str(self.bundle.path.parent),
                "guest_bundle_hashes": {GUEST_BUNDLE_NAME: self.bundle.sha256},
                "self_consent_confirmed": True,
            }
        )
        self.spec = spec

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _runtime(self, **overrides: object) -> tuple[OfficialRuntime, FakeGuestFiles, FakeTask]:
        runtime = OfficialRuntime({**self.spec, **overrides})
        files, task = FakeGuestFiles(), FakeTask()
        runtime.manager = files
        runtime.task = task
        return runtime, files, task

    def test_single_bundle_is_transported_verified_and_launched(self) -> None:
        runtime, files, task = self._runtime()
        evidence = runtime.start_recorder()
        guest_dir = f"{DEFAULT_GUEST_DATA_ROOT}/{SESSION_ID}/recorder"
        guest_bundle = f"{guest_dir}/{GUEST_BUNDLE_NAME}"
        self.assertEqual(set(files.files), {guest_bundle + ".b64"})
        self.assertEqual(
            base64.b64decode(files.files[guest_bundle + ".b64"]), self.bundle.path.read_bytes()
        )
        mkdir, install, start = task.commands
        self.assertEqual(mkdir[:2], ["/usr/bin/mkdir", "-p"])
        self.assertIn(guest_dir, mkdir)
        self.assertEqual(
            install,
            [
                GUEST_PYTHON,
                "-c",
                GUEST_BUNDLE_INSTALLER,
                guest_bundle + ".b64",
                guest_bundle,
                self.bundle.sha256,
            ],
        )
        self.assertEqual(start[:3], [GUEST_PYTHON, guest_bundle, "start-recorder"])
        self.assertIn("--self-consent-confirmed", start)
        deployment = evidence["deployment"]
        assert isinstance(deployment, dict)
        self.assertEqual(deployment["sha256"], self.bundle.sha256)
        self.assertEqual(deployment["bundle"], GUEST_BUNDLE_NAME)

        runtime.stop_recorder()
        stop = task.commands[-1]
        self.assertEqual(stop[:3], [GUEST_PYTHON, guest_bundle, "stop-recorder"])

    def test_guest_installer_decodes_verifies_and_installs_atomically(self) -> None:
        payload = self.bundle.path.read_bytes()
        transport = self.root / "bundle.pyz.b64"
        target = self.root / "installed" / GUEST_BUNDLE_NAME
        target.parent.mkdir()
        transport.write_text(base64.b64encode(payload).decode("ascii"), encoding="ascii")
        wrong = subprocess.run(
            [sys.executable, "-I", "-c", GUEST_BUNDLE_INSTALLER, str(transport), str(target), "0" * 64],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertNotEqual(wrong.returncode, 0)
        self.assertFalse(target.exists())
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                GUEST_BUNDLE_INSTALLER,
                str(transport),
                str(target),
                hashlib.sha256(payload).hexdigest(),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse(transport.exists())

    def test_bundle_hash_drift_and_multi_file_deployments_are_refused(self) -> None:
        runtime, files, task = self._runtime(guest_bundle_hashes={GUEST_BUNDLE_NAME: "0" * 64})
        with self.assertRaisesRegex(WorkerProtocolError, "hash mismatch"):
            runtime.start_recorder()
        self.assertEqual(files.files, {})
        self.assertEqual(task.commands, [])
        legacy = {
            "guest_controller.py": "a" * 64,
            "x11_recorder.py": "b" * 64,
        }
        runtime, _, _ = self._runtime(guest_bundle_hashes=legacy)
        with self.assertRaisesRegex(WorkerProtocolError, "exactly one"):
            runtime.start_recorder()

    def test_human_launch_requires_bound_self_consent(self) -> None:
        runtime, _, task = self._runtime(self_consent_confirmed=False)
        with self.assertRaisesRegex(WorkerProtocolError, "self-consent"):
            runtime.start_recorder()
        self.assertEqual(task.commands, [])


if __name__ == "__main__":
    unittest.main()
