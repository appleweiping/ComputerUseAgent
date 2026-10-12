"""Immutable run-ledger primitives shared by formal experiment orchestrators."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEDGER_SCHEMA = "cui-study-ledger-v3"
SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,160}")


class LedgerError(RuntimeError):
    """Raised when immutable evidence or a concurrent claim blocks execution."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _integrity_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, payload: Mapping[str, Any], *, replace: bool) -> None:
    """Write a JSON document through a same-directory durable rename.

    Immutable ledger/evidence files use ``replace=False``.  Derived summaries
    may use ``replace=True`` because they can always be rebuilt from evidence.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, indent=2, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # A hard-link publication is an atomic, no-clobber operation on
            # the same filesystem.  The temporary inode is fully flushed
            # before it becomes reachable by the immutable evidence name.
            try:
                os.link(temporary, path)
            except FileExistsError as error:
                raise LedgerError(f"immutable file already exists: {path}") from error
            except OSError as error:
                raise LedgerError(
                    f"could not atomically publish immutable file {path}: {error}"
                ) from error
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def ledger_payload(
    *,
    stage: str,
    binding: Mapping[str, Any],
    protocol_sha256: str,
    planned_ids: Sequence[str],
) -> dict[str, Any]:
    if not stage or not SAFE_ID.fullmatch(stage):
        raise LedgerError("ledger stage must be a stable token")
    ids = list(planned_ids)
    if not ids or len(ids) != len(set(ids)) or any(SAFE_ID.fullmatch(item) is None for item in ids):
        raise LedgerError("planned IDs must be non-empty, unique stable tokens")
    if not re.fullmatch(r"[a-f0-9]{64}", protocol_sha256):
        raise LedgerError("protocol_sha256 must be 64 lowercase hex characters")
    binding_snapshot = json.loads(canonical_json(dict(binding)))
    if not isinstance(binding_snapshot, dict):
        raise LedgerError("ledger binding must be one JSON object")
    required_binding_fields = {
        "formal_config",
        "formal_config_sha256",
        "release_capsule",
        "release_capsule_sha256",
        "formal_config_snapshot",
        "release_capsule_snapshot",
    }
    if set(binding_snapshot) != required_binding_fields:
        raise LedgerError("ledger binding must embed the exact config and capsule snapshots")
    rendered_binding = canonical_json(binding_snapshot)
    if len(rendered_binding.encode("utf-8")) > 1_000_000 or "sk-" in rendered_binding:
        raise LedgerError("ledger binding snapshot is oversized or contains secret-like material")
    payload = {
        "schema_version": LEDGER_SCHEMA,
        "stage": stage,
        "created_at": utc_now(),
        "binding": binding_snapshot,
        "protocol_sha256": protocol_sha256,
        "planned_count": len(ids),
        "planned_ids": ids,
        "evidence_policy": {
            "immutable_entries": True,
            "validate_before_resume_skip": True,
            "task_success_distinct_from_bundle_completeness": True,
        },
    }
    return {**payload, "integrity_sha256": _integrity_sha256(payload)}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LedgerError(f"cannot read study ledger {path}: {error}") from error
    if not isinstance(value, dict):
        raise LedgerError("study ledger must contain one object")
    return value


def validate_ledger(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    required_fields = {
        "schema_version",
        "stage",
        "created_at",
        "binding",
        "protocol_sha256",
        "planned_count",
        "planned_ids",
        "evidence_policy",
        "integrity_sha256",
    }
    if set(observed) != required_fields:
        raise LedgerError("study ledger does not match the closed immutable schema")
    integrity = observed.get("integrity_sha256")
    content = {key: value for key, value in observed.items() if key != "integrity_sha256"}
    if not isinstance(integrity, str) or not re.fullmatch(r"[a-f0-9]{64}", integrity):
        raise LedgerError("study ledger integrity hash is invalid")
    if _integrity_sha256(content) != integrity:
        raise LedgerError("study ledger integrity mismatch")
    created_at = observed.get("created_at")
    if not isinstance(created_at, str):
        raise LedgerError("study ledger created_at is invalid")
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise LedgerError("study ledger created_at is invalid") from error
    if parsed.tzinfo is None:
        raise LedgerError("study ledger created_at must include a timezone")
    immutable_fields = (
        "schema_version",
        "stage",
        "binding",
        "protocol_sha256",
        "planned_count",
        "planned_ids",
        "evidence_policy",
    )
    mismatched = [field for field in immutable_fields if observed.get(field) != expected.get(field)]
    if mismatched:
        raise LedgerError("study ledger binding mismatch: " + ", ".join(mismatched))


def initialize_or_resume(
    root: Path,
    expected: dict[str, Any],
    *,
    resume: bool,
) -> dict[str, Any]:
    """Initialize a new root or validate the immutable ledger on resume."""

    root = Path(root).resolve()
    ledger_path = root / "study_ledger.json"
    if ledger_path.exists():
        if not resume:
            raise LedgerError(
                f"output already contains a study ledger; use --resume or a new --output-dir: {root}"
            )
        observed = _load_json(ledger_path)
        validate_ledger(observed, expected)
        return observed
    if root.exists():
        material = [item for item in root.iterdir() if item.name != ".claims"]
        if material:
            raise LedgerError(
                "output contains material but no valid study ledger; refusing to adopt or overwrite it"
            )
    root.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write_json(ledger_path, expected, replace=False)
        return expected
    except LedgerError:
        # Another process may have initialized the same root between the
        # existence check and exclusive creation.  Only --resume may join it.
        if not resume:
            raise
        observed = _load_json(ledger_path)
        validate_ledger(observed, expected)
        return observed


@contextmanager
def claim(root: Path, entry_id: str) -> Iterator[Path]:
    """Exclusively claim one ledger cell for the duration of its execution."""

    if SAFE_ID.fullmatch(entry_id) is None:
        raise LedgerError("entry ID is not a safe stable token")
    claim_dir = Path(root).resolve() / ".claims"
    claim_dir.mkdir(parents=True, exist_ok=True)
    path = claim_dir / f"{entry_id}.lock"
    payload = {
        "schema_version": "cui-study-ledger-claim-v1",
        "entry_id": entry_id,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "claimed_at": utc_now(),
    }
    try:
        atomic_write_json(path, payload, replace=False)
    except LedgerError as error:
        raise LedgerError(
            f"ledger cell is already claimed; no concurrent overwrite attempted: {entry_id}"
        ) from error
    try:
        yield path
    finally:
        # Claims are coordination state, not evidence.  A crashed process
        # intentionally leaves its claim for explicit operator inspection.
        with suppress(FileNotFoundError):
            path.unlink()


def active_claims(root: Path) -> list[str]:
    claim_dir = Path(root).resolve() / ".claims"
    if not claim_dir.is_dir():
        return []
    return sorted(path.stem for path in claim_dir.glob("*.lock"))
