"""Privacy-preserving projection of private adapter evidence into public records."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .protocol import FORMAL_EVIDENCE_SCOPE


def _public_artifact_descriptor(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    artifact_id = value.get("artifact_id")
    sha256 = value.get("sha256")
    byte_count = value.get("bytes")
    if (
        not isinstance(artifact_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", artifact_id)
        or value.get("retained_private") is not True
        or not isinstance(byte_count, int)
        or byte_count < 0
        or not isinstance(sha256, str)
        or not re.fullmatch(r"[a-f0-9]{64}", sha256)
    ):
        return None
    return {
        "artifact_id": artifact_id,
        "retained_private": True,
        "bytes": byte_count,
        "sha256": sha256,
    }


def _public_dependencies(value: Any) -> dict[str, list[dict[str, Any]]]:
    snapshots = value.get("snapshots") if isinstance(value, dict) else None
    public_snapshots: list[dict[str, Any]] = []
    if isinstance(snapshots, list):
        for row in snapshots:
            if not isinstance(row, dict):
                continue
            environment = row.get("environment")
            artifact = _public_artifact_descriptor(row.get("artifact"))
            if (
                isinstance(environment, str)
                and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", environment)
                and artifact is not None
            ):
                public_snapshots.append({"environment": environment, "artifact": artifact})
    return {"snapshots": public_snapshots}


def _public_trace_validation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    boolean_fields = (
        "schema_valid",
        "event_count_consistent",
        "event_ids_unique",
        "timestamps_monotonic",
        "privacy_safe",
        "page_scope_valid",
    )
    natural_count = value.get("natural_non_recorder_event_count")
    event_type_count = value.get("event_type_count")
    if (
        any(not isinstance(value.get(key), bool) for key in boolean_fields)
        or not isinstance(natural_count, int)
        or isinstance(natural_count, bool)
        or natural_count < 0
        or not isinstance(event_type_count, int)
        or isinstance(event_type_count, bool)
        or event_type_count < 0
    ):
        return None
    return {
        **{key: value[key] for key in boolean_fields},
        "natural_non_recorder_event_count": natural_count,
        "event_type_count": event_type_count,
    }


def _public_viewport(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    width, height = value.get("width"), value.get("height")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or width < 1
        or not isinstance(height, int)
        or isinstance(height, bool)
        or height < 1
    ):
        return None
    return {"width": width, "height": height}


def _public_screenshot_validation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    width, height = value.get("width"), value.get("height")
    boolean_fields = (
        "png_header_valid",
        "png_complete",
        "chunk_crc_valid",
        "image_data_decoded",
    )
    chunk_count = value.get("chunk_count")
    if (
        any(not isinstance(value.get(key), bool) for key in boolean_fields)
        or not isinstance(chunk_count, int)
        or isinstance(chunk_count, bool)
        or chunk_count < 0
        or not isinstance(width, int)
        or isinstance(width, bool)
        or width < 1
        or not isinstance(height, int)
        or isinstance(height, bool)
        or height < 1
    ):
        return None
    return {
        **{key: value[key] for key in boolean_fields},
        "chunk_count": chunk_count,
        "width": width,
        "height": height,
    }


def _stable_environment_token(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_+:/.-]{1,80}", value):
        return value
    return None


def _public_environment_guard(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    switch_count = value.get("capture_stability_switch_count")
    return {
        "requested_locale": _stable_environment_token(value.get("requested_locale")),
        "requested_timezone": _stable_environment_token(value.get("requested_timezone")),
        "observed_locale": _stable_environment_token(value.get("observed_locale")),
        "observed_timezone": _stable_environment_token(value.get("observed_timezone")),
        "capture_stability_verified": value.get("capture_stability_verified") is True,
        "capture_stability_switch_count": (
            switch_count
            if isinstance(switch_count, int)
            and not isinstance(switch_count, bool)
            and 0 <= switch_count <= 8
            else None
        ),
        "native_window_occlusion_disabled": value.get("native_window_occlusion_disabled") is True,
        "verified": value.get("verified") is True,
    }


def _public_episode_guard(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    boolean_fields = (
        "stable",
        "sticky_lifecycle_monitoring",
        "pre_validator_attested",
        "post_validator_attested",
        "clean_disarm_attested",
        "target_id_unchanged",
        "loader_id_unchanged",
        "document_token_unchanged",
        "time_origin_unchanged",
        "episode_start_unchanged",
    )
    violation_count = value.get("violation_count")
    attestation_count = value.get("attestation_count")
    return {
        **{key: value.get(key) is True for key in boolean_fields},
        "attestation_count": (
            attestation_count
            if isinstance(attestation_count, int)
            and not isinstance(attestation_count, bool)
            and attestation_count >= 0
            else None
        ),
        "violation_count": (
            violation_count
            if isinstance(violation_count, int) and not isinstance(violation_count, bool) and violation_count >= 0
            else None
        ),
    }


def _public_framework_evidence(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if any(token in lowered for token in ("path", "url", "message", "reason", "prompt", "text", "instruction")):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if lowered.endswith(("status", "type")) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
            return value
        if "checksum" in lowered and re.fullmatch(r"[A-Fa-f0-9]{16,128}", value):
            return value.lower()
        if lowered.endswith("id"):
            return f"ref-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"
        return None
    if isinstance(value, list):
        return [_public_framework_evidence(item, key=key) for item in value]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if not isinstance(child_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", child_key):
                continue
            safe = _public_framework_evidence(child_value, key=child_key)
            if safe is not None:
                cleaned[child_key] = safe
        return cleaned
    return None


def public_adapter_result(
    result: dict[str, Any], *, evidence_scope: str = FORMAL_EVIDENCE_SCOPE
) -> dict[str, Any]:
    """Whitelist the adapter evidence safe to retain in version-controlled JSON."""
    agent_source = result.get("agent") if isinstance(result.get("agent"), dict) else {}
    action_rows: list[dict[str, Any]] = []
    raw_actions = agent_source.get("action_history")
    if isinstance(raw_actions, list):
        for index, raw_action in enumerate(raw_actions[:30], start=1):
            if not isinstance(raw_action, dict):
                continue
            row: dict[str, Any] = {"index": index}
            action_type = raw_action.get("type")
            if isinstance(action_type, str) and re.fullmatch(r"[a-z0-9_.:-]{1,64}", action_type):
                row["type"] = action_type
            for key in ("success", "taskCompleted", "timeMs", "timestamp_ms", "duration_ms"):
                value = raw_action.get(key)
                if isinstance(value, bool) or (isinstance(value, (int, float)) and not isinstance(value, bool)):
                    row[key] = value
            fields = raw_action.get("fields")
            if isinstance(fields, list):
                row["fields"] = [
                    field for field in fields
                    if isinstance(field, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", field)
                ][:12]
            action_rows.append(row)

    validator_source = result.get("validator") if isinstance(result.get("validator"), dict) else {}
    task_setup_artifact = _public_artifact_descriptor(validator_source.get("task_setup_artifact"))
    task_info_artifact = _public_artifact_descriptor(validator_source.get("task_info_artifact"))
    recorder_source = result.get("recorder") if isinstance(result.get("recorder"), dict) else {}
    browser_source = result.get("browser") if isinstance(result.get("browser"), dict) else {}
    artifact_source = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
    error_source = result.get("error")
    error_code = error_source.get("code") if isinstance(error_source, dict) else None
    error_status = result.get("status") in {"error", "timeout"}
    if (
        not error_status
        or not isinstance(error_code, str)
        or not re.fullmatch(r"[a-z0-9_:-]{1,80}", error_code)
    ):
        error_code = "framework_error" if error_status else None
    secondary_error_codes: list[str] = []
    raw_secondary_codes = (
        error_source.get("secondary_codes") if isinstance(error_source, dict) else None
    )
    if error_status and isinstance(raw_secondary_codes, list):
        for value in raw_secondary_codes:
            if (
                isinstance(value, str)
                and re.fullmatch(r"[a-z0-9_:-]{1,80}", value)
                and value != error_code
                and value not in secondary_error_codes
            ):
                secondary_error_codes.append(value)
            if len(secondary_error_codes) == 16:
                break
    session_id = recorder_source.get("session_id")
    if isinstance(session_id, str) and session_id:
        session_id = f"session-ref-{hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:16]}"
    else:
        session_id = None
    recorder_status = recorder_source.get("status")
    if recorder_status not in {"complete", "error", "interrupted", "unknown"}:
        recorder_status = "unknown"
    product_version = browser_source.get("product_version")
    if not isinstance(product_version, str) or not re.fullmatch(
        r"(?:Chrome|Chromium|HeadlessChrome)/[0-9.]{3,40}", product_version
    ):
        product_version = None
    event_count = recorder_source.get("event_count")
    if not isinstance(event_count, int) or isinstance(event_count, bool) or event_count < 0:
        event_count = 0
    device_pixel_ratio = browser_source.get("device_pixel_ratio")
    if not isinstance(device_pixel_ratio, (int, float)) or isinstance(device_pixel_ratio, bool):
        device_pixel_ratio = None

    public = {
        "schema_version": result.get("schema_version"),
        "attempt_id": result.get("attempt_id"),
        "status": result.get("status"),
        "agent": {
            "framework": agent_source.get("framework"),
            "framework_version": agent_source.get("framework_version"),
            "model": agent_source.get("model"),
            "action_count": agent_source.get("action_count"),
            "action_count_observed": agent_source.get("action_count_observed") is True,
            "action_count_censored": agent_source.get("action_count_censored") is True,
            "action_history": action_rows,
            "framework_completed": agent_source.get("framework_completed") is True,
        },
        "validator": {
            "source": validator_source.get("source"),
            "reward": validator_source.get("reward"),
            "terminated": validator_source.get("terminated") is True,
            "truncated": validator_source.get("truncated") is True,
            "task_setup_artifact": task_setup_artifact,
            "task_setup_retained_private": task_setup_artifact is not None,
            "task_info_artifact": task_info_artifact,
            "task_info_retained_private": task_info_artifact is not None,
        },
        "recorder": {
            "capture_adapter": recorder_source.get("capture_adapter"),
            "source_class": recorder_source.get("source_class"),
            "status": recorder_status,
            "session_id": session_id,
            "event_count": event_count,
            "trace_artifact": _public_artifact_descriptor(recorder_source.get("trace_artifact")),
            "artifact_validation": _public_trace_validation(recorder_source.get("artifact_validation")),
        },
        "browser": {
            "product_version": product_version,
            "viewport": _public_viewport(browser_source.get("viewport")),
            "device_pixel_ratio": device_pixel_ratio,
            "locale": _stable_environment_token(browser_source.get("locale")),
            "timezone": _stable_environment_token(browser_source.get("timezone")),
            "environment_guard": _public_environment_guard(browser_source.get("environment_guard")),
            "episode_guard": _public_episode_guard(browser_source.get("episode_guard")),
            "fresh_profile": browser_source.get("fresh_profile") is True,
        },
        "artifacts": {
            "final_screenshot": _public_artifact_descriptor(artifact_source.get("final_screenshot")),
            "final_screenshot_validation": _public_screenshot_validation(
                artifact_source.get("final_screenshot_validation")
            ),
            "framework": _public_framework_evidence(artifact_source.get("framework", {})),
        },
        "dependencies": _public_dependencies(result.get("dependencies")),
        "error": (
            {"code": error_code, "secondary_codes": secondary_error_codes}
            if error_code
            else None
        ),
        "provenance_complete": result.get("provenance_complete") is True,
        "artifact_bundle_complete": result.get("artifact_bundle_complete") is True,
        # Partial/debug evidence is intentionally publishable, but never
        # eligible for formal-matrix or analysis claims even when an adapter
        # returns optimistic flags intended for the full protocol.
        "formal_matrix_eligible": (
            evidence_scope == FORMAL_EVIDENCE_SCOPE
            and result.get("formal_matrix_eligible") is True
        ),
        "analysis_eligible": (
            evidence_scope == FORMAL_EVIDENCE_SCOPE
            and result.get("analysis_eligible") is True
        ),
    }
    return public
