"""Guest controller and recorder entry points that run without an X server."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from computer_use_agent.capture.x11 import guest_controller, recorder


def _start_args(root: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "control_dir": root / "control",
        "data_root": root / "data",
        "output": root / "data" / "trace.jsonl",
        "actor": "human",
        "session_id": "sb-kalgebra-a01-human-r01",
        "task_id": "task-1",
        "protocol_version": "scienceboard-matched-traces-v2",
        "executor_version": "self-human-protocol-v3",
        "duration_seconds": 5.0,
        "participant_code": "self-human-001",
        "self_consent_confirmed": False,
        "guest_id": recorder.DEFAULT_GUEST_ID,
        "guest_os_id": "ubuntu",
        "guest_os_version": "22.04",
        "screen_width": 1920,
        "screen_height": 1080,
        "scienceboard_commit": recorder.DEFAULT_SCIENCEBOARD_COMMIT,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class GuestControllerTests(unittest.TestCase):
    def test_source_checkout_launches_the_package_recorder(self) -> None:
        self.assertEqual(
            guest_controller.recorder_command(),
            [sys.executable, "-m", "computer_use_agent.capture.x11", "record"],
        )

    def test_pins_are_shared_with_the_recorder(self) -> None:
        self.assertEqual(guest_controller.PINNED_COMMIT, recorder.DEFAULT_SCIENCEBOARD_COMMIT)
        doctor = guest_controller.parse_args(
            ["doctor", "--control-dir", "/home/user/c", "--data-root", "/home/user/d"]
        )
        self.assertEqual(doctor.guest_id, recorder.DEFAULT_GUEST_ID)
        record = recorder.parse_args(["--doctor"])
        self.assertTrue(record.doctor)
        self.assertEqual(record.scienceboard_commit, recorder.DEFAULT_SCIENCEBOARD_COMMIT)

    def test_human_recording_requires_explicit_self_consent(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(ValueError, "self-consent"),
        ):
            guest_controller.start_recorder(_start_args(Path(temp)))

    def test_guest_paths_must_stay_under_home_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            args = _start_args(Path(temp), actor="aio")
            with self.assertRaisesRegex(ValueError, "/home/user"):
                guest_controller.start_recorder(args)

    def test_unsafe_session_ids_are_rejected(self) -> None:
        for session_id in ("", "../escape", "a/b", "x" * 161):
            with self.subTest(session_id=session_id), self.assertRaises(ValueError):
                guest_controller._state_path(Path("/home/user/control"), session_id)

    def test_capability_report_binds_the_recorder_version(self) -> None:
        args = guest_controller.parse_args(
            ["doctor", "--control-dir", "/tmp/not-home", "--data-root", "/tmp/not-home"]
        )
        # Never touch a real X server from the unit suite.
        with (
            mock.patch.object(guest_controller, "_screen_size", return_value=None),
            mock.patch.object(
                guest_controller,
                "x11_doctor",
                return_value={"ready": False, "checks": []},
            ),
        ):
            report = guest_controller.capability_doctor(args)
        self.assertFalse(report["ready"])
        self.assertEqual(report["binding"]["recorder_version"], recorder.ADAPTER_VERSION)
        paths = next(row for row in report["checks"] if row["name"] == "private_guest_paths")
        self.assertFalse(paths["passed"])

    def test_controller_errors_are_reported_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch("builtins.print") as printed:
            code = guest_controller.main(
                [
                    "stop-recorder",
                    "--control-dir",
                    str(Path(temp)),
                    "--session-id",
                    "sb-kalgebra-a01-aio-r01",
                ]
            )
        self.assertEqual(code, 2)
        payload = json.loads(printed.call_args.args[0])
        self.assertEqual(payload["error"], "ValueError")


class RecorderCliTests(unittest.TestCase):
    def test_recording_requires_output_and_executor_version(self) -> None:
        with mock.patch("sys.stderr"):
            self.assertEqual(recorder.main(["--actor", "xtest"]), 2)
            with tempfile.TemporaryDirectory() as temp:
                output = str(Path(temp) / "trace.jsonl")
                self.assertEqual(recorder.main(["--output", output, "--actor", "xtest"]), 2)

    def test_human_recording_requires_consent_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch("sys.stderr"):
            output = str(Path(temp) / "trace.jsonl")
            self.assertEqual(
                recorder.main(["--output", output, "--executor-version", "manual-v1"]), 2
            )
            self.assertFalse(Path(output).exists())


if __name__ == "__main__":
    unittest.main()
