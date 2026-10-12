"""Small, append-only trace writer and loader for browser/X11 compatibility."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone  # not datetime.UTC: the guest runs Python 3.10
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from .validation import (
    normalize_source_class,
    validate_browser_trace,
    validate_event,
    validate_session_metadata,
    validate_terminal,
)


def _json_line(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


LEGACY_SCHEMA_VERSION = "cui-trace-v1"
CURRENT_SCHEMA_VERSION = "cui-trace-v2"
MIN_TIME_INCREMENT_MS = 0.001
DEFAULT_MAX_TRACE_BYTES = 256 * 1024 * 1024


class TraceQuotaExceeded(RuntimeError):
    """Raised before an event would exceed the configured local trace quota."""


def _observed_browser_summary(
    events: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    rows = list(events)
    by_type: dict[str, int] = {}
    for event in rows:
        event_type = str(event.get("type"))
        by_type[event_type] = by_type.get(event_type, 0) + 1
    return {
        "event_count": len(rows),
        "duration_ms": 0.0,
        "pointer_distance_px": 0.0,
        "by_type": by_type,
    }


def _normalize_shared_metadata(metadata: Mapping[str, object]) -> dict[str, Any]:
    normalized = dict(metadata)
    normalized["source_class"] = normalize_source_class(normalized.get("source_class"))
    return normalized


def _browser_terminal(
    metadata: Mapping[str, object],
    summary: Mapping[str, object],
    event_count: int,
) -> dict[str, Any]:
    """Derive the shared terminal record from a finalized browser export.

    Browser JSON and JSONL intentionally store final state in metadata rather
    than appending an X11-style ``session_end`` record.  Returning only status
    and count made the two browser formats disagree and silently discarded V2
    loss counters.  This adapter constructs the same closed terminal envelope
    for both formats and validates it before publication to downstream code.
    """

    stopped_at = metadata.get("stopped_at")
    if not isinstance(stopped_at, str) or not stopped_at:
        raise ValueError("Finalized browser trace requires metadata.stopped_at")
    capture_error = metadata.get("capture_error")
    raw_status = metadata.get("status")
    status = (
        "capture_error"
        if capture_error is not None
        else "complete"
        if raw_status == "complete"
        else "interrupted"
    )
    terminal: dict[str, Any] = {
        "status": status,
        "stopped_at": stopped_at,
        "event_count": event_count,
        "capture_error": capture_error,
        "duration_ms": summary.get("duration_ms"),
    }
    if metadata.get("schema_version") == CURRENT_SCHEMA_VERSION:
        for field in (
            "event_seq_last",
            "document_epoch_count",
            "dropped_event_count",
            "rejected_event_count",
            "flush_failure_count",
            "quota_rejection_count",
        ):
            terminal[field] = metadata.get(field)
    validate_terminal(terminal)
    return terminal


def _session_relative_duration(events: Iterable[Mapping[str, object]]) -> float:
    """Return the V2 session-clock time of the final event.

    The extension's ``summary.duration_ms`` is the first-to-last event span,
    which is shorter than the session whenever the first event comes after
    ``t_session_ms == 0``. Features, the viewer and the X11 recorder all
    measure duration from session start, so finalized browser terminals use
    that convention too (as ``recover_stale_partials`` does for X11).
    """

    return round(
        max((_finite_or_default(event.get("t_session_ms"), 0.0) for event in events), default=0.0),
        3,
    )


def _with_session_relative_duration(
    terminal: Mapping[str, object], events: Sequence[Mapping[str, object]]
) -> dict[str, Any]:
    updated = {**terminal, "duration_ms": _session_relative_duration(events)}
    validate_terminal(updated)
    return updated


def _validate_migration_source(
    metadata: Mapping[str, object],
    events: Sequence[Mapping[str, object]],
    terminal: Mapping[str, object],
) -> None:
    """Fail closed before adding V2 fields to an existing trace."""

    if metadata.get("capture_adapter") == "x11_vm":
        validate_session_metadata(metadata)
        for event in events:
            validate_event(event)
        if terminal:
            validate_terminal(terminal)
        return
    validate_browser_trace(metadata, _observed_browser_summary(events), events)


def _upgrade_legacy_browser_v1(
    metadata: Mapping[str, object], events: Sequence[Mapping[str, object]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Expand the exact retained standalone V1 branch without relabeling it.

    The legacy schema omitted extension provenance and several later-nullable
    modality fields. We add explicit ``legacy-*`` provenance and null/default
    structural values; unknown fields and raw content have already failed the
    closed validation gate.
    """

    origin: str | None = None
    for event in events:
        parsed = urlsplit(str(event.get("url", "")))
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            break
    if origin is None:
        raise ValueError("Legacy browser trace has no safe http(s) scope origin")

    upgraded_metadata = dict(metadata)
    evidence_scope = str(upgraded_metadata.pop("evidence_scope"))
    upgraded_metadata.update(
        {
            "pair_id": "unpaired",
            "protocol_version": "legacy-standalone-v1",
            "recorder_version": "legacy-standalone-v1",
            "capture_adapter": "browser_standalone_mirror",
            "environment_scope": "standalone_page",
            "source_class": "human_self"
            if upgraded_metadata.get("actor_type") == "human"
            else "cui_agent",
            "scope_origin": origin,
            "disclosure_version": "legacy-not-recorded",
            "retention_policy": "legacy-not-recorded",
            "notes": evidence_scope[:500],
            "capture_error": None,
            "chunk_count": 0,
        }
    )

    upgraded_events: list[dict[str, Any]] = []
    for source in events:
        event = dict(source)
        pointer = event.get("pointer")
        if isinstance(pointer, Mapping):
            pointer = dict(pointer)
            for name in (
                "pointer_id",
                "width",
                "height",
                "pressure",
                "tangential_pressure",
                "tilt_x",
                "tilt_y",
                "twist",
                "is_primary",
            ):
                pointer.setdefault(name, None)
            event["pointer"] = pointer
        event["keyboard"] = (
            {
                "repeat": False,
                "location": 0,
                "is_composing": False,
                "hold_ms": None,
            }
            if event.get("type") in {"keydown", "keyup"}
            else None
        )
        input_summary = event.get("input")
        if isinstance(input_summary, Mapping):
            input_summary = dict(input_summary)
            input_summary.setdefault("is_composing", False)
            event["input"] = input_summary
        for name in ("clipboard", "transfer", "touch", "form", "document_state"):
            event[name] = None
        upgraded_events.append(event)
    return upgraded_metadata, upgraded_events


