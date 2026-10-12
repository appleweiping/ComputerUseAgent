"""Private artifact, trace/privacy, screenshot, and publication validation.

The functions here are deterministic and model-free.  They validate private
attempt artifacts while returning only bounded, content-free evidence for the
public adapter result.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import zlib
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .adapter_contract import (
    AdapterError,
    DEPENDENCY_FREEZE_DIGEST_FILENAME,
    DEPENDENCY_FREEZE_FILENAME,
    FRAMEWORK_ENVIRONMENT_NAMES,
    LEGACY_TRACE_SCHEMA_PATH,
    REPO_ROOT,
    STEP_ID_PATTERN,
    TRACE_SCHEMA_PATH,
)


def private_artifact_descriptor(path: Path, artifact_type: str) -> dict[str, Any]:
    """Describe a private file without publishing its path or payload."""
    if not path.is_file():
        raise AdapterError(
            "private_artifact_missing", f"expected private {artifact_type} was not written"
        )
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    sha256 = digest.hexdigest()
    return {
        "artifact_id": f"private-{artifact_type}-{sha256[:16]}",
        "retained_private": True,
        "bytes": size,
        "sha256": sha256,
    }


def optional_private_artifact_descriptor(path: Path, artifact_type: str) -> dict[str, Any] | None:
    return private_artifact_descriptor(path, artifact_type) if path.is_file() else None


def _normalized_dependency_freeze(value: str) -> str:
    return "\n".join(sorted(line.strip() for line in value.splitlines() if line.strip())) + "\n"


def dependency_environments(framework: str) -> list[tuple[str, Path, Path]]:
    try:
        from research.week1_miniwob.adapters.dispatch import environment_python, environment_root

        root = environment_root()
    except Exception as error:
        raise AdapterError("dependency_environment_unavailable", type(error).__name__) from error
    if root is None or _is_relative_to(root, REPO_ROOT.resolve()):
        raise AdapterError(
            "dependency_environment_unavailable",
            "adapter environment root must be configured outside the repository",
        )
    rows: list[tuple[str, Path, Path]] = []
    for name in FRAMEWORK_ENVIRONMENT_NAMES[framework]:
        python = environment_python(root, name)
        environment = root / name
        if not python.is_file() or not environment.is_dir():
            raise AdapterError(
                "dependency_environment_unavailable", f"isolated {name} environment is missing"
            )
        rows.append((name, environment, python))
    return rows


def verify_dependency_freezes(
    framework: str,
    *,
    compare_live_environment: bool,
    dependency_environments_fn: Callable[
        [str], list[tuple[str, Path, Path]]
    ] = dependency_environments,
) -> list[dict[str, Any]]:
    """Verify private transitive dependency snapshots without publishing package names."""

    evidence: list[dict[str, Any]] = []
    for name, environment, python in dependency_environments_fn(framework):
        freeze_path = environment / DEPENDENCY_FREEZE_FILENAME
        digest_path = environment / DEPENDENCY_FREEZE_DIGEST_FILENAME
        try:
            snapshot = freeze_path.read_text(encoding="utf-8")
            expected_digest = digest_path.read_text(encoding="ascii").strip()
        except OSError as error:
            raise AdapterError(
                "dependency_freeze_missing",
                f"{name} dependency snapshot is missing; run setup_environments.py --snapshot-existing",
            ) from error
        normalized = _normalized_dependency_freeze(snapshot)
        snapshot_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if (
            snapshot != normalized
            or not re.fullmatch(r"[a-f0-9]{64}", expected_digest)
            or expected_digest != snapshot_digest
        ):
            raise AdapterError(
                "dependency_freeze_invalid", f"{name} dependency snapshot digest changed"
            )
        if compare_live_environment:
            try:
                completed = subprocess.run(
                    [str(python), "-m", "pip", "freeze", "--all", "--local"],
                    text=True,
                    encoding="utf-8",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=45,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise AdapterError(
                    "dependency_freeze_probe_failed", f"{name} pip freeze unavailable"
                ) from error
            live = _normalized_dependency_freeze(completed.stdout)
            if (
                completed.returncode != 0
                or hashlib.sha256(live.encode("utf-8")).hexdigest() != snapshot_digest
            ):
                raise AdapterError(
                    "dependency_environment_drift",
                    f"{name} live dependency graph differs from its private freeze",
                )
        evidence.append(
            {
                "environment": name,
                "artifact": private_artifact_descriptor(freeze_path, "dependency-freeze"),
            }
        )
    return evidence


def validate_formal_trace(
    path: Path,
    *,
    expected_session_id: str,
    expected_event_count: int,
    expected_url: str,
    expected_viewport: dict[str, int],
    require_natural_event: bool = False,
) -> dict[str, Any]:
    """Validate a retained formal trace without publishing its raw contents."""

    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads(TRACE_SCHEMA_PATH.read_text(encoding="utf-8"))
        legacy_schema = json.loads(LEGACY_TRACE_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AdapterError(
            "recorder_trace_unreadable", f"{type(error).__name__}: trace/schema JSON unavailable"
        ) from error

    # Preserve privacy-specific failure codes even though the self-contained
    # V2 schema is now strict enough to reject the same payloads structurally.
    # This pass only classifies obvious content-bearing shapes; the complete
    # schema and recursive checks below remain authoritative.
    safe_transfer_types_precheck = {
        "application/json",
        "application/pdf",
        "application/xml",
        "application/zip",
        "files",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/svg+xml",
        "image/webp",
        "other",
        "text",
        "text/csv",
        "text/html",
        "text/plain",
        "text/rtf",
        "text/uri-list",
    }
    events_precheck = trace.get("events") if isinstance(trace, dict) else None
    if isinstance(events_precheck, list):
        for event in events_precheck:
            if not isinstance(event, dict):
                continue
            if (
                event.get("key_class") in {"letter", "digit", "punctuation", "whitespace", "other"}
                and event.get("key") is not None
            ):
                raise AdapterError(
                    "recorder_raw_key_leak", "printable key content was retained in safe mode"
                )
            exact_payload_keys = {
                "input": {"input_type", "data_length", "is_composing"},
                "clipboard": {"types", "item_count", "file_count"},
                "transfer": {"types", "item_count", "file_count", "effect_allowed", "drop_effect"},
                "touch": {"active_count", "changed_count"},
                "form": {"method", "action", "control_count", "has_submitter", "submitter_tag"},
                "document_state": {"fullscreen", "online"},
            }
            for field_name, allowed_keys in exact_payload_keys.items():
                payload = event.get(field_name)
                if payload is not None and (
                    not isinstance(payload, dict) or set(payload) != allowed_keys
                ):
                    raise AdapterError(
                        "recorder_nested_content_leak",
                        f"{field_name} contains fields outside the content-free contract",
                    )
            for field_name in ("clipboard", "transfer"):
                payload = event.get(field_name)
                if not isinstance(payload, dict):
                    continue
                types = payload.get("types")
                if (
                    not isinstance(types, list)
                    or any(
                        not isinstance(item, str) or item not in safe_transfer_types_precheck
                        for item in types
                    )
                    or (
                        field_name == "transfer"
                        and any(
                            value is not None and not isinstance(value, str)
                            for value in (payload.get("effect_allowed"), payload.get("drop_effect"))
                        )
                    )
                ):
                    raise AdapterError(
                        "recorder_nested_content_leak",
                        f"{field_name} contains content-bearing nested values",
                    )
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
    except ImportError as error:
        raise AdapterError(
            "recorder_schema_validator_missing", "jsonschema 4.26.0 is required"
        ) from error
    legacy_schema_id = legacy_schema.get("$id")
    if not isinstance(legacy_schema_id, str) or not legacy_schema_id:
        raise AdapterError(
            "recorder_schema_invalid", "local V1 compatibility schema has no stable $id"
        )
    registry = Registry().with_resource(
        legacy_schema_id,
        Resource.from_contents(legacy_schema),
    )
    validator = Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
        registry=registry,
    )
    errors = sorted(
        validator.iter_errors(trace), key=lambda row: tuple(str(item) for item in row.path)
    )
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.path) or "root"
        raise AdapterError(
            "recorder_schema_invalid", f"cui-trace-v2 violation at {location}: {first.message}"
        )

    metadata = trace["metadata"]
    summary = trace["summary"]
    events = trace["events"]
    if metadata.get("session_id") != expected_session_id:
        raise AdapterError(
            "recorder_trace_session_mismatch", "trace session ID differs from recorder RPC"
        )
    if metadata.get("status") != "complete" or metadata.get("stopped_at") is None:
        raise AdapterError("recorder_trace_incomplete", "trace is not a stopped complete session")
    if metadata.get("raw_key_capture") is not False or metadata.get("include_query") is not False:
        raise AdapterError(
            "recorder_privacy_mode_invalid", "formal traces require safe keys and query stripping"
        )
    if metadata.get("source_class") != "cui_agent" or metadata.get("actor_type") != "agent":
        raise AdapterError(
            "recorder_actor_provenance_invalid", "formal trace actor/source provenance changed"
        )
    if (
        metadata.get("schema_version") != "cui-trace-v2"
        or metadata.get("timing_model") != "session-global-monotonic-v1"
    ):
        raise AdapterError(
            "recorder_timing_contract_invalid", "formal trace is not a TraceEnvelopeV2 session"
        )
    counts = (
        metadata.get("event_count"),
        summary.get("event_count"),
        len(events),
        expected_event_count,
    )
    if len(set(counts)) != 1:
        raise AdapterError(
            "recorder_event_count_mismatch",
            f"metadata/summary/events/RPC counts disagree: {counts}",
        )
    if not events:
        raise AdapterError("recorder_no_events", "formal trace contains no observed event")

    event_ids = [event["event_id"] for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise AdapterError(
            "recorder_duplicate_event_id", "formal trace contains duplicate event IDs"
        )
    # Raw wall/document clocks are diagnostic only.  Independent iframe
    # batches can arrive out of wall-clock or frame-local monotonic order even
    # when the background has serialized them correctly.  TraceEnvelopeV2's
    # event_seq and t_session_ms are the authoritative cross-frame timeline.
    for event in events:
        for timestamp_field in ("epoch_ms", "monotonic_ms"):
            value = event.get(timestamp_field)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise AdapterError(
                    "recorder_timestamp_invalid",
                    f"{timestamp_field} is not a finite diagnostic timestamp",
                )
    event_sequences = [event["event_seq"] for event in events]
    session_times = [event["t_session_ms"] for event in events]
    document_epochs = [event["document_epoch"] for event in events]
    if event_sequences != list(range(1, len(events) + 1)) or metadata.get("event_seq_last") != len(
        events
    ):
        raise AdapterError(
            "recorder_event_sequence_invalid", "TraceEnvelopeV2 event_seq is not continuous"
        )
    if any(right <= left for left, right in zip(session_times, session_times[1:])):
        raise AdapterError(
            "recorder_session_timeline_invalid",
            "TraceEnvelopeV2 session time is not strictly increasing",
        )
    if document_epochs != sorted(document_epochs):
        raise AdapterError(
            "recorder_session_timeline_invalid",
            "TraceEnvelopeV2 session time or document epoch regressed",
        )
    expected_epoch_count = max(document_epochs, default=-1) + 1
    if metadata.get("document_epoch_count") != expected_epoch_count:
        raise AdapterError(
            "recorder_document_epoch_invalid",
            "TraceEnvelopeV2 document epoch count is inconsistent",
        )
    reliability_counters: dict[str, int] = {}
    for counter in (
        "dropped_event_count",
        "rejected_event_count",
        "flush_failure_count",
        "quota_rejection_count",
    ):
        value = metadata.get(counter)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 100_000:
            raise AdapterError(
                "recorder_reliability_counter_invalid", f"{counter} is not a nonnegative integer"
            )
        reliability_counters[counter] = value
    capture_lossless = all(value == 0 for value in reliability_counters.values())

    expected_origin = f"{urlparse(expected_url).scheme}://{urlparse(expected_url).netloc}"
    expected_path = urlparse(expected_url).path
    if metadata.get("scope_origin") != expected_origin:
        raise AdapterError(
            "recorder_scope_origin_mismatch", "trace origin differs from official task origin"
        )
    printable_classes = {"letter", "digit", "punctuation", "whitespace", "other"}
    natural_count = 0
    aligned_natural_count = 0
    observed_step_ids: set[str] = set()
    safe_transfer_types = {
        "application/json",
        "application/pdf",
        "application/xml",
        "application/zip",
        "files",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/svg+xml",
        "image/webp",
        "other",
        "text",
        "text/csv",
        "text/html",
        "text/plain",
        "text/rtf",
        "text/uri-list",
    }

    def exact_object(
        value: Any,
        *,
        field: str,
        keys: set[str],
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != keys:
            raise AdapterError(
                "recorder_nested_content_leak",
                f"{field} must contain exactly the recorder's content-free metadata fields",
            )
        return value

    def bounded_integer(value: Any, *, field: str, minimum: int, maximum: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise AdapterError(
                "recorder_nested_content_leak",
                f"{field} is not a bounded structural count",
            )

    def safe_token_or_null(value: Any, *, field: str, maximum: int) -> None:
        if value is None:
            return
        if (
            not isinstance(value, str)
            or len(value) > maximum
            or re.fullmatch(r"[A-Za-z0-9_.:-]*", value) is None
        ):
            raise AdapterError(
                "recorder_nested_content_leak",
                f"{field} is not a bounded metadata token",
            )

    def validate_content_free_payloads(event: dict[str, Any]) -> None:
        input_payload = exact_object(
            event.get("input"),
            field="input",
            keys={"input_type", "data_length", "is_composing"},
        )
        if input_payload is not None:
            safe_token_or_null(input_payload["input_type"], field="input.input_type", maximum=40)
            bounded_integer(
                input_payload["data_length"],
                field="input.data_length",
                minimum=0,
                maximum=1_000_000,
            )
            if not isinstance(input_payload["is_composing"], bool):
                raise AdapterError(
                    "recorder_nested_content_leak", "input.is_composing must be boolean metadata"
                )

        for payload_field in ("clipboard", "transfer"):
            keys = {"types", "item_count", "file_count"}
            if payload_field == "transfer":
                keys |= {"effect_allowed", "drop_effect"}
            payload = exact_object(event.get(payload_field), field=payload_field, keys=keys)
            if payload is None:
                continue
            types = payload["types"]
            if (
                not isinstance(types, list)
                or len(types) > 16
                or any(
                    not isinstance(item, str) or item not in safe_transfer_types for item in types
                )
                or len(types) != len(set(types))
            ):
                raise AdapterError(
                    "recorder_nested_content_leak",
                    f"{payload_field}.types contains content rather than a normalized MIME category",
                )
            bounded_integer(
                payload["item_count"], field=f"{payload_field}.item_count", minimum=0, maximum=1_000
            )
            bounded_integer(
                payload["file_count"], field=f"{payload_field}.file_count", minimum=0, maximum=1_000
            )
            if payload_field == "transfer":
                effect_allowed = payload["effect_allowed"]
                drop_effect = payload["drop_effect"]
                if (
                    effect_allowed is not None
                    and (
                        not isinstance(effect_allowed, str)
                        or effect_allowed
                        not in {
                            "all",
                            "copy",
                            "copylink",
                            "copymove",
                            "link",
                            "linkmove",
                            "move",
                            "none",
                            "uninitialized",
                        }
                    )
                ) or (
                    drop_effect is not None
                    and (
                        not isinstance(drop_effect, str)
                        or drop_effect not in {"copy", "link", "move", "none"}
                    )
                ):
                    raise AdapterError(
                        "recorder_nested_content_leak",
                        "transfer effects are not normalized metadata enums",
                    )

        touch = exact_object(
            event.get("touch"),
            field="touch",
            keys={"active_count", "changed_count"},
        )
        if touch is not None:
            bounded_integer(
                touch["active_count"], field="touch.active_count", minimum=0, maximum=100
            )
            bounded_integer(
                touch["changed_count"], field="touch.changed_count", minimum=0, maximum=100
            )

        form = exact_object(
            event.get("form"),
            field="form",
            keys={"method", "action", "control_count", "has_submitter", "submitter_tag"},
        )
        if form is not None:
            method = form["method"]
            if method is not None and (
                not isinstance(method, str) or method not in {"dialog", "get", "post"}
            ):
                raise AdapterError(
                    "recorder_nested_content_leak", "form.method is not a normalized enum"
                )
            action = form["action"]
            if not isinstance(action, str):
                raise AdapterError(
                    "recorder_nested_content_leak", "form.action is not a sanitized URL"
                )
            parsed_action = urlparse(action)
            if action != "unavailable" and (
                parsed_action.scheme not in {"http", "https"}
                or not parsed_action.netloc
                or parsed_action.query
                or parsed_action.fragment
                or parsed_action.username is not None
                or parsed_action.password is not None
            ):
                raise AdapterError(
                    "recorder_nested_content_leak", "form.action retained unsafe URL content"
                )
            bounded_integer(
                form["control_count"], field="form.control_count", minimum=0, maximum=100_000
            )
            if not isinstance(form["has_submitter"], bool):
                raise AdapterError(
                    "recorder_nested_content_leak", "form.has_submitter must be boolean metadata"
                )
            safe_token_or_null(form["submitter_tag"], field="form.submitter_tag", maximum=24)

        document_state = exact_object(
            event.get("document_state"),
            field="document_state",
            keys={"fullscreen", "online"},
        )
        if document_state is not None and any(
            value is not None and not isinstance(value, bool) for value in document_state.values()
        ):
            raise AdapterError(
                "recorder_nested_content_leak",
                "document_state contains non-boolean page content",
            )

    for event in events:
        parsed_url = urlparse(event["url"])
        if (
            f"{parsed_url.scheme}://{parsed_url.netloc}" != expected_origin
            or parsed_url.path != expected_path
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise AdapterError(
                "recorder_page_scope_invalid", "event URL escaped the official query-free task page"
            )
        viewport = event["viewport"]
        if (
            viewport.get("width") != expected_viewport["width"]
            or viewport.get("height") != expected_viewport["height"]
        ):
            raise AdapterError(
                "recorder_viewport_mismatch",
                "event viewport differs from observed guarded viewport",
            )
        event_dpr = viewport.get("device_pixel_ratio")
        if (
            not isinstance(event_dpr, (int, float))
            or isinstance(event_dpr, bool)
            or abs(float(event_dpr) - 1.0) > 1e-6
        ):
            raise AdapterError(
                "recorder_device_scale_mismatch", "formal event DPR must be exactly 1"
            )
        if event.get("key_class") in printable_classes and event.get("key") is not None:
            raise AdapterError(
                "recorder_raw_key_leak", "printable key content was retained in safe mode"
            )
        if event.get("target", {}).get("password_like") and event.get("key") not in {
            None,
            "<redacted>",
        }:
            raise AdapterError(
                "recorder_password_key_leak", "password-like input retained key content"
            )
        # The JSON schema deliberately keeps these optional event payloads
        # extensible for backward compatibility. Formal evidence is narrower:
        # accept only the recorder's exact, recursively content-free shapes.
        validate_content_free_payloads(event)
        step_id = event.get("step_id")
        if step_id is not None:
            if not isinstance(step_id, str) or STEP_ID_PATTERN.fullmatch(step_id) is None:
                raise AdapterError(
                    "recorder_step_alignment_invalid",
                    "formal trace contains a non-canonical step token",
                )
            observed_step_ids.add(step_id)
        if event.get("type") != "navigation" and event.get("is_trusted") is True:
            natural_count += 1
            if step_id is not None:
                aligned_natural_count += 1
    if require_natural_event and natural_count < 1:
        raise AdapterError(
            "recorder_no_natural_agent_event",
            "formal trace requires at least one trusted non-recorder browser event",
        )
    actual_by_type = dict(sorted(Counter(event["type"] for event in events).items()))
    if dict(sorted(summary.get("by_type", {}).items())) != actual_by_type:
        raise AdapterError(
            "recorder_summary_mismatch", "summary.by_type disagrees with retained events"
        )
    return {
        "schema_valid": True,
        "event_count_consistent": True,
        "event_ids_unique": True,
        "timestamps_monotonic": True,
        "privacy_safe": True,
        "page_scope_valid": True,
        "natural_non_recorder_event_count": natural_count,
        "natural_event_requirement_met": natural_count > 0,
        "aligned_natural_event_count": aligned_natural_count,
        "unaligned_natural_event_count": natural_count - aligned_natural_count,
        "step_id_count": len(observed_step_ids),
        "step_id_format_valid": True,
        "capture_lossless": capture_lossless,
        "reliability_counters": reliability_counters,
        "event_type_count": len(actual_by_type),
    }


def validate_png_screenshot(path: Path, expected_viewport: dict[str, int]) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise AdapterError(
            "final_screenshot_unreadable", f"{type(error).__name__}: screenshot unavailable"
        ) from error
    if len(payload) > 32 * 1024 * 1024:
        raise AdapterError(
            "final_screenshot_invalid", "final screenshot exceeds the bounded PNG size"
        )
    if len(payload) < 8 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise AdapterError(
            "final_screenshot_invalid", "final screenshot has no valid PNG signature"
        )

    offset = 8
    chunk_count = 0
    ihdr: bytes | None = None
    idat_parts: list[bytes] = []
    saw_iend = False
    saw_idat = False
    idat_closed = False
    saw_palette = False
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise AdapterError(
                "final_screenshot_structure_invalid", "PNG ends inside a chunk header"
            )
        length = int.from_bytes(payload[offset : offset + 4], "big")
        chunk_type = payload[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if length > 32 * 1024 * 1024 or crc_end > len(payload):
            raise AdapterError(
                "final_screenshot_structure_invalid", "PNG chunk length exceeds the file"
            )
        if len(chunk_type) != 4 or not all(
            65 <= value <= 90 or 97 <= value <= 122 for value in chunk_type
        ):
            raise AdapterError("final_screenshot_structure_invalid", "PNG chunk type is invalid")
        chunk_data = payload[data_start:data_end]
        expected_crc = int.from_bytes(payload[data_end:crc_end], "big")
        actual_crc = zlib.crc32(chunk_data, zlib.crc32(chunk_type)) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise AdapterError("final_screenshot_crc_invalid", "PNG chunk CRC verification failed")
        chunk_count += 1
        if chunk_count == 1 and chunk_type != b"IHDR":
            raise AdapterError(
                "final_screenshot_structure_invalid", "PNG IHDR is not the first chunk"
            )
        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13:
                raise AdapterError("final_screenshot_structure_invalid", "PNG has an invalid IHDR")
            ihdr = chunk_data
        elif chunk_type == b"PLTE":
            if saw_idat or not 0 < length <= 768 or length % 3:
                raise AdapterError(
                    "final_screenshot_structure_invalid", "PNG palette chunk is invalid"
                )
            saw_palette = True
        elif chunk_type == b"IDAT":
            if ihdr is None or idat_closed:
                raise AdapterError(
                    "final_screenshot_structure_invalid", "PNG IDAT chunks are out of order"
                )
            saw_idat = True
            idat_parts.append(chunk_data)
        elif chunk_type == b"IEND":
            if length != 0 or not saw_idat:
                raise AdapterError(
                    "final_screenshot_structure_invalid",
                    "PNG IEND is invalid or precedes image data",
                )
            saw_iend = True
            offset = crc_end
            if offset != len(payload):
                raise AdapterError(
                    "final_screenshot_structure_invalid", "PNG contains bytes after IEND"
                )
            break
        else:
            if chunk_type[0] & 0x20 == 0:
                raise AdapterError(
                    "final_screenshot_structure_invalid", "PNG contains an unknown critical chunk"
                )
            if saw_idat:
                idat_closed = True
        offset = crc_end

    if ihdr is None or not saw_idat or not saw_iend:
        raise AdapterError(
            "final_screenshot_structure_invalid", "PNG is missing IHDR, IDAT, or IEND"
        )
    width = int.from_bytes(ihdr[0:4], "big")
    height = int.from_bytes(ihdr[4:8], "big")
    if {"width": width, "height": height} != expected_viewport:
        raise AdapterError(
            "final_screenshot_viewport_mismatch",
            f"PNG dimensions {width}x{height} differ from guarded viewport",
        )
    bit_depth, color_type, compression, filter_method, interlace = ihdr[8:13]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if (
        width < 1
        or height < 1
        or bit_depth != 8
        or channels is None
        or compression != 0
        or filter_method != 0
        or interlace != 0
        or (color_type == 3 and not saw_palette)
    ):
        raise AdapterError(
            "final_screenshot_decode_invalid",
            "PNG encoding is outside the verified non-interlaced 8-bit screenshot contract",
        )
    row_bytes = width * channels
    expected_decoded_bytes = height * (row_bytes + 1)
    decoder = zlib.decompressobj()
    try:
        decoded = decoder.decompress(b"".join(idat_parts), expected_decoded_bytes + 1)
        if len(decoded) > expected_decoded_bytes or decoder.unconsumed_tail:
            raise ValueError("decoded image exceeds expected scanline size")
        decoded += decoder.flush(expected_decoded_bytes + 1 - len(decoded))
    except (zlib.error, ValueError) as error:
        raise AdapterError(
            "final_screenshot_decode_invalid", "PNG image data cannot be decoded"
        ) from error
    if (
        len(decoded) != expected_decoded_bytes
        or not decoder.eof
        or decoder.unused_data
        or any(decoded[row * (row_bytes + 1)] > 4 for row in range(height))
    ):
        raise AdapterError(
            "final_screenshot_decode_invalid", "PNG scanline stream is incomplete or invalid"
        )
    return {
        "png_header_valid": True,
        "png_complete": True,
        "chunk_crc_valid": True,
        "image_data_decoded": True,
        "chunk_count": chunk_count,
        "width": width,
        "height": height,
    }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
