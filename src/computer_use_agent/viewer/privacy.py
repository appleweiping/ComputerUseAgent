"""Server-side projection from research artifacts to a privacy-safe viewer DTO."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from computer_use_agent.detector.reporting import RESULT_SCHEMA, validate_reporting_contract
from computer_use_agent.trace.io import load_trace

VIEW_SCHEMA = "cui-local-view-v1"
MAX_TIMELINE_EVENTS = 2500
SAFE_RESULT_METRICS = (
    "count",
    "threshold",
    "accuracy",
    "balanced_accuracy",
    "agent_precision",
    "agent_recall",
    "agent_f1",
    "roc_auc",
    "brier_score",
    "expected_calibration_error",
)

POINTER_TYPES = frozenset(
    {"pointermove", "pointerdown", "pointerup", "click", "dblclick", "auxclick", "contextmenu"}
)
KEYBOARD_TYPES = frozenset(
    {"keydown", "keyup", "beforeinput", "input", "compositionstart", "compositionend"}
)
FOCUS_TYPES = frozenset({"focus", "blur", "focusin", "focusout", "visibilitychange"})
SCROLL_TYPES = frozenset({"wheel", "scroll"})
TOUCH_TYPES = frozenset({"touchstart", "touchmove", "touchend", "touchcancel"})

_URL = re.compile(r"(?:https?|file)://\S+", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s]+")
_UNIX_PATH = re.compile(r"(?<!\w)/(?:[^\s/]+/)+[^\s]+")
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
_SAFE_EVENT_TYPE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,39}$")


def _safe_text(value: object, *, maximum: int = 180) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    text = _URL.sub("[url redacted]", text)
    text = _WINDOWS_PATH.sub("[path redacted]", text)
    text = _UNIX_PATH.sub("[path redacted]", text)
    text = _EMAIL.sub("[email redacted]", text)
    text = _LONG_TOKEN.sub("[token redacted]", text)
    return text[:maximum] or None


def _modality(event_type: str) -> str:
    if event_type in POINTER_TYPES:
        return "pointer"
    if event_type in KEYBOARD_TYPES:
        return "keyboard"
    if event_type in FOCUS_TYPES:
        return "focus"
    if event_type in SCROLL_TYPES:
        return "scroll"
    if event_type in TOUCH_TYPES:
        return "touch"
    return "other"


def _number(value: object, default: float = 0.0) -> float:
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def sanitize_loaded_trace(
    *,
    display_name: str,
    metadata: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    terminal: Mapping[str, Any],
    validator: Mapping[str, Any] | None = None,
    task_ref: str = "task-local",
) -> dict[str, Any]:
    """Expose modality and relative timing, never raw event payload fields."""

    name = _safe_text(display_name, maximum=60) or "session"
    raw_times = [_number(event.get("t_session_ms", event.get("monotonic_ms"))) for event in events]
    origin = min(raw_times) if raw_times else 0.0
    projected_events = []
    modalities: Counter[str] = Counter()
    types: Counter[str] = Counter()
    for index, event in enumerate(events):
        raw_type = str(event.get("type") or "other")
        event_type = raw_type if _SAFE_EVENT_TYPE.fullmatch(raw_type) else "other"
        modality = _modality(event_type)
        modalities[modality] += 1
        types[event_type] += 1
        projected_events.append(
            {
                "index": index + 1,
                "relative_ms": round(max(0.0, raw_times[index] - origin), 3),
                "type": event_type,
                "modality": modality,
                "document_epoch": max(0, int(_number(event.get("document_epoch"), 0))),
            }
        )
    duration = _number(terminal.get("duration_ms"), 0.0)
    if duration <= 0 and raw_times:
        duration = max(raw_times) - min(raw_times)
    raw_status = str(terminal.get("status") or metadata.get("status") or "unknown")
    status = (
        raw_status
        if raw_status in {"complete", "interrupted", "capture_error", "incomplete"}
        else "unknown"
    )
    capture_error = terminal.get("capture_error", metadata.get("capture_error"))
    if len(projected_events) > MAX_TIMELINE_EVENTS:
        first_by_type: dict[str, int] = {}
        for index, event in enumerate(projected_events):
            first_by_type.setdefault(str(event["type"]), index)
        # Reserve room for the first event of every type first, then spread
        # the remaining budget uniformly over the whole session. The grid
        # always contains the first and the final event, so sampling never
        # trims the end of the timeline, and the union stays within the cap.
        protected = set(sorted(first_by_type.values())[: MAX_TIMELINE_EVENTS - 2])
        budget = MAX_TIMELINE_EVENTS - len(protected)
        last = len(projected_events) - 1
        uniform = {round(step * last / (budget - 1)) for step in range(budget)}
        selected = sorted(uniform | protected)
        display_events = [projected_events[index] for index in selected]
        sampling = "uniform_plus_first_event_type"
    else:
        display_events = projected_events
        sampling = "none"
    actor_type = metadata.get("actor_type")
    role = actor_type if actor_type in {"human", "agent"} else "unspecified"
    return {
        "name": name,
        "role": role,
        "task_ref": _safe_text(task_ref, maximum=24),
        "capture_adapter": _safe_text(metadata.get("capture_adapter") or "unknown", maximum=40),
        "status": status,
        "failure_reason": _safe_text(capture_error),
        "event_count": len(projected_events),
        "duration_ms": round(max(0.0, duration), 3),
        "modality_counts": dict(sorted(modalities.items())),
        "event_type_counts": dict(sorted(types.items())),
        "timeline": display_events,
        "timeline_sampling": sampling,
        "timeline_display_count": len(display_events),
        "validator": sanitize_validator(validator) if validator is not None else None,
    }


def sanitize_validator(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Extract a tiny outcome projection from heterogeneous validator JSON."""

    if payload is None:
        return None
    candidates: dict[str, object] = {}

    def visit(value: object, *, depth: int = 0) -> None:
        if depth > 4 or not isinstance(value, Mapping):
            return
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in {"passed", "success", "valid"} and isinstance(item, bool):
                candidates.setdefault("passed", item)
            elif normalized in {"status", "outcome", "result"} and isinstance(
                item, (str, int, float, bool)
            ):
                candidates.setdefault("status", item)
            elif normalized in {"failure_reason", "reason", "error", "message"} and isinstance(
                item, str
            ):
                candidates.setdefault("failure_reason", item)
            elif isinstance(item, Mapping):
                visit(item, depth=depth + 1)

    visit(payload)
    return {
        "passed": candidates.get("passed") if isinstance(candidates.get("passed"), bool) else None,
        "status": _safe_text(candidates.get("status"), maximum=60),
        "failure_reason": _safe_text(candidates.get("failure_reason")),
    }