def migrate_trace_v1_to_v2(
    metadata: Mapping[str, object],
    events: Iterable[Mapping[str, object]],
    terminal: Mapping[str, object] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Deterministically add the V2 ordering envelope to a V1 trace.

    Source records are never mutated. Browser navigation records and a reset
    in document-relative monotonic time create a new ``document_epoch``.
    Session time is derived from the captured wall-clock delta and clamped
    strictly forward by one microsecond, making the result deterministic even
    for duplicated or clock-regressing input.
    """

    source_metadata = dict(metadata)
    source_events = [dict(event) for event in events]
    source_terminal = dict(terminal or {})
    _validate_migration_source(source_metadata, source_events, source_terminal)
    if source_metadata.get("schema_version") == CURRENT_SCHEMA_VERSION:
        return (
            _normalize_shared_metadata(source_metadata),
            source_events,
            source_terminal,
        )
    if source_metadata.get("schema_version") != LEGACY_SCHEMA_VERSION:
        raise ValueError("Only cui-trace-v1 and cui-trace-v2 are supported")
    if source_metadata.get("capture_adapter") != "x11_vm" and "evidence_scope" in source_metadata:
        source_metadata, source_events = _upgrade_legacy_browser_v1(source_metadata, source_events)

    try:
        started_epoch_ms = datetime_to_epoch_ms(source_metadata.get("started_at"))
    except (TypeError, ValueError):
        started_epoch_ms = (
            float(cast(Any, source_events[0].get("epoch_ms", 0.0))) if source_events else 0.0
        )
    document_epoch = 0
    previous_document_ms: float | None = None
    previous_session_ms = -MIN_TIME_INCREMENT_MS
    migrated_events: list[dict[str, Any]] = []
    for index, source in enumerate(source_events):
        event = dict(source)
        if source_metadata.get("capture_adapter") != "x11_vm" and isinstance(
            event.get("keyboard"), Mapping
        ):
            keyboard = dict(cast(Mapping[str, object], event["keyboard"]))
            keyboard.setdefault("hold_ms", None)
            event["keyboard"] = keyboard
        document_ms = _finite_or_default(event.get("t_document_ms", event.get("monotonic_ms")), 0.0)
        if index and (
            event.get("type") == "navigation"
            or (previous_document_ms is not None and document_ms < previous_document_ms)
        ):
            document_epoch += 1
        wall_relative = max(
            0.0,
            _finite_or_default(event.get("epoch_ms"), started_epoch_ms) - started_epoch_ms,
        )
        session_ms = max(previous_session_ms + MIN_TIME_INCREMENT_MS, wall_relative)
        event.update(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "event_seq": index + 1,
                "document_epoch": document_epoch,
                "t_session_ms": round(session_ms, 3),
                "t_document_ms": round(max(0.0, document_ms), 3),
                "step_id": _safe_step_id(event.get("step_id")),
            }
        )
        migrated_events.append(event)
        previous_document_ms = document_ms
        previous_session_ms = session_ms

    source_metadata.update(
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "timing_model": "session-global-monotonic-v1",
            "document_epoch_count": document_epoch + 1 if migrated_events else 0,
        }
    )
    if source_metadata.get("capture_adapter") != "x11_vm":
        source_metadata.setdefault("capture_adapter", "browser_mv3")
        source_metadata.setdefault("environment_scope", "active_origin")
        source_metadata.setdefault(
            "source_class",
            "human_self" if source_metadata.get("actor_type") == "human" else "cui_agent",
        )
        source_metadata.setdefault("notes", "")
        source_metadata.setdefault("capture_error", None)
    # Browser JSON keeps final counters in metadata; append-only X11 JSONL
    # keeps them in the terminal record so its immutable header stays truthful.
    if source_metadata.get("capture_adapter") != "x11_vm" or "chunk_count" in source_metadata:
        source_metadata.setdefault("event_seq_last", len(migrated_events))
        source_metadata.setdefault("dropped_event_count", 0)
        source_metadata.setdefault("rejected_event_count", 0)
        source_metadata.setdefault("flush_failure_count", 0)
        source_metadata.setdefault("quota_rejection_count", 0)
    if source_terminal:
        source_terminal.setdefault("event_seq_last", len(migrated_events))
        source_terminal.setdefault(
            "document_epoch_count", document_epoch + 1 if migrated_events else 0
        )
        source_terminal.setdefault("dropped_event_count", 0)
        source_terminal.setdefault("rejected_event_count", 0)
        source_terminal.setdefault("flush_failure_count", 0)
        source_terminal.setdefault("quota_rejection_count", 0)
    return _normalize_shared_metadata(source_metadata), migrated_events, source_terminal


def _finite_or_default(value: object, default: float) -> float:
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def _safe_step_id(value: object) -> str | None:
    import re

    raw = str(value or "").strip()
    return raw if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", raw) else None


def datetime_to_epoch_ms(value: object) -> float:
    from datetime import datetime

    if not isinstance(value, str):
        raise TypeError("started_at is not an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.timestamp() * 1_000


class TraceWriter:
    """Append privacy-checked events to a local JSONL file.

    The first record is immutable session metadata. A terminal record makes a
    clean stop explicit; truncated files remain identifiable as interrupted.
    Records are flushed in bounded batches to avoid making disk latency a
    behavioral confound. A terminal record is always flushed and fsynced.
    """

    def __init__(
        self,
        path: Path,
        metadata: Mapping[str, object],
        *,
        sync_every_events: int = 64,
        sync_interval_seconds: float = 0.25,
        max_trace_bytes: int = DEFAULT_MAX_TRACE_BYTES,
    ) -> None:
        validate_session_metadata(metadata)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise FileExistsError(self.path)
        self.partial_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.partial")
        self._handle = self.partial_path.open("x", encoding="utf-8", newline="\n")
        self.event_count = 0
        self.closed = False
        self.sync_every_events = max(1, int(sync_every_events))
        self.sync_interval_seconds = max(0.01, float(sync_interval_seconds))
        if (
            not isinstance(max_trace_bytes, int)
            or isinstance(max_trace_bytes, bool)
            or max_trace_bytes < 1
        ):
            raise ValueError("max_trace_bytes must be a positive integer")
        self.max_trace_bytes = max_trace_bytes
        self._bytes_written = 0
        self._unsynced_events = 0
        self._last_sync = time.monotonic()
        self.started_monotonic_ns = time.perf_counter_ns()
        self.started_epoch_ms = time.time_ns() / 1_000_000
        self._last_session_ms = -MIN_TIME_INCREMENT_MS
        self._document_epoch_count = 0
        self.dropped_event_count = 0
        self.rejected_event_count = 0
        self.flush_failure_count = 0
        self.quota_rejection_count = 0
        header_metadata, _, _ = (
            migrate_trace_v1_to_v2(metadata, [], {})
            if metadata.get("schema_version") == LEGACY_SCHEMA_VERSION
            else (dict(metadata), [], {})
        )
        validate_session_metadata(header_metadata)
        self.metadata = header_metadata
        self._write({"record_type": "session", "metadata": header_metadata}, force=True)

    def _write(
        self,
        payload: Mapping[str, object],
        *,
        force: bool = False,
        enforce_quota: bool = False,
    ) -> None:
        rendered = _json_line(payload)
        encoded_bytes = len(rendered.encode("utf-8"))
        if enforce_quota and self._bytes_written + encoded_bytes > self.max_trace_bytes:
            raise TraceQuotaExceeded("trace event would exceed max_trace_bytes")
        self._handle.write(rendered)
        self._bytes_written += encoded_bytes
        should_sync = (
            force
            or self._unsynced_events >= self.sync_every_events
            or time.monotonic() - self._last_sync >= self.sync_interval_seconds
        )
        if should_sync:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._unsynced_events = 0
            self._last_sync = time.monotonic()

    def append(self, event: Mapping[str, object]) -> None:
        if self.closed:
            raise RuntimeError("Cannot append after the trace has stopped")
        source = dict(event)
        try:
            validate_event(source)
            document_epoch = int(cast(Any, source.get("document_epoch", 0)))
            if document_epoch < 0:
                raise ValueError("document_epoch must be nonnegative")
            document_ms = _finite_or_default(
                source.get("t_document_ms", source.get("monotonic_ms")), 0.0
            )
            requested_session_ms = _finite_or_default(
                source.get("t_session_ms", source.get("monotonic_ms")), 0.0
            )
            session_ms = max(self._last_session_ms + MIN_TIME_INCREMENT_MS, requested_session_ms)
            source.update(
                {
                    "schema_version": CURRENT_SCHEMA_VERSION,
                    "event_seq": self.event_count + 1,
                    "document_epoch": document_epoch,
                    "t_session_ms": round(session_ms, 3),
                    "t_document_ms": round(max(0.0, document_ms), 3),
                    "step_id": _safe_step_id(source.get("step_id")),
                }
            )
            validate_event(source)
        except (TypeError, ValueError):
            self.rejected_event_count += 1
            raise
        self._unsynced_events += 1
        try:
            self._write(
                {"record_type": "event", **source},
                enforce_quota=True,
            )
        except TraceQuotaExceeded:
            self._unsynced_events -= 1
            self.quota_rejection_count += 1
            raise
        except OSError:
            self.flush_failure_count += 1
            raise
        self.event_count += 1
        self._last_session_ms = session_ms
        self._document_epoch_count = max(self._document_epoch_count, document_epoch + 1)

    def close(self, *, status: str, stopped_at: str, capture_error: str | None = None) -> None:
        if self.closed:
            return
        terminal = {
            "status": status,
            "stopped_at": stopped_at,
            "event_count": self.event_count,
            "capture_error": capture_error,
            "duration_ms": round(
                max(
                    0.0,
                    (time.perf_counter_ns() - self.started_monotonic_ns) / 1_000_000,
                ),
                3,
            ),
            "event_seq_last": self.event_count,
            "document_epoch_count": self._document_epoch_count,
            "dropped_event_count": self.dropped_event_count,
            "rejected_event_count": self.rejected_event_count,
            "flush_failure_count": self.flush_failure_count,
            "quota_rejection_count": self.quota_rejection_count,
        }
        validate_terminal(terminal)
        self._write({"record_type": "session_end", **terminal}, force=True)
        self._handle.close()
        os.replace(self.partial_path, self.path)
        try:
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Windows does not consistently permit directory fsync. os.replace
            # still prevents consumers from observing a half-written final.
            pass
        self.closed = True

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, _tb: object) -> None:
        self.close(
            status="complete" if exc is None else "interrupted",
            stopped_at=datetime.now(timezone.utc).isoformat(),
            capture_error=None if exc is None else type(exc).__name__,
        )


def recover_stale_partials(final_path: Path) -> list[dict[str, object]]:
    """Recover crash-left X11 partials without treating them as formal data.

    A fully finalized partial is atomically promoted to its intended final
    path.  A partial with a valid header/events but no terminal is copied into
    a separate ``recovered-interrupted`` trace with an explicit terminal.  At
    most one incomplete final line is discarded and counted as a flush
    failure.  Any earlier corruption is left untouched for operator review.
    """

    final_path = Path(final_path)
    parent = final_path.parent
    reports: list[dict[str, object]] = []
    if not parent.is_dir():
        return reports
    pattern = f".{final_path.name}.*.partial"
    for partial in sorted(parent.glob(pattern), key=lambda item: item.name):
        report: dict[str, object] = {
            "partial_name": partial.name,
            "status": "rejected",
            "recovered_name": None,
            "trailing_fragment_discarded": False,
        }
        try:
            raw = partial.read_bytes()
            lines = raw.splitlines(keepends=True)
            if not lines:
                raise ValueError("empty partial")
            trailing_fragment = not lines[-1].endswith((b"\n", b"\r"))
            complete_lines = lines[:-1] if trailing_fragment else lines
            parsed: list[dict[str, Any]] = []
            for index, line in enumerate(complete_lines):
                try:
                    value = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError(
                        f"invalid complete JSONL record at line {index + 1}"
                    ) from error
                if not isinstance(value, dict):
                    raise ValueError(f"non-object JSONL record at line {index + 1}")
                parsed.append(value)
            if not parsed:
                raise ValueError("partial has no complete session header")
            if parsed[0].get("record_type") != "session":
                raise ValueError("partial does not start with a session header")
            if any(record.get("record_type") == "session_end" for record in parsed[:-1]):
                raise ValueError("record follows a terminal record")

            candidate = parent / f".{partial.name}.recovery-check"
            candidate.write_bytes(b"".join(complete_lines))
            try:
                _metadata, events, _terminal = load_trace(candidate)
            finally:
                candidate.unlink(missing_ok=True)

            terminal_present = parsed[-1].get("record_type") == "session_end"
            if terminal_present:
                if trailing_fragment:
                    raise ValueError("bytes follow a complete terminal record")
                if final_path.exists():
                    raise FileExistsError("intended final trace already exists")
                os.replace(partial, final_path)
                report.update(
                    {
                        "status": "promoted_complete",
                        "recovered_name": final_path.name,
                        "event_count": len(events),
                    }
                )
                reports.append(report)
                continue

            stopped_at = datetime.now(timezone.utc).isoformat()
            recovered_terminal = {
                "status": "interrupted",
                "stopped_at": stopped_at,
                "event_count": len(events),
                "capture_error": "process_crash_recovered",
                "duration_ms": round(
                    max((float(event.get("t_session_ms", 0.0)) for event in events), default=0.0),
                    3,
                ),
                "event_seq_last": len(events),
                "document_epoch_count": max(
                    (int(event.get("document_epoch", 0)) for event in events),
                    default=-1,
                )
                + 1,
                "dropped_event_count": 0,
                "rejected_event_count": 0,
                "flush_failure_count": 1 if trailing_fragment else 0,
                "quota_rejection_count": 0,
            }
            validate_terminal(recovered_terminal)
            recovered = parent / (
                f"{final_path.stem}.recovered-interrupted-{uuid.uuid4().hex}.jsonl"
            )
            temporary = recovered.with_name(f".{recovered.name}.tmp")
            with temporary.open("xb") as stream:
                stream.write(b"".join(complete_lines))
                stream.write(
                    _json_line({"record_type": "session_end", **recovered_terminal}).encode("utf-8")
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, recovered)
            # Validate the promoted artifact before removing the crash source.
            _, recovered_events, validated_terminal = load_trace(recovered)
            if (
                len(recovered_events) != len(events)
                or validated_terminal.get("status") != "interrupted"
            ):
                raise ValueError("recovered trace failed readback validation")
            partial.unlink()
            report.update(
                {
                    "status": "recovered_interrupted",
                    "recovered_name": recovered.name,
                    "event_count": len(events),
                    "trailing_fragment_discarded": trailing_fragment,
                }
            )
        except (OSError, ValueError, TypeError) as error:
            report["error_class"] = type(error).__name__
        reports.append(report)
    return reports


def load_trace(
    path: Path, *, migrate_to_v2: bool = True
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Load browser JSON/JSONL or X11 append-only JSONL into one V2 view."""

    path = Path(path)
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or set(payload) != {
            "metadata",
            "summary",
            "events",
        }:
            raise ValueError("Browser JSON root must contain exactly metadata/summary/events")
        if not isinstance(payload["metadata"], Mapping):
            raise ValueError("Browser JSON metadata must be an object")
        if not isinstance(payload["summary"], Mapping):
            raise ValueError("Browser JSON summary must be an object")
        if not isinstance(payload["events"], list):
            raise ValueError("Browser JSON events must be an array")
        validate_browser_trace(payload["metadata"], payload["summary"], payload["events"])
        result = (
            dict(payload["metadata"]),
            list(payload["events"]),
            _browser_terminal(payload["metadata"], payload["summary"], len(payload["events"])),
        )
        loaded = migrate_trace_v1_to_v2(*result) if migrate_to_v2 else result
        loaded_terminal = loaded[2]
        if loaded[0].get("schema_version") == CURRENT_SCHEMA_VERSION:
            assert_v2_stream_integrity(loaded[1])
            # Only V2 events (native or migrated) carry the session clock.
            loaded_terminal = _with_session_relative_duration(loaded_terminal, loaded[1])
        return _normalize_shared_metadata(loaded[0]), loaded[1], loaded_terminal

    metadata: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    summary: dict[str, Any] | None = None
    terminal: dict[str, Any] = {"status": "interrupted", "event_count": None}
    terminal_seen = False
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"Trace record at line {line_number} is not an object")
            if terminal_seen:
                raise ValueError(f"Trace record follows session_end at line {line_number}")
            kind = record.pop("record_type", None)
            if kind == "session":
                if metadata:
                    raise ValueError(f"Duplicate session record at line {line_number}")
                has_metadata = "metadata" in record
                has_session = "session" in record
                if has_metadata == has_session:
                    raise ValueError(
                        f"Session record must contain exactly one metadata/session object at line {line_number}"
                    )
                metadata_value = record.get("metadata") if has_metadata else record.get("session")
                if not isinstance(metadata_value, Mapping):
                    raise ValueError(f"Session metadata is not an object at line {line_number}")
                metadata = dict(metadata_value)
                if metadata.get("capture_adapter") == "x11_vm":
                    if set(record) != {"metadata"}:
                        raise ValueError(
                            f"X11 session record has unexpected fields at line {line_number}"
                        )
                    validate_session_metadata(metadata)
                else:
                    allowed_header = {
                        "session" if has_session else "metadata",
                        "schema_version",
                        "summary",
                    }
                    if not set(record).issubset(allowed_header):
                        raise ValueError(
                            f"Browser session record has unexpected fields at line {line_number}"
                        )
                    if record.get("schema_version", metadata.get("schema_version")) != metadata.get(
                        "schema_version"
                    ):
                        raise ValueError("Browser header schema_version mismatch")
                    if not isinstance(record.get("summary"), Mapping):
                        raise ValueError("Browser JSONL header must contain a summary object")
                    summary = dict(record["summary"])
            elif kind == "event":
                if not metadata:
                    raise ValueError(f"Event precedes session record at line {line_number}")
                if metadata.get("capture_adapter") == "x11_vm":
                    validate_event(record)
                events.append(record)
            elif kind == "session_end":
                if not metadata:
                    raise ValueError(f"session_end precedes session record at line {line_number}")
                validate_terminal(record)
                terminal = record
                terminal_seen = True
            else:
                raise ValueError(f"Unknown trace record_type at line {line_number}: {kind!r}")
    if not metadata:
        raise ValueError(f"Trace has no session metadata: {path}")
    is_x11 = metadata.get("capture_adapter") == "x11_vm"
    if not is_x11:
        if summary is None:
            raise ValueError("Browser JSONL trace has no summary")
        validate_browser_trace(metadata, summary, events)
        if not terminal_seen:
            terminal = _browser_terminal(metadata, summary, len(events))
    elif not terminal_seen:
        # An immutable X11 header always says recording; absence of the atomic
        # terminal is evidence of interruption, never completion.
        terminal = {"status": "interrupted", "event_count": None}
    if terminal.get("event_count") not in (None, len(events)):
        raise ValueError("Terminal event_count does not match parsed events")
    if migrate_to_v2 and metadata.get("schema_version") == LEGACY_SCHEMA_VERSION:
        metadata, events, terminal = migrate_trace_v1_to_v2(metadata, events, terminal)
    if metadata.get("schema_version") == CURRENT_SCHEMA_VERSION:
        assert_v2_stream_integrity(events)
        if not is_x11 and not terminal_seen:
            # After migration, so V1 events already carry ``t_session_ms``.
            terminal = _with_session_relative_duration(terminal, events)
    return _normalize_shared_metadata(metadata), events, terminal


