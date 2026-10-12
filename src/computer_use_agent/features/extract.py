"""Deterministic, leakage-reviewed behavioral features for CUI traces."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..trace.io import load_trace, validate_event_stream

# These fields may label or uniquely identify an observation. They are valid
# provenance, but must never be copied into the model matrix.
LEAKAGE_FIELDS = frozenset(
    {
        "actor_label",
        "actor_type",
        "application_id",
        "browser",
        "capture_adapter",
        "capture_error",
        "document_epoch",
        "environment",
        "environment_scope",
        "epoch_ms",
        "event_seq",
        "executor",
        "executor_version",
        "guest_id",
        "guest_os_id",
        "guest_os_version",
        "is_trusted",
        "monotonic_ms",
        "os",
        "pair_id",
        "participant_code",
        "protocol_version",
        "recorder_version",
        "schema_version",
        "scope_origin",
        "selector",
        "selector_hint",
        "session_id",
        "scienceboard_commit",
        "screen_resolution",
        "source_class",
        "status",
        "step_id",
        "task_id",
        "t_document_ms",
        "t_session_ms",
        "url",
        "validator_outcome",
        "validator_passed",
    }
)

FEATURE_NAMES = (
    "event_count",
    "duration_ms",
    "event_rate_hz",
    "inter_event_median_ms",
    "inter_event_p90_ms",
    "inter_event_cv",
    "inter_event_burstiness",
    "time_to_first_input_ms",
    "pointer_path_px",
    "pointer_speed_px_s",
    "pointer_straightness",
    "pointer_turning_entropy",
    "click_dwell_median_ms",
    "pre_click_movement_median_px",
    "inter_key_median_ms",
    "key_hold_median_ms",
    "correction_ratio",
    "focus_transition_count",
    "event_type_entropy",
    "missing_pointer",
    "missing_keyboard",
    "missing_click_dwell",
    "missing_focus",
)

#: Feature families used for modality ablations. Every name in
#: ``FEATURE_NAMES`` belongs to exactly one group (drift-tested).
FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "timing": (
        "duration_ms",
        "event_rate_hz",
        "inter_event_median_ms",
        "inter_event_p90_ms",
        "inter_event_cv",
        "inter_event_burstiness",
        "time_to_first_input_ms",
    ),
    "pointer": (
        "pointer_path_px",
        "pointer_speed_px_s",
        "pointer_straightness",
        "pointer_turning_entropy",
        "click_dwell_median_ms",
        "pre_click_movement_median_px",
    ),
    "keyboard": (
        "inter_key_median_ms",
        "key_hold_median_ms",
        "correction_ratio",
    ),
    "focus_and_mix": (
        "event_count",
        "focus_transition_count",
        "event_type_entropy",
    ),
    "missingness": (
        "missing_pointer",
        "missing_keyboard",
        "missing_click_dwell",
        "missing_focus",
    ),
}

INPUT_EVENT_TYPES = frozenset(
    {
        "auxclick",
        "beforeinput",
        "click",
        "contextmenu",
        "input",
        "keydown",
        "keyup",
        "pointerdown",
        "pointermove",
        "pointerup",
        "touchstart",
        "wheel",
    }
)


def _number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]  # non-numbers fall back below
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _event_time(event: Mapping[str, object]) -> float:
    """Use the V2 session clock, with a V1 compatibility fallback."""

    return _number(event.get("t_session_ms", event.get("monotonic_ms")))


def _key_class_for_features(event: Mapping[str, object]) -> str:
    """Normalize browser and X11 destructive-edit semantics.

    Browser traces retain only a small allowlist of non-content control key
    names and label them ``control``. X11 never retains a key name and labels
    BackSpace/Delete as ``correction`` before persistence. Treat those two
    representations identically without exposing or inferring typed content.
    """

    key_class = str(event.get("key_class") or "unknown")
    if event.get("key") in {"Backspace", "Delete"} and key_class == "control":
        return "correction"
    return key_class


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def _cv(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    mean = statistics.fmean(values)
    if mean <= 0:
        return 0.0
    variance = statistics.fmean((value - mean) ** 2 for value in values)
    return math.sqrt(variance) / mean


def _burstiness(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    mean = statistics.fmean(values)
    variance = statistics.fmean((value - mean) ** 2 for value in values)
    deviation = math.sqrt(variance)
    denominator = deviation + mean
    return (deviation - mean) / denominator if denominator > 0 else 0.0


def _entropy(values: Iterable[object], *, normalized_bins: int | None = None) -> float:
    counts = Counter(values)
    total = sum(counts.values())
    if total <= 0 or len(counts) <= 1:
        return 0.0
    entropy = -sum((count / total) * math.log2(count / total) for count in counts.values())
    denominator = math.log2(normalized_bins or len(counts))
    return entropy / denominator if denominator > 0 else 0.0


def _point(event: Mapping[str, object]) -> tuple[float, float] | None:
    pointer = event.get("pointer")
    if not isinstance(pointer, Mapping):
        return None
    if "x" not in pointer or "y" not in pointer:
        return None
    return _number(pointer.get("x")), _number(pointer.get("y"))


def _distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(second[0] - first[0], second[1] - first[1])


def _path_distance(points: Sequence[tuple[float, float, float]]) -> float:
    return sum(
        _distance(points[index - 1][1:], points[index][1:]) for index in range(1, len(points))
    )


def _turning_entropy(points: Sequence[tuple[float, float, float]]) -> float:
    directions: list[float] = []
    for index in range(1, len(points)):
        dx = points[index][1] - points[index - 1][1]
        dy = points[index][2] - points[index - 1][2]
        if dx or dy:
            directions.append(math.atan2(dy, dx))
    if len(directions) < 2:
        return 0.0
    turn_bins = []
    for index in range(1, len(directions)):
        delta = (directions[index] - directions[index - 1] + math.pi) % (2 * math.pi) - math.pi
        turn_bins.append(min(7, int(((delta + math.pi) / (2 * math.pi)) * 8)))
    return _entropy(turn_bins, normalized_bins=8)


def extract_features(
    events: Sequence[Mapping[str, object]],
    *,
    trace_origin_ms: float = 0.0,
    session_duration_ms: float | None = None,
) -> dict[str, Any]:
    """Extract a fixed feature vector and separate data-quality diagnostics.

    Non-monotonic input is retained and flagged, then a stable time sort is
    used for metrics. Duplicate timestamps yield zero intervals. Unmatched
    key/button pairs are reported rather than assigned invented durations.
    """

    quality = validate_event_stream(events)
    ordered = sorted(
        enumerate(events),
        key=lambda pair: (_event_time(pair[1]), pair[0]),
    )
    timeline = [event for _, event in ordered]
    times = [_event_time(event) for event in timeline]
    origin = _number(trace_origin_ms)
    observed_duration = max(0.0, max(times) - origin) if times else 0.0
    duration_ms = (
        max(0.0, _number(session_duration_ms))
        if session_duration_ms is not None
        else observed_duration
    )
    if times and duration_ms + origin < max(times):
        raise ValueError("session_duration_ms ends before the final event")
    deltas = [max(0.0, times[index] - times[index - 1]) for index in range(1, len(times))]

    input_times = [
        _event_time(event) for event in timeline if event.get("type") in INPUT_EVENT_TYPES
    ]
    time_to_first_input = max(0.0, input_times[0] - origin) if input_times else 0.0

    pointer_points = [
        (_event_time(event), *point)
        for event in timeline
        if event.get("type") in {"pointermove", "pointerdown", "pointerup"}
        and (point := _point(event)) is not None
    ]
    pointer_path = _path_distance(pointer_points)
    pointer_duration_ms = (
        pointer_points[-1][0] - pointer_points[0][0] if len(pointer_points) >= 2 else 0.0
    )
    pointer_speed = pointer_path / (pointer_duration_ms / 1_000) if pointer_duration_ms > 0 else 0.0
    pointer_displacement = (
        _distance(pointer_points[0][1:], pointer_points[-1][1:])
        if len(pointer_points) >= 2
        else 0.0
    )
    pointer_straightness = pointer_displacement / pointer_path if pointer_path > 0 else 0.0

    pointer_down: dict[int, deque[float]] = defaultdict(deque)
    dwell_times: list[float] = []
    unmatched_pointer_ups = 0
    pre_click_distances: list[float] = []
    historical_points: list[tuple[float, float, float]] = []
    for event in timeline:
        event_type = event.get("type")
        timestamp = _event_time(event)
        point = _point(event)
        if point is not None:
            historical_points.append((timestamp, *point))
        pointer = event.get("pointer")
        if not isinstance(pointer, Mapping):
            continue
        button = int(_number(pointer.get("button"), 0))
        if event_type == "pointerdown":
            pointer_down[button].append(timestamp)
            window = [item for item in historical_points[:-1] if 0 <= timestamp - item[0] <= 500]
            if point is not None and window:
                pre_click_distances.append(_path_distance([*window, (timestamp, *point)]))
        elif event_type == "pointerup":
            if pointer_down[button]:
                dwell_times.append(max(0.0, timestamp - pointer_down[button].popleft()))
            else:
                unmatched_pointer_ups += 1

    key_down_by_class: dict[str, deque[float]] = defaultdict(deque)
    keydown_times: list[float] = []
    hold_times: list[float] = []
    correction_count = 0
    unmatched_keyups = 0
    for event in timeline:
        event_type = event.get("type")
        key_class = _key_class_for_features(event)
        timestamp = _event_time(event)
        if event_type == "keydown":
            keydown_times.append(timestamp)
            keyboard = event.get("keyboard")
            repeated = bool(keyboard.get("repeat")) if isinstance(keyboard, Mapping) else False
            if not repeated:
                key_down_by_class[key_class].append(timestamp)
            correction_count += int(key_class == "correction")
        elif event_type == "keyup":
            keyboard = event.get("keyboard")
            explicit_hold = (
                _number(keyboard.get("hold_ms"), -1) if isinstance(keyboard, Mapping) else -1
            )
            if explicit_hold >= 0:
                hold_times.append(explicit_hold)
                if key_down_by_class[key_class]:
                    key_down_by_class[key_class].popleft()
            elif key_down_by_class[key_class]:
                hold_times.append(max(0.0, timestamp - key_down_by_class[key_class].popleft()))
            else:
                unmatched_keyups += 1

    inter_key = [
        max(0.0, keydown_times[index] - keydown_times[index - 1])
        for index in range(1, len(keydown_times))
    ]
    focus_count = sum(event.get("type") in {"focusin", "focusout"} for event in timeline)
    event_types = [str(event.get("type") or "unknown") for event in timeline]

    features = {
        "event_count": float(len(timeline)),
        "duration_ms": duration_ms,
        "event_rate_hz": len(timeline) / (duration_ms / 1_000) if duration_ms > 0 else 0.0,
        "inter_event_median_ms": _median(deltas),
        "inter_event_p90_ms": _percentile(deltas, 0.9),
        "inter_event_cv": _cv(deltas),
        "inter_event_burstiness": _burstiness(deltas),
        "time_to_first_input_ms": time_to_first_input,
        "pointer_path_px": pointer_path,
        "pointer_speed_px_s": pointer_speed,
        "pointer_straightness": pointer_straightness,
        "pointer_turning_entropy": _turning_entropy(pointer_points),
        "click_dwell_median_ms": _median(dwell_times),
        "pre_click_movement_median_px": _median(pre_click_distances),
        "inter_key_median_ms": _median(inter_key),
        "key_hold_median_ms": _median(hold_times),
        "correction_ratio": correction_count / len(keydown_times) if keydown_times else 0.0,
        "focus_transition_count": float(focus_count),
        "event_type_entropy": _entropy(event_types),
        "missing_pointer": float(not pointer_points),
        "missing_keyboard": float(not keydown_times),
        "missing_click_dwell": float(not dwell_times),
        "missing_focus": float(focus_count == 0),
    }
    quality.update(
        {
            "zero_duration": int(duration_ms <= 0),
            "unmatched_pointer_down_count": sum(len(queue) for queue in pointer_down.values()),
            "unmatched_pointer_up_count": unmatched_pointer_ups,
            "unmatched_key_down_count": sum(len(queue) for queue in key_down_by_class.values()),
            "unmatched_key_up_count": unmatched_keyups,
        }
    )
    validate_feature_vector(features)
    return {
        "schema_version": "cui-behavior-features-v1",
        "features": {name: round(float(features[name]), 8) for name in FEATURE_NAMES},
        "quality": quality,
    }


def validate_feature_vector(features: Mapping[str, object]) -> None:
    if set(features) != set(FEATURE_NAMES):
        missing = sorted(set(FEATURE_NAMES) - set(features))
        unexpected = sorted(set(features) - set(FEATURE_NAMES))
        raise ValueError(f"Feature schema mismatch; missing={missing}, unexpected={unexpected}")
    leaked = set(features) & LEAKAGE_FIELDS
    if leaked:
        raise ValueError(f"Leakage fields entered feature vector: {sorted(leaked)}")
    for name in FEATURE_NAMES:
        if not math.isfinite(float(features[name])):  # type: ignore[arg-type]
            raise ValueError(f"Feature {name} is not finite")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    metadata, events, terminal = load_trace(args.trace)
    result = extract_features(
        events,
        trace_origin_ms=float(metadata.get("monotonic_origin_ms", 0.0)),
        session_duration_ms=(
            float(terminal["duration_ms"]) if terminal.get("duration_ms") is not None else None
        ),
    )
    result["provenance"] = {
        # Provenance is outside ``features`` and is never passed to a model.
        "session_id": metadata.get("session_id"),
        "task_id": metadata.get("task_id"),
        "terminal_status": terminal.get("status"),
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
