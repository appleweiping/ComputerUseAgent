"""Preflight gates: calibration recomputation, private storage and driver timeouts."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from computer_use_agent.capture.x11.calibration import evaluate_calibration
from computer_use_agent.capture.x11.recorder import build_session_metadata
from computer_use_agent.trace.io import TraceWriter
from research.scienceboard.orchestrate import load_manifest
from research.scienceboard.preflight import (
    DEFAULT_MANIFEST,
    _driver_is_valid,
    _recompute_calibration,
    _timeout_margin_errors,
    run_preflight,
)
from research.scienceboard.private_storage import PRIVATE_ROOT_ENV
from research.scienceboard.worker_contract import PHASE_RESPONSE_MARGIN_SECONDS

COMMIT = "c8d5010bdba32afc5dfb16472873bed148c39a45"
BINDING = {
    "guest_id": "scienceboard-vm-sci-bench-v1",
    "guest_os_id": "ubuntu",
    "guest_os_version": "22.04",
    "screen_resolution": {"width": 1920, "height": 1080},
    "scienceboard_commit": COMMIT,
    "capture_adapter": "x11_vm",
    "recorder_version": "x11-v2",
}


def _calibration_metadata(actor: str, session_id: str) -> dict[str, Any]:
    human = actor == "human"
    return build_session_metadata(
        argparse.Namespace(
            actor=actor,
            session_id=session_id,
            participant_code="self-human-001",
            pair_id=None,
            task_id="scienceboard-x11-calibration-v1",
            protocol_version="scienceboard-x11-calibration-v1",
            executor_version="self-human-protocol-v2" if human else "python-xlib-0.33",
            consent_confirmed=human,
            controller_confirmed=not human,
            guest_id=BINDING["guest_id"],
            guest_os_id="ubuntu",
            guest_os_version="22.04",
            screen_width=1920,
            screen_height=1080,
            scienceboard_commit=COMMIT,
        )
    )


def _event(event_id: str, event_type: str, timestamp: float) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "cui-trace-v1",
        "event_id": event_id,
        "type": event_type,
        "epoch_ms": 1_000 + timestamp,
        "monotonic_ms": timestamp,
        "viewport": {"width": 1920, "height": 1080, "device_pixel_ratio": 1},
        "target": {"application_id": "KAlgebra", "role": "desktop_application"},
        "key_class": None,
        "keyboard": None,
        "pointer": None,
        "wheel": None,
    }
    if event_type in {"keydown", "keyup"}:
        payload["key_class"] = "printable"
        payload["keyboard"] = {"repeat": False}
    else:
        payload["pointer"] = {"x": timestamp, "y": timestamp, "button": 1, "pointer_type": "mouse"}
    return payload


def _write_calibration(directory: Path) -> tuple[Path, Path]:
    phases = {}
    for name in ("xtest", "human"):
        path = directory / f"{name}.jsonl"
        writer = TraceWriter(path, _calibration_metadata(name, f"calibration-{name}"))
        for index, event_type in enumerate(
            ["pointermove", "pointerdown", "pointerup", "keydown", "keyup"], 1
        ):
            writer.append(_event(f"{name}-{index}", event_type, float(index)))
        writer.close(status="complete", stopped_at="2026-08-08T00:00:00+00:00")
        phases[name] = (path.name, writer.event_count)
    evidence = {
        "schema_version": "scienceboard-x11-calibration-evidence-v1",
        "xtest": {
            "trace": phases["xtest"][0],
            "event_count_at_stop": phases["xtest"][1],
            "event_count_after_post_stop_probe": phases["xtest"][1],
        },
        "self_human": {
            "trace": phases["human"][0],
            "event_count_at_stop": phases["human"][1],
            "event_count_after_post_stop_probe": phases["human"][1],
        },
    }
    evidence_path = directory / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    certificate_path = directory / "certificate.json"
    certificate_path.write_text(json.dumps(evaluate_calibration(evidence_path)), encoding="utf-8")
    return evidence_path, certificate_path


def _check(report: dict[str, Any], name: str) -> dict[str, Any]:
    return dict(next(row for row in report["checks"] if row["name"] == name))


class CalibrationGateTests(unittest.TestCase):
    def test_preflight_recomputes_and_rejects_forged_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evidence_path, certificate_path = _write_calibration(Path(temp))
            passed, _ = _recompute_calibration(evidence_path, certificate_path, BINDING)
            self.assertTrue(passed)
            certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
            certificate["phases"][0]["trace_sha256"] = "0" * 64
            certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
            passed, detail = _recompute_calibration(evidence_path, certificate_path, BINDING)
            self.assertFalse(passed)
            self.assertIn("differs", detail)

    def test_calibration_counts_only_under_the_private_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            private = root / "private"
            private.mkdir()
            evidence_path, certificate_path = _write_calibration(private)
            environment = {
                "SCIENCEBOARD_CALIBRATION_EVIDENCE": str(evidence_path),
                "SCIENCEBOARD_CALIBRATION_CERT": str(certificate_path),
            }
            with (
                mock.patch.dict(os.environ, {**environment, PRIVATE_ROOT_ENV: str(private)}),
                mock.patch("research.scienceboard.preflight._discover_vmrun", return_value=None),
            ):
                report = run_preflight(DEFAULT_MANIFEST)
            self.assertTrue(_check(report, "x11_calibration")["passed"])
            self.assertTrue(_check(report, "private_root")["passed"])
            self.assertIn("GiB free", _check(report, "free_disk")["detail"])
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            with (
                mock.patch.dict(os.environ, {**environment, PRIVATE_ROOT_ENV: str(elsewhere)}),
                mock.patch("research.scienceboard.preflight._discover_vmrun", return_value=None),
            ):
                report = run_preflight(DEFAULT_MANIFEST)
            calibration = _check(report, "x11_calibration")
            self.assertFalse(calibration["passed"])
            self.assertIn(PRIVATE_ROOT_ENV, calibration["detail"])

    def test_vm_path_must_be_an_existing_vmx_on_any_volume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            vmx = Path(temp) / "ScienceBoard.vmx"
            vmx.write_text("vmx", encoding="utf-8")
            other = Path(temp) / "ScienceBoard.zip"
            other.write_text("zip", encoding="utf-8")
            for path, expected in ((vmx, True), (other, False)):
                with (
                    self.subTest(path=path.name),
                    mock.patch.dict(os.environ, {"VM_PATH": str(path)}),
                    mock.patch(
                        "research.scienceboard.preflight._discover_vmrun", return_value=None
                    ),
                ):
                    self.assertIs(_check(run_preflight(DEFAULT_MANIFEST), "vm_path")["passed"], expected)


class DriverTimeoutMarginTests(unittest.TestCase):
    def _resolved_driver(self) -> dict[str, Any]:
        source = DEFAULT_MANIFEST.parents[1] / "driver.example.json"
        payload: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
        for specification in payload["commands"].values():
            specification["argv"] = [
                sys.executable if value == "REPLACE_WITH_PINNED_HOST_PYTHON" else value
                for value in specification["argv"]
            ]
        return payload

    def test_example_driver_leaves_twice_the_response_margin(self) -> None:
        manifest = load_manifest(DEFAULT_MANIFEST)
        self.assertEqual(_timeout_margin_errors(self._resolved_driver()["commands"], manifest), [])

    def test_too_small_outer_timeout_is_rejected(self) -> None:
        manifest = load_manifest(DEFAULT_MANIFEST)
        payload = self._resolved_driver()
        payload["commands"]["run_aio"]["timeout_seconds"] = (
            manifest["agent_timeout_seconds"] + PHASE_RESPONSE_MARGIN_SECONDS
        )
        errors = _timeout_margin_errors(payload["commands"], manifest)
        self.assertEqual(len(errors), 1)
        self.assertIn("run_aio", errors[0])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "driver.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            valid, detail = _driver_is_valid(path, manifest)
        self.assertFalse(valid)
        self.assertIn("timeout margins", detail)

    def test_actor_placeholder_without_protocol_budget_fails_closed(self) -> None:
        errors = _timeout_margin_errors(self._resolved_driver()["commands"], None)
        self.assertTrue(errors)
        self.assertTrue(all("{timeout_seconds}" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
