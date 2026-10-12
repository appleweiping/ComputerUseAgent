"""Shared X11 trace fixtures for the trace-core and capture tests (not a test module)."""

from __future__ import annotations

import argparse
from typing import Any

from computer_use_agent.capture.x11.recorder import build_session_metadata

COMMIT = "c8d5010bdba32afc5dfb16472873bed148c39a45"
CALIBRATION_TASK_ID = "scienceboard-x11-calibration-v1"
CALIBRATION_PROTOCOL = "scienceboard-x11-calibration-v1"


def metadata(actor: str, session_id: str, *, calibration: bool = False) -> dict[str, Any]:
    """Return X11 session metadata exactly as the guest recorder builds it."""

    human = actor == "human"
    return build_session_metadata(
        argparse.Namespace(
            actor=actor,
            session_id=session_id,
            participant_code="self-human-001",
            pair_id=None,
            task_id=CALIBRATION_TASK_ID if calibration else "task-1",
            protocol_version=CALIBRATION_PROTOCOL
            if calibration
            else "scienceboard-matched-traces-v2",
            executor_version="self-human-protocol-v2" if human else "python-xlib-0.33",
            consent_confirmed=human,
            controller_confirmed=not human,
            guest_id="scienceboard-vm-sci-bench-v1",
            guest_os_id="ubuntu",
            guest_os_version="22.04",
            screen_width=1920,
            screen_height=1080,
            scienceboard_commit=COMMIT,
        )
    )


def event(event_id: str, event_type: str, timestamp: float) -> dict[str, Any]:
    """Return one privacy-safe capture candidate in the recorder's v1 shape."""

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
    elif event_type.startswith("pointer"):
        payload["pointer"] = {
            "x": timestamp,
            "y": timestamp + 1,
            "button": 1,
            "pointer_type": "mouse",
        }
    return payload
