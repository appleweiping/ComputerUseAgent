"""Fail-closed validators for publication-safe CUI JSON contracts.

JSON Schema owns structure.  This module adds repository-wide safety rules
that JSON Schema cannot express conveniently: no secret-shaped material, no
machine-absolute paths, UTC timestamps, unique identifiers, and semantic
cross-field checks.  It never resolves locators or writes an artifact.

Schemas are read from the installed package through :mod:`importlib.resources`
(see :mod:`computer_use_agent.schemas`). The legacy ``cui-detector-dataset-v1``
format is accepted for reading only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType
from typing import Any

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ModuleNotFoundError as error:  # pragma: no cover - dependency gate
    raise RuntimeError("jsonschema>=4.23 is required for public-contract validation") from error

from computer_use_agent.schemas import SCHEMA_FILES, load_schema, schema_resource

CONTRACT_SCHEMAS = (
    "cui-source-manifest-v1",
    "cui-detector-dataset-v2",
    "cui-citation-assertion-v1",
    "cui-codex-case-v1",
)
#: Contract schema identifier -> packaged schema resource.
SCHEMA_PATHS: Mapping[str, Traversable] = MappingProxyType(
    {name: schema_resource(name) for name in CONTRACT_SCHEMAS}
)
LEGACY_READ_ONLY_SCHEMAS = frozenset({"cui-detector-dataset-v1"})
TRACE_SCHEMAS = frozenset({"cui-trace-v1", "cui-trace-v2", "x11-trace-v2"})
DETECTOR_DATASET_SCHEMAS = frozenset({"cui-detector-dataset-v1", "cui-detector-dataset-v2"})
#: Every identifier accepted by :func:`validate_file` and the ``validate`` CLI.
VALIDATABLE_SCHEMAS = tuple(sorted({*SCHEMA_FILES, *LEGACY_READ_ONLY_SCHEMAS}))

SECRET_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|"
    r"credential|authorization|cookie)(?:$|[_-])"
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)[?&](?:api[_-]?key|access[_-]?token|token|secret|password)="),
)
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
SAFETY_ASSERTION_KEYS = frozenset({"secrets_included"})


class ContractValidationError(ValueError):
    """Raised when a public artifact violates its declared closed contract."""


def _walk(value: object, path: str = "$") -> Iterator[tuple[str, object]]:
    yield path, value
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _safety_scan(payload: Mapping[str, Any]) -> None:
    for path, value in _walk(payload):
        if isinstance(value, Mapping):
            for key in value:
                if (
                    isinstance(key, str)
                    and key not in SAFETY_ASSERTION_KEYS
                    and SECRET_KEY.search(key)
                ):
                    raise ContractValidationError(f"secret-like key is forbidden at {path}.{key}")
        if not isinstance(value, str):
            continue
        if WINDOWS_ABSOLUTE.match(value) or value.startswith(("\\\\", "file:", "/")):
            raise ContractValidationError(f"absolute path is forbidden at {path}")
        if any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS):
            raise ContractValidationError(f"secret-like value is forbidden at {path}")


def _load_schema(schema_version: str) -> dict[str, Any]:
    if schema_version not in SCHEMA_FILES:
        raise ContractValidationError(f"unsupported public schema: {schema_version}")
    try:
        schema = load_schema(schema_version)
    except (OSError, ValueError) as error:
        raise ContractValidationError(f"cannot load schema {schema_version}: {error}") from error
    Draft202012Validator.check_schema(schema)
    return schema


def _json_location(path: Sequence[object]) -> str:
    return "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in path)


def _schema_check(payload: object, schema: Mapping[str, Any], *, label: str) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        error = errors[0]
        raise ContractValidationError(
            f"{label} validation failed at {_json_location(list(error.path))}: {error.message}"
        )


def _validate_legacy_detector_v1(payload: Mapping[str, Any]) -> None:
    """Delegate exact legacy semantics without making v1 a write target."""

    if not {"schema_version", "records"}.issubset(payload) or not set(payload).issubset(
        {"schema_version", "records", "audit", "provenance"}
    ):
        raise ContractValidationError("legacy detector dataset root is not closed")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ContractValidationError("legacy detector dataset records must be an array")
    try:
        from computer_use_agent.detector.contract import DATASET_SCHEMA_V1, validate_dataset

        audit = validate_dataset(records, dataset_schema=DATASET_SCHEMA_V1)
    except ValueError as error:
        raise ContractValidationError(str(error)) from error
    if "audit" in payload and payload["audit"] != audit:
        raise ContractValidationError("legacy detector dataset audit differs from revalidation")


def _parse_utc(value: str, *, field: str) -> datetime:
    if UTC_TIMESTAMP.fullmatch(value) is None:
        raise ContractValidationError(f"{field} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ContractValidationError(f"{field} is not a valid UTC timestamp") from error


def _codex_case_checks(payload: Mapping[str, Any]) -> None:
    started = _parse_utc(payload["started_at_utc"], field="started_at_utc")
    completed = _parse_utc(payload["completed_at_utc"], field="completed_at_utc")
    _parse_utc(payload["actor"]["observed_at_utc"], field="actor.observed_at_utc")
    if completed < started:
        raise ContractValidationError("completed_at_utc precedes started_at_utc")
    if payload["action_count"] > payload["limits"]["max_actions"]:
        raise ContractValidationError("action_count exceeds limits.max_actions")

    recorder = payload["recorder"]
    recorder_descriptor = (
        recorder["session_id"],
        recorder["record_sha256"],
        recorder["event_count"],
    )
    if recorder["status"] == "captured":
        if any(value is None for value in recorder_descriptor):
            raise ContractValidationError(
                "captured recorder requires session_id, record_sha256, and event_count"
            )
        if recorder["event_count"] < 1:
            raise ContractValidationError("captured recorder event_count must be positive")
        if recorder["event_count"] < payload["action_count"]:
            raise ContractValidationError(
                "captured recorder event_count cannot be lower than action_count"
            )
    elif any(value is not None for value in recorder_descriptor):
        raise ContractValidationError(
            "unavailable recorder must use null session_id, record_sha256, and event_count"
        )

    validator = payload["validator"]
    outcome = payload["outcome"]
    status = outcome["status"]
    reward = validator["reward"]
    failure_code = outcome["failure_code"]
    if status == "success":
        if reward is None or reward <= 0 or not validator["terminated"] or validator["truncated"]:
            raise ContractValidationError(
                "successful outcome requires positive official reward, terminated=true, "
                "and truncated=false"
            )
        if failure_code is not None:
            raise ContractValidationError("successful outcome must have null failure_code")
    else:
        if failure_code is None:
            raise ContractValidationError("non-success outcome requires failure_code")
        if status == "failure" and (reward != 0 or not validator["terminated"]):
            raise ContractValidationError(
                "failure outcome requires zero official reward and terminated=true"
            )
        if status == "timeout" and not validator["truncated"]:
            raise ContractValidationError("timeout outcome requires truncated=true")
        if status in {"error", "blocked"} and reward is not None:
            raise ContractValidationError(
                f"{status} outcome requires unavailable (null) official reward"
            )

    artifact_ids = [artifact["artifact_id"] for artifact in payload["artifacts"]]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ContractValidationError("Codex case contains duplicate artifact_id values")
    check_ids = [check["check_id"] for check in payload["verification"]]
    if len(check_ids) != len(set(check_ids)):
        raise ContractValidationError("Codex case contains duplicate check_id values")
    screenshot_paths = [shot["repository_path"] for shot in payload["screenshots"]]
    if len(screenshot_paths) != len(set(screenshot_paths)):
        raise ContractValidationError("Codex case contains duplicate screenshot paths")


def _detector_dataset_v2_checks(payload: Mapping[str, Any]) -> None:
    """Record integrity plus the stored-audit recomputation ``analyze`` applies.

    Duplicate records are always rejected. A stored ``audit`` is recomputed
    with the runtime gate (as for v1) because a dataset only carries one after
    passing it; the leave-one-task-out size gate itself (>= 3 tasks, both
    classes per task) is enforced by ``analyze``, not by this contract.
    Provenance-bound datasets are checked by their registered stage validator
    at analysis time.
    """

    records = payload["records"]
    refs = [row["record_ref"] for row in records]
    hashes = [row["source_sha256"] for row in records]
    if len(refs) != len(set(refs)) or len(hashes) != len(set(hashes)):
        raise ContractValidationError(
            "detector dataset contains duplicate record_ref or source_sha256 values"
        )
    if payload.get("provenance") is not None:
        return
    if any(row.get("evidence_class") == "formal_trace" for row in records):
        raise ContractValidationError(
            "Formal detector datasets require stage-specific immutable-ledger provenance"
        )
    if "audit" not in payload:
        return
    from computer_use_agent.detector.contract import DATASET_SCHEMA_V2, validate_dataset

    try:
        audit = validate_dataset(records, dataset_schema=DATASET_SCHEMA_V2)
    except ValueError as error:
        raise ContractValidationError(str(error)) from error
    if payload["audit"] != audit:
        raise ContractValidationError("detector dataset audit differs from revalidation")


def _semantic_checks(payload: Mapping[str, Any], schema_version: str) -> None:
    if schema_version == "cui-source-manifest-v1":
        source_ids = [source["source_id"] for source in payload["sources"]]
        if len(source_ids) != len(set(source_ids)):
            raise ContractValidationError("source manifest contains duplicate source_id values")
    elif schema_version == "cui-citation-assertion-v1":
        if payload["citing_work"]["canonical_id"] == payload["cited_work"]["canonical_id"]:
            raise ContractValidationError("citation assertion cannot contain a self-edge")
        provider = payload["provider"]
        is_xpac = payload["observation"]["is_xpac"]
        if provider != "openalex" and is_xpac is not None:
            raise ContractValidationError("is_xpac must be null for non-OpenAlex assertions")
    elif schema_version == "cui-codex-case-v1":
        _codex_case_checks(payload)
    elif schema_version == "cui-detector-dataset-v2":
        _detector_dataset_v2_checks(payload)


def validate_contract(
    payload: Mapping[str, Any],
    *,
    schema_version: str | None = None,
    for_write: bool = False,
) -> None:
    """Validate one object; v1 detector data are accepted only for reading."""

    if not isinstance(payload, Mapping):
        raise ContractValidationError("contract payload must be one JSON object")
    declared = payload.get("schema_version")
    selected = schema_version or declared
    if not isinstance(selected, str) or not selected:
        raise ContractValidationError("schema_version is required")
    if declared != selected:
        raise ContractValidationError(
            f"declared schema_version {declared!r} does not match requested {selected!r}"
        )
    if selected not in {*CONTRACT_SCHEMAS, *LEGACY_READ_ONLY_SCHEMAS}:
        raise ContractValidationError(f"unsupported public schema: {selected}")
    _safety_scan(payload)
    if selected in LEGACY_READ_ONLY_SCHEMAS:
        if for_write:
            raise ContractValidationError(
                "cui-detector-dataset-v1 is read-only; new datasets must use v2"
            )
        _validate_legacy_detector_v1(payload)
        return
    _schema_check(payload, _load_schema(selected), label=selected)
    _semantic_checks(payload, selected)


def validate_contract_records(
    records: list[Mapping[str, Any]],
    *,
    schema_version: str,
    for_write: bool = False,
) -> None:
    """Validate a batch and reject duplicate edge/case identities across files."""

    if not records:
        raise ContractValidationError("contract record batch must not be empty")
    for record in records:
        validate_contract(record, schema_version=schema_version, for_write=for_write)
    identity_field = {
        "cui-citation-assertion-v1": "assertion_id",
        "cui-codex-case-v1": "case_id",
    }.get(schema_version)
    if identity_field is None:
        return
    identities = [record[identity_field] for record in records]
    if len(identities) != len(set(identities)):
        raise ContractValidationError(f"contract batch contains duplicate {identity_field} values")


def load_json_records(path: Path) -> list[Mapping[str, Any]]:
    """Load one JSON object/list or newline-delimited JSON objects."""

    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        records: list[Mapping[str, Any]] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ContractValidationError(f"cannot read JSONL artifact {path}: {error}") from error
        # Split on LF only (read_text already turned CRLF and lone CR into LF),
        # like load_trace's line iteration: str.splitlines() would also break
        # inside JSON strings at U+2028, U+2029, U+0085 and U+001C..U+001E.
        for line_number, line in enumerate(text.split("\n"), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ContractValidationError(
                    f"invalid JSONL line {line_number}: {error}"
                ) from error
            if not isinstance(record, Mapping):
                raise ContractValidationError(f"JSONL line {line_number} is not an object")
            records.append(record)
        if not records:
            raise ContractValidationError("JSONL artifact contains no objects")
        return records
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractValidationError(f"cannot read JSON artifact {path}: {error}") from error
    if isinstance(payload, Mapping):
        return [payload]
    if isinstance(payload, list) and payload and all(isinstance(item, Mapping) for item in payload):
        return payload
    raise ContractValidationError("JSON artifact must contain an object or non-empty object array")


def summarize_contract_records(
    records: Sequence[Mapping[str, Any]], *, schema_version: str
) -> dict[str, Any]:
    """Count validated objects and, for detector datasets, the records inside them."""

    object_count = len(records)
    record_count = object_count
    if schema_version in DETECTOR_DATASET_SCHEMAS:
        record_count = 0
        for payload in records:
            rows = payload.get("records")
            record_count += len(rows) if isinstance(rows, list) else 0
    return {
        "schema_version": schema_version,
        "object_count": object_count,
        "record_count": record_count,
        "validation_status": "pass",
        "writes_performed": False,
    }


def _browser_jsonl_payload(path: Path) -> dict[str, Any]:
    metadata: object = None
    summary: object = None
    events: list[object] = []
    for record in load_json_records(path):
        kind = record.get("record_type")
        if kind == "session":
            metadata = record.get("metadata", record.get("session"))
            summary = record.get("summary")
        elif kind == "event":
            events.append({key: value for key, value in record.items() if key != "record_type"})
    return {"metadata": metadata, "summary": summary, "events": events}


def validate_trace_file(path: Path, *, schema_version: str) -> dict[str, Any]:
    """Validate a browser or X11 trace file against a packaged trace schema.

    JSON Schema validation is followed by the dependency-free runtime checks
    in :mod:`computer_use_agent.trace` (privacy rules and stream integrity),
    so a file passes only when both agree. The trace is never migrated or
    rewritten.
    """

    from computer_use_agent.trace.io import load_trace

    if schema_version not in TRACE_SCHEMAS:
        raise ContractValidationError(f"unsupported trace schema: {schema_version}")
    path = Path(path)
    schema = _load_schema(schema_version)
    suffix = path.suffix.lower()
    if schema_version == "x11-trace-v2":
        if suffix != ".jsonl":
            raise ContractValidationError("x11-trace-v2 traces are append-only .jsonl files")
        lines = load_json_records(path)
        for index, line in enumerate(lines, 1):
            _schema_check(line, schema, label=f"{schema_version} line {index}")
        declared = next(
            (
                line["metadata"].get("schema_version")
                for line in lines
                if line.get("record_type") == "session"
                and isinstance(line.get("metadata"), Mapping)
            ),
            None,
        )
        if declared != "cui-trace-v2":
            raise ContractValidationError("x11-trace-v2 session metadata must declare cui-trace-v2")
    else:
        if suffix == ".json":
            try:
                payload: object = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ContractValidationError(f"cannot read trace {path}: {error}") from error
        elif suffix == ".jsonl":
            payload = _browser_jsonl_payload(path)
        else:
            raise ContractValidationError(f"{schema_version} traces are .json or .jsonl exports")
        _schema_check(payload, schema, label=schema_version)
        metadata = payload.get("metadata") if isinstance(payload, Mapping) else None
        declared = metadata.get("schema_version") if isinstance(metadata, Mapping) else None
        if declared != schema_version:
            raise ContractValidationError(
                f"trace declares schema_version {declared!r}, not {schema_version!r}"
            )
    try:
        _, events, _ = load_trace(path, migrate_to_v2=False)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ContractValidationError(
            f"{schema_version} runtime validation failed: {error}"
        ) from error
    return {
        "schema_version": schema_version,
        "object_count": 1,
        "record_count": len(events),
        "validation_status": "pass",
        "writes_performed": False,
    }


def validate_file(path: Path, *, schema_version: str, for_write: bool = False) -> dict[str, Any]:
    """Validate one file against any packaged schema and return a count summary.

    Contract files may hold one object, an object array or JSONL objects.
    Detector datasets report the number of records they contain
    (``record_count``) besides the number of dataset objects
    (``object_count``). ``cui-detector-dataset-v1`` is read-only.
    """

    if schema_version in TRACE_SCHEMAS:
        if for_write:
            raise ContractValidationError("--for-write applies only to contract schemas")
        return validate_trace_file(path, schema_version=schema_version)
    if schema_version not in {*CONTRACT_SCHEMAS, *LEGACY_READ_ONLY_SCHEMAS}:
        raise ContractValidationError(f"unsupported public schema: {schema_version}")
    records = load_json_records(path)
    validate_contract_records(records, schema_version=schema_version, for_write=for_write)
    return summarize_contract_records(records, schema_version=schema_version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, choices=VALIDATABLE_SCHEMAS)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--for-write", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = validate_file(args.input, schema_version=args.schema, for_write=args.for_write)
    except (ContractValidationError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
