"""Dependency-free runtime validation for ScienceBoard X11 trace records.

The JSON Schema is the machine-readable contract.  These checks deliberately
duplicate its security-critical constraints so capture does not become
fail-open when ``jsonschema`` is not installed in the guest VM.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit

from .privacy import (
    ALLOWED_EVENT_TYPES,
    ALLOWED_KEY_CLASSES,
    assert_privacy_safe_event,
)

HEX40 = re.compile(r"^[0-9a-f]{40}$")
UUIDISH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
APP_ID = re.compile(r"^(KAlgebra|Celestia|GrassGIS|GnomeTerminal|unknown|other_[0-9a-f]{12})$")

V1_SESSION_FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "actor_type",
        "actor_label",
        "participant_code",
        "pair_id",
        "task_id",
        "protocol_version",
        "executor",
        "executor_version",
        "recorder_version",
        "capture_adapter",
        "environment_scope",
        "source_class",
        "environment",
        "guest_id",
        "guest_os_id",
        "guest_os_version",
        "screen_resolution",
        "scienceboard_commit",
        "raw_key_capture",
        "include_query",
        "consent_confirmed",
        "controller_confirmed",
        "started_at",
        "status",
        "privacy_policy",
        "monotonic_origin_ms",
    }
)
V2_SESSION_FIELDS = V1_SESSION_FIELDS | frozenset({"timing_model", "document_epoch_count"})
V1_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "type",
        "epoch_ms",
        "monotonic_ms",
        "viewport",
        "target",
        "key_class",
        "keyboard",
        "pointer",
        "wheel",
    }
)
V2_EVENT_FIELDS = V1_EVENT_FIELDS | frozenset(
    {"event_seq", "document_epoch", "t_session_ms", "t_document_ms", "step_id"}
)
V1_TERMINAL_FIELDS = frozenset(
    {"status", "stopped_at", "event_count", "capture_error", "duration_ms"}
)
V2_TERMINAL_FIELDS = V1_TERMINAL_FIELDS | frozenset(
    {
        "event_seq_last",
        "document_epoch_count",
        "dropped_event_count",
        "rejected_event_count",
        "flush_failure_count",
        "quota_rejection_count",
    }
)

# Browser exports use a wider event vocabulary than the X11 adapter, but the
# shared reader must still fail closed. These sets mirror the checked-in
# recorder schemas without requiring jsonschema inside the ScienceBoard guest.
BROWSER_V1_REQUIRED_SESSION_FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "actor_type",
        "actor_label",
        "participant_code",
        "pair_id",
        "task_id",
        "protocol_version",
        "executor",
        "executor_version",
        "recorder_version",
        "environment",
        "raw_key_capture",
        "include_query",
        "scope_origin",
        "consent_confirmed",
        "disclosure_version",
        "retention_policy",
        "started_at",
        "stopped_at",
        "status",
        "event_count",
        "chunk_count",
    }
)
BROWSER_V1_OPTIONAL_SESSION_FIELDS = frozenset(
    {
        "capture_adapter",
        "environment_scope",
        "source_class",
        "notes",
        "capture_error",
    }
)
BROWSER_V1_SESSION_FIELDS = BROWSER_V1_REQUIRED_SESSION_FIELDS | BROWSER_V1_OPTIONAL_SESSION_FIELDS
BROWSER_LEGACY_SESSION_FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "actor_type",
        "actor_label",
        "participant_code",
        "task_id",
        "executor",
        "executor_version",
        "environment",
        "raw_key_capture",
        "include_query",
        "consent_confirmed",
        "started_at",
        "stopped_at",
        "status",
        "event_count",
        "evidence_scope",
    }
)
BROWSER_V2_SESSION_FIELDS = BROWSER_V1_SESSION_FIELDS | frozenset(
    {
        "timing_model",
        "event_seq_last",
        "document_epoch_count",
        "dropped_event_count",
        "rejected_event_count",
        "flush_failure_count",
        "quota_rejection_count",
    }
)
BROWSER_V1_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "type",
        "epoch_ms",
        "monotonic_ms",
        "url",
        "viewport",
        "pointer",
        "wheel",
        "scroll",
        "key",
        "key_class",
        "keyboard",
        "modifiers",
        "input",
        "clipboard",
        "transfer",
        "touch",
        "form",
        "document_state",
        "target",
        "is_trusted",
        "visibility_state",
    }
)
BROWSER_V2_EVENT_FIELDS = BROWSER_V1_EVENT_FIELDS | frozenset(
    {"event_seq", "document_epoch", "t_session_ms", "t_document_ms", "step_id"}
)
BROWSER_LEGACY_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "type",
        "epoch_ms",
        "monotonic_ms",
        "url",
        "viewport",
        "pointer",
        "wheel",
        "scroll",
        "key",
        "key_class",
        "modifiers",
        "input",
        "target",
        "is_trusted",
        "visibility_state",
    }
)
BROWSER_SUMMARY_FIELDS = frozenset({"event_count", "duration_ms", "pointer_distance_px", "by_type"})
BROWSER_NESTED_FIELDS = {
    "viewport": frozenset({"width", "height", "device_pixel_ratio"}),
    "pointer": frozenset(
        {
            "x",
            "y",
            "button",
            "buttons",
            "pointer_type",
            "pointer_id",
            "width",
            "height",
            "pressure",
            "tangential_pressure",
            "tilt_x",
            "tilt_y",
            "twist",
            "is_primary",
        }
    ),
    "wheel": frozenset({"delta_x", "delta_y", "delta_mode"}),
    "scroll": frozenset({"x", "y"}),
    "keyboard": frozenset({"repeat", "location", "is_composing"}),
    "modifiers": frozenset({"alt", "ctrl", "meta", "shift"}),
    "input": frozenset({"input_type", "data_length", "is_composing"}),
    "clipboard": frozenset({"types", "item_count", "file_count"}),
    "transfer": frozenset({"types", "item_count", "file_count", "effect_allowed", "drop_effect"}),
    "touch": frozenset({"active_count", "changed_count"}),
    "form": frozenset({"method", "action", "control_count", "has_submitter", "submitter_tag"}),
    "document_state": frozenset({"fullscreen", "online"}),
    "target": frozenset({"tag", "role", "input_type", "selector_hint", "password_like"}),
}
#: cui-trace-v2 nested objects: identical to V1 except that keyboard summaries
#: also carry ``hold_ms``. Drift-tested against the packaged v2 JSON Schema.
BROWSER_V2_NESTED_FIELDS = {
    **BROWSER_NESTED_FIELDS,
    "keyboard": BROWSER_NESTED_FIELDS["keyboard"] | frozenset({"hold_ms"}),
}
BROWSER_LEGACY_NESTED_FIELDS = {
    "viewport": frozenset({"width", "height", "device_pixel_ratio"}),
    "pointer": frozenset({"x", "y", "button", "buttons", "pointer_type"}),
    "wheel": frozenset({"delta_x", "delta_y", "delta_mode"}),
    "scroll": frozenset({"x", "y"}),
    "modifiers": frozenset({"alt", "ctrl", "meta", "shift"}),
    "input": frozenset({"input_type", "data_length"}),
    "target": frozenset({"tag", "role", "input_type", "selector_hint", "password_like"}),
}
BROWSER_SOURCE_CLASSES = frozenset(
    {
        "human_self",
        "self_human",
        "cui_agent",
        "scripted_automation_control",
        "synthetic_coverage",
    }
)
SAFE_BROWSER_KEYS = frozenset(
    {
        "<redacted>",
        "Alt",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "Backspace",
        "CapsLock",
        "Control",
        "Delete",
        "End",
        "Enter",
        "Escape",
        "Home",
        "Insert",
        "Meta",
        "PageDown",
        "PageUp",
        "Shift",
        "Tab",
    }
)
PRIVATE_BROWSER_FIELD_NAMES = frozenset(
    {
        "clipboard_contents",
        "command",
        "file_path",
        "filename",
        "keycode",
        "keysym",
        "process_command",
        "text",
        "title",
        "value",
        "window_title",
    }
)


def normalize_source_class(value: object) -> object:
    """Return the shared analysis vocabulary without mutating stored evidence.

    The browser recorder historically used ``human_self`` while ScienceBoard
    used ``self_human``. The shared boundary canonicalizes both spellings to
    ``self_human``; raw files retain their original, schema-valid vocabulary.
    """

    return "self_human" if value in {"human_self", "self_human"} else value


def _require_fields(
    value: Mapping[str, object],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    label: str,
) -> None:
    keys = set(value)
    missing = required - keys
    unexpected = keys - allowed
    if missing or unexpected:
        raise ValueError(
            f"{label} fields differ from contract; missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )


def _reject_private_browser_fields(value: object, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).lower()
            if key in PRIVATE_BROWSER_FIELD_NAMES:
                raise ValueError(f"Privacy-forbidden browser field: {'.'.join((*path, key))}")
            _reject_private_browser_fields(nested, (*path, key))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_private_browser_fields(nested, (*path, str(index)))


def _validate_browser_event(
    event: Mapping[str, object], version: str, *, legacy: bool = False
) -> None:
    expected = (
        BROWSER_LEGACY_EVENT_FIELDS
        if legacy
        else BROWSER_V2_EVENT_FIELDS
        if version == "cui-trace-v2"
        else BROWSER_V1_EVENT_FIELDS
    )
    _reject_private_browser_fields(event)
    _exact_fields(event, expected, "browser event")
    if event["schema_version"] != version:
        raise ValueError("Browser event schema_version does not match metadata")
    if not isinstance(event["event_id"], str) or not event["event_id"]:
        raise ValueError("Browser event_id must be a nonempty string")
    if not isinstance(event["type"], str) or not event["type"]:
        raise ValueError("Browser event type must be a nonempty string")
    _finite_number(event["epoch_ms"], "epoch_ms")
    _finite_number(event["monotonic_ms"], "monotonic_ms")
    if version == "cui-trace-v2":
        if type(event["event_seq"]) is not int or event["event_seq"] < 1:
            raise ValueError("event_seq must be a positive integer")
        if type(event["document_epoch"]) is not int or event["document_epoch"] < 0:
            raise ValueError("document_epoch must be a nonnegative integer")
        _finite_number(event["t_session_ms"], "t_session_ms")
        _finite_number(event["t_document_ms"], "t_document_ms")
        step_id = event["step_id"]
        if step_id is not None and (
            not isinstance(step_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", step_id)
        ):
            raise ValueError("step_id must be null or a bounded token")

    nested_contract = (
        BROWSER_LEGACY_NESTED_FIELDS
        if legacy
        else BROWSER_V2_NESTED_FIELDS
        if version == "cui-trace-v2"
        else BROWSER_NESTED_FIELDS
    )
    for name, expected_fields in nested_contract.items():
        nested = event[name]
        if nested is None and name not in {"viewport", "modifiers", "target"}:
            continue
        if not isinstance(nested, Mapping):
            raise ValueError(f"browser event {name} must be an object or allowed null")
        _exact_fields(nested, expected_fields, f"browser event {name}")
        if version == "cui-trace-v2" and name == "keyboard":
            hold_ms = nested["hold_ms"]
            if hold_ms is not None:
                _finite_number(hold_ms, "browser keyboard.hold_ms")
                if float(hold_ms) < 0 or float(hold_ms) > 600_000:
                    raise ValueError("browser keyboard.hold_ms is outside the bounded range")

    key = event["key"]
    if key is not None and (
        not isinstance(key, str)
        or (key not in SAFE_BROWSER_KEYS and not re.fullmatch(r"F[0-9]{1,2}", key))
    ):
        raise ValueError("Privacy-forbidden raw browser key value")
    if event["key_class"] not in {
        None,
        "control",
        "whitespace",
        "letter",
        "digit",
        "punctuation",
        "other",
    }:
        raise ValueError("Unsupported browser key_class")
    if event["type"] not in {"keydown", "keyup"} and any(
        event.get(name) is not None for name in ("key", "key_class", "keyboard")
    ):
        raise ValueError("Non-key browser event carries keyboard data")
    parsed_url = urlsplit(str(event["url"]))
    if parsed_url.scheme in {"http", "https"} and parsed_url.query:
        raise ValueError("Browser event URL contains a forbidden query string")


def validate_browser_trace(
    metadata: Mapping[str, object],
    summary: Mapping[str, object],
    events: Sequence[Mapping[str, object]],
) -> None:
    """Validate one browser export without a registry or network dependency."""

    version = metadata.get("schema_version")
    if version not in {"cui-trace-v1", "cui-trace-v2"}:
        raise ValueError("Unsupported browser trace schema_version")
    legacy = version == "cui-trace-v1" and "evidence_scope" in metadata
    if version == "cui-trace-v2":
        _exact_fields(metadata, BROWSER_V2_SESSION_FIELDS, "browser metadata")
    elif legacy:
        _exact_fields(metadata, BROWSER_LEGACY_SESSION_FIELDS, "browser metadata")
    else:
        _require_fields(
            metadata,
            allowed=BROWSER_V1_SESSION_FIELDS,
            required=BROWSER_V1_REQUIRED_SESSION_FIELDS,
            label="browser metadata",
        )
    _reject_private_browser_fields(metadata)
    capture_adapter = metadata.get(
        "capture_adapter", "browser_standalone_mirror" if legacy else "browser_mv3"
    )
    if capture_adapter not in {"browser_mv3", "browser_standalone_mirror"}:
        raise ValueError("Unsupported browser trace capture_adapter")
    if metadata["actor_type"] not in {"human", "agent"}:
        raise ValueError("Browser actor_type must be human or agent")
    source_class = metadata.get("source_class")
    if source_class is not None and source_class not in BROWSER_SOURCE_CLASSES:
        raise ValueError("Unsupported browser source_class")
    if metadata["actor_type"] == "human" and source_class not in {
        None,
        "human_self",
        "self_human",
    }:
        raise ValueError("Human browser trace has an agent source_class")
    if metadata["actor_type"] == "agent" and source_class in {
        "human_self",
        "self_human",
    }:
        raise ValueError("Agent browser trace has a human source_class")
    if metadata["raw_key_capture"] is not False or metadata["include_query"] is not False:
        raise ValueError("Shared research traces must disable raw keys and URL queries")
    if type(metadata["event_count"]) is not int or metadata["event_count"] < 0:
        raise ValueError("metadata.event_count must be a nonnegative integer")
    if metadata["event_count"] != len(events):
        raise ValueError("Browser metadata event_count does not match parsed events")
    if any(not isinstance(event, Mapping) for event in events):
        raise ValueError("Browser events must be objects")
    if version == "cui-trace-v2":
        if metadata["timing_model"] != "session-global-monotonic-v1":
            raise ValueError("Unexpected browser V2 timing_model")
        for name in (
            "event_seq_last",
            "document_epoch_count",
            "dropped_event_count",
            "rejected_event_count",
            "flush_failure_count",
            "quota_rejection_count",
        ):
            value = metadata[name]
            if type(value) is not int or value < 0:
                raise ValueError(f"metadata.{name} must be a nonnegative integer")
        if metadata["event_seq_last"] != len(events):
            raise ValueError("Browser metadata event_seq_last does not match parsed events")

    _exact_fields(summary, BROWSER_SUMMARY_FIELDS, "browser summary")
    if type(summary["event_count"]) is not int or summary["event_count"] != len(events):
        raise ValueError("Browser summary event_count does not match parsed events")
    for name in ("duration_ms", "pointer_distance_px"):
        _finite_number(summary[name], f"summary.{name}")
    by_type = summary["by_type"]
    if not isinstance(by_type, Mapping) or any(
        not isinstance(name, str) or type(count) is not int or count < 0
        for name, count in by_type.items()
    ):
        raise ValueError("Browser summary.by_type is invalid")
    observed = Counter(str(event.get("type")) for event in events)
    if dict(by_type) != dict(observed):
        raise ValueError("Browser summary.by_type does not match parsed events")
    for event in events:
        _validate_browser_event(event, str(version), legacy=legacy)


def _exact_fields(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    keys = set(value)
    if keys != expected:
        raise ValueError(
            f"{label} fields differ from contract; missing={sorted(expected - keys)}, "
            f"unexpected={sorted(keys - expected)}"
        )


def _finite_number(value: object, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a number") from error
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{label} must be finite and >= {minimum}")
    return number


def _iso8601(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 string")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 string") from error


def _nullable_identifier(value: object, label: str) -> None:
    if value is not None and (not isinstance(value, str) or not UUIDISH.fullmatch(value)):
        raise ValueError(f"{label} must be null or a bounded identifier")


def validate_session_metadata(metadata: Mapping[str, object]) -> None:
    """Validate structural, provenance, privacy, and actor/source invariants."""

    version = metadata.get("schema_version")
    if version not in {"cui-trace-v1", "cui-trace-v2"}:
        raise ValueError("Unsupported trace schema_version")
    _exact_fields(
        metadata,
        V2_SESSION_FIELDS if version == "cui-trace-v2" else V1_SESSION_FIELDS,
        "session metadata",
    )
    for name in ("session_id", "task_id", "protocol_version", "guest_id"):
        if not isinstance(metadata[name], str) or not UUIDISH.fullmatch(str(metadata[name])):
            raise ValueError(f"{name} must be a bounded identifier")
    _nullable_identifier(metadata["pair_id"], "pair_id")
    if metadata["capture_adapter"] != "x11_vm":
        raise ValueError("ScienceBoard trace capture_adapter must be x11_vm")
    if metadata["environment_scope"] != "scienceboard_vm_x11":
        raise ValueError("ScienceBoard trace environment_scope must be scienceboard_vm_x11")
    if metadata["privacy_policy"] != "x11-metadata-only-v1":
        raise ValueError("Unexpected privacy policy")
    if metadata["raw_key_capture"] is not False or metadata["include_query"] is not False:
        raise ValueError("Content-bearing key/query capture must remain disabled")
    if metadata["status"] != "recording":
        raise ValueError("Initial session status must be recording")
    _iso8601(metadata["started_at"], "started_at")
    _finite_number(metadata["monotonic_origin_ms"], "monotonic_origin_ms")
    if version == "cui-trace-v2":
        if metadata["timing_model"] != "session-global-monotonic-v1":
            raise ValueError("Unexpected V2 timing_model")
        if (
            type(metadata["document_epoch_count"]) is not int
            or metadata["document_epoch_count"] < 0
        ):
            raise ValueError("document_epoch_count must be a nonnegative integer")
    if not isinstance(metadata["environment"], str) or len(str(metadata["environment"])) > 160:
        raise ValueError("environment must be a bounded string")
    if not isinstance(metadata["executor_version"], str) or not metadata["executor_version"]:
        raise ValueError("executor_version must be recorded")
    if any(
        marker in str(metadata["executor_version"]).lower()
        for marker in ("unrecorded", "unknown", "placeholder", "blocked")
    ):
        raise ValueError("executor_version contains an unresolved provenance placeholder")
    if metadata["recorder_version"] != "x11-v2":
        raise ValueError("Formal protocol requires recorder_version x11-v2")
    if metadata["guest_os_id"] != "ubuntu" or metadata["guest_os_version"] != "22.04":
        raise ValueError("Guest OS does not match the pinned protocol")
    resolution = metadata["screen_resolution"]
    if not isinstance(resolution, Mapping) or set(resolution) != {"width", "height"}:
        raise ValueError("screen_resolution must contain exactly width and height")
    for name in ("width", "height"):
        if type(resolution[name]) is not int or not 320 <= int(resolution[name]) <= 16384:
            raise ValueError(f"screen_resolution.{name} is invalid")
    if not isinstance(metadata["scienceboard_commit"], str) or not HEX40.fullmatch(
        str(metadata["scienceboard_commit"])
    ):
        raise ValueError("scienceboard_commit must be a lowercase 40-hex commit")

    actor = metadata["actor_label"]
    expected: dict[str, tuple[object, ...]] = {
        "human": ("human", "self_human", "manual", True, False),
        "aio": ("agent", "reasoning_agent", "scienceboard-agent", False, True),
        "seeact": ("agent", "reasoning_agent", "scienceboard-agent", False, True),
        "xtest": ("agent", "scripted_automation_control", "xtest", False, True),
    }
    if actor not in expected:
        raise ValueError(f"Unsupported actor_label: {actor!r}")
    actual = (
        metadata["actor_type"],
        metadata["source_class"],
        metadata["executor"],
        metadata["consent_confirmed"],
        metadata["controller_confirmed"],
    )
    if actual != expected[str(actor)]:
        raise ValueError(f"actor/source/executor/confirmation mismatch for {actor}")
    participant = metadata["participant_code"]
    if actor == "human":
        if not isinstance(participant, str) or not re.fullmatch(
            r"self-human-[0-9]{3}", participant
        ):
            raise ValueError(
                "This protocol permits only a de-identified self-human participant_code"
            )
    elif participant is not None:
        raise ValueError("Non-human trace participant_code must be null")


def validate_event(event: Mapping[str, object]) -> None:
    """Validate one X11 event, including modality/type conditionals."""

    version = event.get("schema_version")
    if version not in {"cui-trace-v1", "cui-trace-v2"}:
        raise ValueError("Unsupported event schema_version")
    _exact_fields(
        event,
        V2_EVENT_FIELDS if version == "cui-trace-v2" else V1_EVENT_FIELDS,
        "event",
    )
    assert_privacy_safe_event(event)
    if not isinstance(event["event_id"], str) or not UUIDISH.fullmatch(str(event["event_id"])):
        raise ValueError("event_id must be a bounded identifier")
    _finite_number(event["epoch_ms"], "epoch_ms")
    _finite_number(event["monotonic_ms"], "monotonic_ms")
    if version == "cui-trace-v2":
        if type(event["event_seq"]) is not int or event["event_seq"] < 1:
            raise ValueError("event_seq must be a positive integer")
        if type(event["document_epoch"]) is not int or event["document_epoch"] < 0:
            raise ValueError("document_epoch must be a nonnegative integer")
        _finite_number(event["t_session_ms"], "t_session_ms")
        _finite_number(event["t_document_ms"], "t_document_ms")
        step_id = event["step_id"]
        if step_id is not None and (
            not isinstance(step_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", step_id)
        ):
            raise ValueError("step_id must be null or a bounded token")
    viewport = event["viewport"]
    if not isinstance(viewport, Mapping) or set(viewport) != {
        "width",
        "height",
        "device_pixel_ratio",
    }:
        raise ValueError("viewport shape is invalid")
    for name in ("width", "height"):
        if type(viewport[name]) is not int or int(viewport[name]) <= 0:
            raise ValueError(f"viewport.{name} must be a positive integer")
    _finite_number(viewport["device_pixel_ratio"], "viewport.device_pixel_ratio", minimum=0.01)
    target = event["target"]
    if not isinstance(target, Mapping) or set(target) != {"role", "application_id"}:
        raise ValueError("target shape is invalid")
    if (
        target["role"] != "desktop_application"
        or not isinstance(target["application_id"], str)
        or not APP_ID.fullmatch(str(target["application_id"]))
    ):
        raise ValueError("target application identity is not privacy-safe")

    event_type = str(event["type"])
    keyboard, pointer, wheel, key_class = (
        event["keyboard"],
        event["pointer"],
        event["wheel"],
        event["key_class"],
    )
    if event_type in {"keydown", "keyup"}:
        if key_class not in ALLOWED_KEY_CLASSES or not isinstance(keyboard, Mapping):
            raise ValueError("Keyboard events require key_class and keyboard metadata")
        allowed = {"repeat"} if event_type == "keydown" else {"repeat", "hold_ms"}
        if (
            not set(keyboard).issubset(allowed)
            or "repeat" not in keyboard
            or type(keyboard["repeat"]) is not bool
        ):
            raise ValueError("keyboard metadata shape is invalid")
        if "hold_ms" in keyboard:
            _finite_number(keyboard["hold_ms"], "keyboard.hold_ms")
        if pointer is not None or wheel is not None:
            raise ValueError("Keyboard events cannot carry pointer/wheel data")
    elif event_type in {"pointermove", "pointerdown", "pointerup"}:
        if not isinstance(pointer, Mapping) or set(pointer) != {
            "x",
            "y",
            "button",
            "pointer_type",
        }:
            raise ValueError("Pointer event metadata shape is invalid")
        _finite_number(pointer["x"], "pointer.x")
        _finite_number(pointer["y"], "pointer.y")
        if type(pointer["button"]) is not int or pointer["button"] < -1 or pointer["button"] > 32:
            raise ValueError("pointer.button is invalid")
        if pointer["pointer_type"] != "mouse":
            raise ValueError("Only mouse pointer_type is supported")
        if key_class is not None or keyboard is not None or wheel is not None:
            raise ValueError("Pointer events cannot carry keyboard/wheel data")
    elif event_type == "wheel":
        if not isinstance(wheel, Mapping) or set(wheel) != {
            "delta_x",
            "delta_y",
            "delta_mode",
        }:
            raise ValueError("Wheel metadata shape is invalid")
        _finite_number(abs(float(wheel["delta_x"])), "abs(wheel.delta_x)")
        _finite_number(abs(float(wheel["delta_y"])), "abs(wheel.delta_y)")
        if wheel["delta_mode"] != "step":
            raise ValueError("X11 wheel delta_mode must be step")
        if key_class is not None or keyboard is not None or pointer is not None:
            raise ValueError("Wheel events cannot carry keyboard/pointer data")
    elif event_type in {"focusin", "focusout"}:
        if any(value is not None for value in (key_class, keyboard, pointer, wheel)):
            raise ValueError("Focus events cannot carry input modality data")
    elif event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"Unsupported event type: {event_type}")


def validate_terminal(terminal: Mapping[str, object]) -> None:
    if set(terminal) == set(V1_TERMINAL_FIELDS):
        version = "cui-trace-v1"
    elif set(terminal) == set(V2_TERMINAL_FIELDS):
        version = "cui-trace-v2"
    else:
        _exact_fields(terminal, V2_TERMINAL_FIELDS, "session_end")
        raise AssertionError("unreachable")
    if terminal["status"] not in {"complete", "interrupted", "capture_error"}:
        raise ValueError("Invalid terminal status")
    _iso8601(terminal["stopped_at"], "stopped_at")
    if type(terminal["event_count"]) is not int or terminal["event_count"] < 0:
        raise ValueError("event_count must be a nonnegative integer")
    if terminal["capture_error"] is not None and not isinstance(terminal["capture_error"], str):
        raise ValueError("capture_error must be null or a bounded error class")
    if isinstance(terminal["capture_error"], str) and len(terminal["capture_error"]) > 120:
        raise ValueError("capture_error is too long")
    _finite_number(terminal["duration_ms"], "duration_ms")
    if version == "cui-trace-v2":
        for name in (
            "event_seq_last",
            "document_epoch_count",
            "dropped_event_count",
            "rejected_event_count",
            "flush_failure_count",
            "quota_rejection_count",
        ):
            value = terminal[name]
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if terminal["event_seq_last"] != terminal["event_count"]:
            raise ValueError("event_seq_last must match event_count")