def validate_event_stream(events: Iterable[Mapping[str, object]]) -> dict[str, int]:
    """Report timing/identifier quality without silently dropping events."""

    seen: set[str] = set()
    previous: float | None = None
    duplicate_ids = 0
    duplicate_timestamps = 0
    non_monotonic = 0
    event_seq_gap_count = 0
    event_seq_duplicate_count = 0
    document_epoch_regression_count = 0
    negative_document_time_count = 0
    seen_sequences: set[int] = set()
    previous_document_epoch: int | None = None
    count = 0
    for event in events:
        count += 1
        event_id = str(event.get("event_id", ""))
        if not event_id or event_id in seen:
            duplicate_ids += 1
        seen.add(event_id)
        current = float(cast(Any, event.get("t_session_ms", event.get("monotonic_ms", 0.0))))
        if previous is not None:
            if current == previous:
                duplicate_timestamps += 1
            elif current < previous:
                non_monotonic += 1
        previous = current
        if event.get("schema_version") == CURRENT_SCHEMA_VERSION or "event_seq" in event:
            sequence = int(cast(Any, event.get("event_seq", 0)))
            if sequence in seen_sequences:
                event_seq_duplicate_count += 1
            seen_sequences.add(sequence)
            if sequence != count:
                event_seq_gap_count += 1
            document_epoch = int(cast(Any, event.get("document_epoch", -1)))
            if previous_document_epoch is not None and document_epoch < previous_document_epoch:
                document_epoch_regression_count += 1
            previous_document_epoch = document_epoch
            if float(cast(Any, event.get("t_document_ms", -1.0))) < 0:
                negative_document_time_count += 1
    return {
        "event_count": count,
        "duplicate_event_id_count": duplicate_ids,
        "duplicate_timestamp_count": duplicate_timestamps,
        "non_monotonic_transition_count": non_monotonic,
        "event_seq_gap_count": event_seq_gap_count,
        "event_seq_duplicate_count": event_seq_duplicate_count,
        "document_epoch_regression_count": document_epoch_regression_count,
        "negative_document_time_count": negative_document_time_count,
    }


