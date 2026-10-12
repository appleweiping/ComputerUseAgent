"""Content-free publication transforms for framework action evidence."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .adapter_contract import SAFE_ACTION_TYPES


def safe_public_artifacts(value: Any, *, key: str = "") -> Any:
    """Keep structural framework evidence while dropping paths and free text."""
    lowered = key.lower()
    if any(
        token in lowered
        for token in ("path", "url", "message", "reason", "prompt", "text", "instruction")
    ):
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
        return [safe_public_artifacts(item, key=key) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if not isinstance(child_key, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,80}", child_key
            ):
                continue
            safe_value = safe_public_artifacts(child_value, key=child_key)
            if safe_value is not None:
                result[child_key] = safe_value
        return result
    return None


def compact_actions(actions: Any, limit: int = 30) -> list[dict[str, Any]]:
    """Keep only action types and field names; applying this twice is a no-op."""
    if not isinstance(actions, list):
        return []
    safe_action_types = SAFE_ACTION_TYPES | {"type_text"}
    canonical_fields = {"index", "type", "fields"}

    def field_names(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return sorted(
            {
                field
                for field in value
                if isinstance(field, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", field)
            }
        )[:12]

    compact: list[dict[str, Any]] = []
    for index, action in enumerate(actions[:limit], start=1):
        safe = _json_safe(action)
        if not isinstance(safe, dict):
            safe = {"value_type": type(action).__name__}

        # Canonical rows may pass through another publication boundary.  Keep
        # their representation stable instead of treating the wrapper keys as
        # a second action schema.
        if set(safe).issubset(canonical_fields) and isinstance(safe.get("fields"), list):
            row: dict[str, Any] = {"index": index}
            existing_type = safe.get("type")
            if isinstance(existing_type, str) and existing_type in safe_action_types:
                row["type"] = existing_type
            row["fields"] = field_names(safe["fields"])
            compact.append(row)
            continue

        row: dict[str, Any] = {"index": index}
        declared_types = [safe.get(key) for key in ("type", "name", "method")]
        normalized_types = [
            str(value).strip().lower().replace("-", "_").replace(" ", "_")
            for value in declared_types
            if isinstance(value, str)
        ]
        action_type = next(
            (value for value in normalized_types if value in safe_action_types), None
        )
        if action_type is None:
            key_types = [
                key.lower()
                for key in safe
                if isinstance(key, str)
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key)
                and key.lower() in safe_action_types
            ]
            if len(key_types) == 1:
                action_type = key_types[0]
        if action_type:
            row["type"] = action_type
        structural_fields = sorted(
            key
            for key in safe
            if isinstance(key, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key)
            and key
            not in {
                "reasoning",
                "pageText",
                "messages",
                "description",
                "error",
                "action",
                "instruction",
            }
        )
        row["fields"] = structural_fields[:12]
        compact.append(row)
    return compact


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, default=str))