def sanitize_result(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    if payload.get("schema_version") != RESULT_SCHEMA:
        raise ValueError("Viewer accepts only the detector analysis result schema")
    validate_reporting_contract(payload)
    evaluation = payload.get("evaluation")
    if not isinstance(evaluation, Mapping):
        raise ValueError("Detector result has no evaluation object")
    primary = evaluation.get("primary_loto")
    if not isinstance(primary, Mapping) or not isinstance(primary.get("aggregate"), Mapping):
        raise ValueError("Detector result has no primary aggregate metrics")
    metrics = primary["aggregate"]
    task_control = evaluation.get("task_identification_negative_control")
    permutation = evaluation.get("label_permutation")
    bootstrap = evaluation.get("task_cluster_bootstrap")
    return {
        "claim_status": _safe_text(payload.get("claim_status"), maximum=240),
        "formal_claim_permitted": bool(payload.get("formal_claim_permitted") is True),
        "primary_metrics": {
            name: metrics[name]
            for name in SAFE_RESULT_METRICS
            if name in metrics and isinstance(metrics[name], (int, float))
        },
        "task_control": {
            name: task_control[name]
            for name in ("accuracy", "balanced_accuracy", "chance_accuracy")
            if isinstance(task_control, Mapping)
            and name in task_control
            and isinstance(task_control[name], (int, float))
        },
        "label_permutation": {
            name: permutation[name]
            for name in (
                "repetitions",
                "observed_balanced_accuracy",
                "null_mean_balanced_accuracy",
                "null_p95_balanced_accuracy",
                "p_value_one_sided",
            )
            if isinstance(permutation, Mapping)
            and name in permutation
            and isinstance(permutation[name], (int, float))
        },
        "bootstrap_intervals_95": {
            metric: {
                bound: interval[bound]
                for bound in ("lower", "upper")
                if isinstance(interval, Mapping)
                and bound in interval
                and isinstance(interval[bound], (int, float))
            }
            for metric, interval in (
                bootstrap.get("intervals_95", {}).items()
                if isinstance(bootstrap, Mapping)
                and isinstance(bootstrap.get("intervals_95"), Mapping)
                else []
            )
            if metric in SAFE_RESULT_METRICS
        },
        "task_descriptive": payload["descriptive"],
        "trajectory_artifact": payload["trajectory_artifact"],
    }


def viewer_task_aliases(task_ids: Iterable[str]) -> dict[str, str]:
    """Map source task identifiers to ``task-NN`` aliases.

    Uses the detector's ``sorted_source_task_to_ordinal_v1`` method: unique
    source identifiers in sorted order become ``task-01``, ``task-02``, ...
    so the same task set gets the same aliases as a detector result.
    """

    return {task: f"task-{index:02d}" for index, task in enumerate(sorted(set(task_ids)), start=1)}


def load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return value


def build_view_state(
    traces: Sequence[tuple[str, Path]],
    *,
    validators: Mapping[str, Path] | None = None,
    result_path: Path | None = None,
) -> dict[str, Any]:
    if not traces:
        raise ValueError("Viewer requires at least one explicitly selected trace")
    if len(traces) > 8:
        raise ValueError("Viewer is intentionally limited to eight local traces")
    validators = validators or {}
    names = [name for name, _ in traces]
    if len(names) != len(set(names)):
        raise ValueError("Viewer trace display names must be unique")
    loaded = []
    for name, path in traces:
        metadata, events, terminal = load_trace(Path(path).resolve(strict=True))
        loaded.append((name, metadata, events, terminal))
    task_aliases = viewer_task_aliases(
        str(metadata.get("task_id") or "unknown-task") for _, metadata, _, _ in loaded
    )
    projected = []
    for name, metadata, events, terminal in loaded:
        validator = load_json_object(validators[name]) if name in validators else None
        projected.append(
            sanitize_loaded_trace(
                display_name=name,
                metadata=metadata,
                events=events,
                terminal=terminal,
                validator=validator,
                task_ref=task_aliases[str(metadata.get("task_id") or "unknown-task")],
            )
        )
    result = sanitize_result(load_json_object(result_path)) if result_path else None
    state = {
        "schema_version": VIEW_SCHEMA,
        "privacy_mode": "projected_relative_timeline_no_raw_event_payloads",
        "traces": projected,
        "detector_result": result,
    }
    assert_privacy_safe_payload(state)
    return state


def assert_privacy_safe_payload(payload: Mapping[str, Any]) -> None:
    """Defense-in-depth audit for fields never permitted in the browser DTO."""

    forbidden_keys = {
        "url",
        "selector",
        "key",
        "code",
        "keyboard",
        "pointer",
        "clipboard",
        "transfer",
        "form",
        "input",
        "text",
        "notes",
        "session_id",
        "participant_code",
        "actor_label",
        "executor",
        "framework",
        "started_at",
        "stopped_at",
        "epoch_ms",
        "monotonic_ms",
        "t_session_ms",
        "t_document_ms",
        "step_id",
        "is_trusted",
    }

    def visit(value: object, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, Mapping):
            overlap = (
                set()
                if path[-1:] in {("modality_counts",), ("event_type_counts",)}
                else forbidden_keys & {str(key).lower() for key in value}
            )
            if overlap:
                raise ValueError(f"Viewer DTO contains forbidden raw fields: {sorted(overlap)}")
            for key, item in value.items():
                visit(item, (*path, str(key)))
        elif isinstance(value, list):
            for item in value:
                visit(item, path)
        elif isinstance(value, str):
            if _URL.search(value) or _WINDOWS_PATH.search(value) or _EMAIL.search(value):
                raise ValueError("Viewer DTO contains an unredacted location or address")

    visit(payload)