def assert_v2_stream_integrity(events: Iterable[Mapping[str, object]]) -> None:
    """Fail closed when a V2 stream cannot support deterministic analysis."""

    rows = list(events)
    quality = validate_event_stream(rows)
    violations = {
        name: quality[name]
        for name in (
            "duplicate_event_id_count",
            "duplicate_timestamp_count",
            "non_monotonic_transition_count",
            "event_seq_gap_count",
            "event_seq_duplicate_count",
            "document_epoch_regression_count",
            "negative_document_time_count",
        )
        if quality[name]
    }
    previous_time = -float("inf")
    previous_epoch = -1
    for index, event in enumerate(rows, 1):
        sequence = event.get("event_seq")
        epoch = event.get("document_epoch")
        session_time = _finite_or_default(event.get("t_session_ms"), -1.0)
        document_time = _finite_or_default(event.get("t_document_ms"), -1.0)
        if type(sequence) is not int or sequence != index:
            violations["invalid_event_seq"] = violations.get("invalid_event_seq", 0) + 1
        if type(epoch) is not int or epoch < previous_epoch:
            violations["invalid_document_epoch"] = violations.get("invalid_document_epoch", 0) + 1
        if session_time <= previous_time:
            violations["invalid_session_time"] = violations.get("invalid_session_time", 0) + 1
        if document_time < 0:
            violations["invalid_document_time"] = violations.get("invalid_document_time", 0) + 1
        previous_time = session_time
        if type(epoch) is int:
            previous_epoch = epoch
    if violations:
        raise ValueError(f"Invalid TraceEnvelopeV2 event stream: {violations}")
