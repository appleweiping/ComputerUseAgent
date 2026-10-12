"""Guest-side X11 event recorder for ScienceBoard native applications.

The Chrome extension cannot observe input inside the Linux VM. This adapter
uses the X RECORD extension for global X11 input and emits the same high-level
event families inside ``cui-trace-v2``. It never stores a key symbol/code, window
title, clipboard value, filename, command line, or document content.

It runs inside the guest from the single-file bundle built by
:mod:`computer_use_agent.capture.x11.bundle`::

    python3 computer-use-agent-x11-recorder.pyz record --doctor

The only third-party dependency is ``python-xlib`` (the ``x11`` extra). It must
be baked into the guest snapshot, because a snapshot revert discards anything
installed afterwards.

The recorder intentionally refuses a human session without an affirmative
``--consent-confirmed`` flag.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...trace.io import DEFAULT_MAX_TRACE_BYTES, TraceWriter, recover_stale_partials
from ...trace.privacy import classify_keysym, sanitize_application_id

SCHEMA_VERSION = "cui-trace-v2"
CAPTURE_CANDIDATE_SCHEMA_VERSION = "cui-trace-v1"
ADAPTER_VERSION = "x11-v2"
# Default guest binding; the ScienceBoard host always passes these explicitly.
DEFAULT_SCIENCEBOARD_COMMIT = "c8d5010bdba32afc5dfb16472873bed148c39a45"
DEFAULT_GUEST_ID = "scienceboard-vm-sci-bench-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()  # not datetime.UTC: guest Python 3.10


def build_session_metadata(args: argparse.Namespace) -> dict[str, object]:
    actor_type = "human" if args.actor == "human" else "agent"
    if args.actor == "human":
        executor, source_class = "manual", "self_human"
    elif args.actor == "xtest":
        executor, source_class = "xtest", "scripted_automation_control"
    else:
        executor, source_class = "scienceboard-agent", "reasoning_agent"
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": args.session_id or str(uuid.uuid4()),
        "actor_type": actor_type,
        "actor_label": args.actor,
        "participant_code": args.participant_code if actor_type == "human" else None,
        "pair_id": args.pair_id,
        "task_id": args.task_id,
        "protocol_version": args.protocol_version,
        "executor": executor,
        "executor_version": args.executor_version,
        "recorder_version": ADAPTER_VERSION,
        "capture_adapter": "x11_vm",
        "environment_scope": "scienceboard_vm_x11",
        "source_class": source_class,
        "environment": f"{platform.system()}_{platform.release()}_X11",
        "guest_id": args.guest_id,
        "guest_os_id": args.guest_os_id,
        "guest_os_version": args.guest_os_version,
        "screen_resolution": {"width": args.screen_width, "height": args.screen_height},
        "scienceboard_commit": args.scienceboard_commit,
        "raw_key_capture": False,
        "include_query": False,
        "consent_confirmed": bool(args.consent_confirmed),
        "controller_confirmed": bool(args.controller_confirmed),
        "started_at": utc_now(),
        "status": "recording",
        "privacy_policy": "x11-metadata-only-v1",
        "monotonic_origin_ms": 0.0,
        "timing_model": "session-global-monotonic-v1",
        "document_epoch_count": 1,
    }


def x11_doctor() -> dict[str, object]:
    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    display_name = os.environ.get("DISPLAY")
    check(
        "display_environment",
        bool(display_name),
        "DISPLAY is set" if display_name else "DISPLAY is not set",
    )
    try:
        from Xlib import display as xdisplay
    except Exception as error:
        check("python_xlib", False, f"python-xlib unavailable: {type(error).__name__}")
        return {"ready": False, "checks": checks}

    check("python_xlib", True, "python-xlib imported")
    try:
        connection = xdisplay.Display(display_name)
    except Exception as error:
        check("x11_connection", False, f"X11 connection failed: {type(error).__name__}")
        return {"ready": False, "checks": checks}

    try:
        check("x11_connection", True, "X11 display opened")
        extensions = {name.upper() for name in connection.list_extensions()}
        check(
            "record_extension",
            "RECORD" in extensions,
            "RECORD extension available" if "RECORD" in extensions else "RECORD extension missing",
        )
        check(
            "xtest_extension",
            "XTEST" in extensions,
            "XTEST extension available" if "XTEST" in extensions else "XTEST extension missing",
        )
    finally:
        connection.close()
    return {"ready": all(bool(item["passed"]) for item in checks), "checks": checks}


class X11Recorder:
    """Translate core X11 input events into privacy-safe trace records."""

    def __init__(self, writer: TraceWriter, *, pointer_sample_ms: float = 16.0) -> None:
        from Xlib import XK, X, display
        from Xlib.ext import record
        from Xlib.protocol import rq

        self.X = X
        self.XK = XK
        self.record = record
        self.rq = rq
        self.control_display = display.Display()
        self.record_display = display.Display()
        self.writer = writer
        self.pointer_sample_ms = max(0.0, float(pointer_sample_ms))
        self.started_epoch_ms = writer.started_epoch_ms
        self.started_monotonic_ns = writer.started_monotonic_ns
        self.last_pointer_ms: float | None = None
        self.pressed_keys: dict[int, tuple[float, str]] = {}
        self.context: object | None = None
        self._active_application_id = "unknown"
        self._last_application_lookup_ms = -1_000.0

    def _times(self) -> tuple[float, float]:
        monotonic_ms = (time.perf_counter_ns() - self.started_monotonic_ns) / 1_000_000
        return self.started_epoch_ms + monotonic_ms, monotonic_ms

    def _screen_size(self) -> tuple[int, int]:
        screen = self.control_display.screen()
        return int(screen.width_in_pixels), int(screen.height_in_pixels)

    def _application_id(self, monotonic_ms: float) -> str:
        # WM_CLASS lookup is intentionally throttled. Window titles are never
        # requested, so application identity cannot accidentally include task
        # text, a filename, or a document value.
        if monotonic_ms - self._last_application_lookup_ms < 250:
            return self._active_application_id
        self._last_application_lookup_ms = monotonic_ms
        try:
            root = self.control_display.screen().root
            atom = self.control_display.intern_atom("_NET_ACTIVE_WINDOW")
            prop = root.get_full_property(atom, self.X.AnyPropertyType)
            if prop is None or not len(prop.value):
                return self._active_application_id
            window = self.control_display.create_resource_object("window", int(prop.value[0]))
            wm_class = window.get_wm_class() or ()
            self._active_application_id = sanitize_application_id(
                wm_class[-1] if wm_class else "unknown"
            )
        except Exception:
            self._active_application_id = "unknown"
        return self._active_application_id

    def _base_event(self, event_type: str) -> dict[str, Any]:
        epoch_ms, monotonic_ms = self._times()
        width, height = self._screen_size()
        return {
            # TraceWriter assigns the authoritative V2 event envelope. Keeping
            # capture candidates in the V1 shape prevents a half-populated V2
            # record from crossing the validation boundary.
            "schema_version": CAPTURE_CANDIDATE_SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "type": event_type,
            "epoch_ms": round(epoch_ms, 3),
            "monotonic_ms": round(monotonic_ms, 3),
            "viewport": {"width": width, "height": height, "device_pixel_ratio": 1},
            "target": {
                "role": "desktop_application",
                "application_id": self._application_id(monotonic_ms),
            },
            "key_class": None,
            "keyboard": None,
            "pointer": None,
            "wheel": None,
        }

    def _key_event(self, event: Any, *, down: bool) -> None:
        keycode = int(event.detail)
        keysym = self.control_display.keycode_to_keysym(keycode, 0)
        # The name is used only for reduction to a coarse class. Neither name,
        # numeric keysym, nor keycode is placed in the emitted record.
        key_class = classify_keysym(self.XK.keysym_to_string(keysym) if keysym else None)
        payload = self._base_event("keydown" if down else "keyup")
        payload["key_class"] = key_class
        now_ms = float(payload["monotonic_ms"])
        keyboard: dict[str, object] = {"repeat": down and keycode in self.pressed_keys}
        if down:
            self.pressed_keys.setdefault(keycode, (now_ms, key_class))
        else:
            pressed = self.pressed_keys.pop(keycode, None)
            if pressed is not None:
                keyboard["hold_ms"] = round(max(0.0, now_ms - pressed[0]), 3)
        payload["keyboard"] = keyboard
        self.writer.append(payload)

    def _button_event(self, event: Any, *, down: bool) -> None:
        button = int(event.detail)
        if button in {4, 5, 6, 7}:
            if not down:
                return
            payload = self._base_event("wheel")
            payload["wheel"] = {
                "delta_x": -1 if button == 6 else 1 if button == 7 else 0,
                "delta_y": -1 if button == 4 else 1 if button == 5 else 0,
                "delta_mode": "step",
            }
        else:
            payload = self._base_event("pointerdown" if down else "pointerup")
            payload["pointer"] = {
                "x": float(getattr(event, "root_x", 0)),
                "y": float(getattr(event, "root_y", 0)),
                "button": button,
                "pointer_type": "mouse",
            }
        self.writer.append(payload)

    def _motion_event(self, event: Any) -> None:
        payload = self._base_event("pointermove")
        now_ms = float(payload["monotonic_ms"])
        if (
            self.last_pointer_ms is not None
            and now_ms - self.last_pointer_ms < self.pointer_sample_ms
        ):
            return
        self.last_pointer_ms = now_ms
        payload["pointer"] = {
            "x": float(getattr(event, "root_x", 0)),
            "y": float(getattr(event, "root_y", 0)),
            "button": -1,
            "pointer_type": "mouse",
        }
        self.writer.append(payload)

    def _focus_event(self, *, incoming: bool) -> None:
        self.writer.append(self._base_event("focusin" if incoming else "focusout"))

    def _callback(self, reply: Any) -> None:
        if reply.category != self.record.FromServer or reply.client_swapped or not reply.data:
            return
        data = reply.data
        while data:
            event, data = self.rq.EventField(None).parse_binary_value(
                data,
                self.record_display.display,
                None,
                None,
            )
            event_type = int(event.type)
            if event_type == self.X.KeyPress:
                self._key_event(event, down=True)
            elif event_type == self.X.KeyRelease:
                self._key_event(event, down=False)
            elif event_type == self.X.ButtonPress:
                self._button_event(event, down=True)
            elif event_type == self.X.ButtonRelease:
                self._button_event(event, down=False)
            elif event_type == self.X.MotionNotify:
                self._motion_event(event)
            elif event_type == self.X.FocusIn:
                self._focus_event(incoming=True)
            elif event_type == self.X.FocusOut:
                self._focus_event(incoming=False)

    def capture(self, duration_seconds: float, *, control_file: Path | None = None) -> None:
        if not self.record_display.has_extension("RECORD"):
            raise RuntimeError("X11 RECORD extension is unavailable")
        self.context = self.record_display.record_create_context(
            0,
            [self.record.AllClients],
            [
                {
                    "core_requests": (0, 0),
                    "core_replies": (0, 0),
                    "ext_requests": (0, 0, 0, 0),
                    "ext_replies": (0, 0, 0, 0),
                    "delivered_events": (0, 0),
                    "device_events": (self.X.KeyPress, self.X.FocusOut),
                    "errors": (0, 0),
                    "client_started": False,
                    "client_died": False,
                }
            ],
        )

        stop_lock = threading.Lock()

        def stop_capture() -> None:
            with stop_lock:
                if self.context is not None:
                    self.control_display.record_disable_context(self.context)
                    self.control_display.flush()

        def stop_after_limit() -> None:
            deadline = time.monotonic() + duration_seconds
            while time.monotonic() < deadline:
                if control_file is not None and control_file.exists():
                    stop_capture()
                    return
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            if self.context is not None:
                stop_capture()

        timer = threading.Thread(target=stop_after_limit, daemon=True)
        timer.start()
        try:
            self.record_display.record_enable_context(self.context, self._callback)
        finally:
            if self.context is not None:
                self.record_display.record_free_context(self.context)
            self.record_display.close()
            self.control_display.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="record",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="check X11 prerequisites without recording",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--actor", choices=("human", "aio", "seeact", "xtest"), default="human")
    parser.add_argument("--task-id", default="scienceboard-calibration")
    parser.add_argument("--session-id")
    parser.add_argument("--pair-id")
    parser.add_argument("--participant-code", default="self-human-001")
    parser.add_argument("--protocol-version", default="scienceboard-matched-traces-v2")
    parser.add_argument("--executor-version", help="required when recording; not used by --doctor")
    parser.add_argument("--guest-id", default=DEFAULT_GUEST_ID)
    parser.add_argument("--guest-os-id", default="ubuntu")
    parser.add_argument("--guest-os-version", default="22.04")
    parser.add_argument("--screen-width", type=int, default=1920)
    parser.add_argument("--screen-height", type=int, default=1080)
    parser.add_argument("--scienceboard-commit", default=DEFAULT_SCIENCEBOARD_COMMIT)
    parser.add_argument("--control-file", type=Path)
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--pointer-sample-ms", type=float, default=16.0)
    parser.add_argument(
        "--max-trace-bytes",
        type=int,
        default=DEFAULT_MAX_TRACE_BYTES,
        help="hard local event quota; terminal metadata is always retained",
    )
    parser.add_argument("--consent-confirmed", action="store_true")
    parser.add_argument("--controller-confirmed", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.doctor:
        report = x11_doctor()
        print(json.dumps(report, indent=2))
        return 0 if report["ready"] else 2
    if args.output is None:
        print("--output is required when recording", file=sys.stderr)
        return 2
    if not args.executor_version:
        print("--executor-version is required when recording", file=sys.stderr)
        return 2
    if args.control_file is not None and args.control_file.exists():
        print(
            "--control-file already exists; refusing an immediately stopped/stale session",
            file=sys.stderr,
        )
        return 2
    if not 0 < args.duration_seconds <= 900:
        print("--duration-seconds must be in (0, 900]", file=sys.stderr)
        return 2
    if args.max_trace_bytes < 1:
        print("--max-trace-bytes must be a positive integer", file=sys.stderr)
        return 2
    if args.actor == "human" and not args.consent_confirmed:
        print("Human recording requires --consent-confirmed", file=sys.stderr)
        return 2
    if args.actor != "human" and not args.controller_confirmed:
        print("Agent recording requires --controller-confirmed", file=sys.stderr)
        return 2

    recovery = recover_stale_partials(args.output)
    if any(item.get("status") == "rejected" for item in recovery):
        print(
            "A stale X11 partial could not be recovered; refusing to start a new trace",
            file=sys.stderr,
        )
        print(json.dumps({"recovery": recovery}, indent=2), file=sys.stderr)
        return 2

    metadata = build_session_metadata(args)
    with TraceWriter(
        args.output,
        metadata,
        max_trace_bytes=args.max_trace_bytes,
    ) as writer:
        recorder = X11Recorder(writer, pointer_sample_ms=args.pointer_sample_ms)
        recorder.capture(args.duration_seconds, control_file=args.control_file)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "event_count": writer.event_count,
                "recovered_prior_sessions": recovery,
                "max_trace_bytes": args.max_trace_bytes,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
