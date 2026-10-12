"""Locate frozen Week 1 protocol paths in this checkout.

The benchmark manifest and the archived evidence record repository-relative
paths in the source repository's layout (``recorder``,
``experiments/week1/...``).  Those bytes are never rewritten: the manifest
hash identifies the protocol and the evidence is archival.  This module maps
the recorded paths onto this checkout when a file has to be found, checks the
live extension against the frozen recorder pin, and keeps new output out of
``research/evidence``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .protocol import ProtocolError

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = REPO_ROOT / "research" / "evidence"
LEGACY_EXACT_PATHS = {
    "recorder": "extension/app",
    "experiments/week1": "research/week1_miniwob",
}
LEGACY_PATH_PREFIXES = (("experiments/week1/", "research/week1_miniwob/"),)


def translate_legacy_path(recorded: str) -> str:
    """Map a recorded repository-relative path onto this checkout.

    Paths that are not legacy are returned unchanged (with ``/`` separators).
    Absolute paths and ``..`` segments are rejected because recorded paths are
    always repository-relative.
    """

    value = recorded.replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ProtocolError(f"recorded path must be repository-relative: {recorded!r}")
    if ".." in value.split("/"):
        raise ProtocolError(f"recorded path must not contain '..': {recorded!r}")
    if value in LEGACY_EXACT_PATHS:
        return LEGACY_EXACT_PATHS[value]
    for legacy, current in LEGACY_PATH_PREFIXES:
        if value.startswith(legacy):
            return current + value[len(legacy) :]
    return value


def require_output_outside_evidence(output_dir: Path) -> Path:
    """Refuse to write new attempt artifacts into the archived evidence tree."""

    resolved = output_dir.resolve()
    evidence = EVIDENCE_ROOT.resolve()
    if resolved == evidence or evidence in resolved.parents:
        raise ProtocolError(
            "--output-dir must not be inside research/evidence; archived evidence is read-only"
        )
    return resolved


def recorder_extension_check(manifest: dict[str, Any]) -> dict[str, str]:
    """Compare the checkout's extension with the frozen recorder pin (0.4.0)."""

    def row(status: str, detail: str) -> dict[str, str]:
        return {"id": "recorder-extension", "status": status, "detail": detail}

    try:
        relative = translate_legacy_path(manifest["recorder"]["extension_path"])
    except ProtocolError as error:
        return row("fail", str(error))
    expected = manifest["recorder"]["minimum_version"]
    try:
        payload = json.loads((REPO_ROOT / relative / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return row("fail", f"{relative}/manifest.json: {type(error).__name__}")
    version = payload.get("version") if isinstance(payload, dict) else None
    if version != expected:
        return row(
            "fail",
            f"{relative} is version {version}; this frozen harness requires recorder {expected}",
        )
    return row("pass", f"version {version}")
