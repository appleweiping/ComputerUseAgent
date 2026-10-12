"""Shared, literature-local helpers for the frozen 68-item corpus.

This module intentionally has no dependency on the repository-wide CLI or contracts.

Layout (relative to ``research/literature``):

* ``sources/``   hand-curated inputs;
* ``snapshots/`` retrieval manifests written by the network collectors;
* ``generated/`` deterministic, hash-bound builder outputs;
* ``generated/large/`` the two large builder outputs, never committed, fetched and
  verified by ``python -m research.literature.fetch_large`` (git-ignored);
* ``archive/``   superseded historical files.

Generated files record their inputs with the source repository's flat layout
(``literature/<file>``). Those strings are part of the hash-bound bytes, so
:func:`recorded_path` reproduces them and :func:`resolve_recorded_path` maps them back.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any


LITERATURE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = LITERATURE_DIR.parents[1]

SOURCES_DIR = LITERATURE_DIR / "sources"
SNAPSHOTS_DIR = LITERATURE_DIR / "snapshots"
GENERATED_DIR = LITERATURE_DIR / "generated"
LARGE_DIR = GENERATED_DIR / "large"
ARCHIVE_DIR = LITERATURE_DIR / "archive"
LARGE_ARTIFACTS_PATH = GENERATED_DIR / "LARGE_ARTIFACTS.json"
LARGE_ARTIFACTS_SCHEMA = "cua-large-artifacts-v1"
FETCH_LARGE_COMMAND = "python -m research.literature.fetch_large"

#: Prefix that generated artifacts use for literature files (source-repository layout).
LEGACY_LITERATURE_PREFIX = "literature"
#: Directories whose files were flat under ``literature/`` in the source repository.
_FLAT_LEGACY_DIRS = (SOURCES_DIR, SNAPSHOTS_DIR, GENERATED_DIR, LARGE_DIR, ARCHIVE_DIR)

#: Recorded prefix of private, never-redistributed raw payloads (full texts, provider
#: responses). It resolves against ``CUA_PRIVATE_ROOT`` when that is set, otherwise
#: against the git-ignored ``private_data/`` directory at the repository root.
PRIVATE_DATA_PREFIX = "private_data"
PRIVATE_ROOT_ENV = "CUA_PRIVATE_ROOT"

BASE_PATH = SOURCES_DIR / "literature_survey_upsert.json"
FOUNDATIONAL_PATH = SOURCES_DIR / "foundational_expansion_sources.json"
LATEST_PATH = SOURCES_DIR / "latest_agent_detection_source.json"
ADDENDUM_PATH = SOURCES_DIR / "literature_high_priority_deep_read_addendum.json"

EXPECTED_TOTAL = 68
EXPECTED_PAPERS = 64
EXPECTED_REPOSITORIES = 4

ACCESS_BLOCKED_DOIS = frozenset(
    {
        "10.1145/581271.581272",
        "10.1145/1085126.1085129",
        "10.1109/tdsc.2007.70207",
        "10.1109/tifs.2012.2223677",
    }
)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def recorded_path(path: Path) -> str:
    """Return the path string that generated artifacts record for a literature file.

    Data files under ``sources/``, ``snapshots/``, ``generated/`` (including
    ``generated/large/``) and ``archive/`` were flat under ``literature/`` in the
    source repository; everything else keeps its path below ``literature/``.
    """

    resolved = path.resolve()
    if resolved.parent in {directory.resolve() for directory in _FLAT_LEGACY_DIRS}:
        return f"{LEGACY_LITERATURE_PREFIX}/{resolved.name}"
    relative = resolved.relative_to(LITERATURE_DIR).as_posix()
    return f"{LEGACY_LITERATURE_PREFIX}/{relative}"


def resolve_recorded_path(recorded: str) -> Path:
    """Map a recorded ``literature/...`` path back to its location in this layout."""

    parts = PurePosixPath(recorded).parts
    if (
        len(parts) < 2
        or parts[0] != LEGACY_LITERATURE_PREFIX
        or PurePosixPath(recorded).is_absolute()
        or ".." in parts
    ):
        raise ValueError(f"Not a recorded literature path: {recorded!r}")
    if len(parts) == 2:
        for directory in _FLAT_LEGACY_DIRS:
            candidate = directory / parts[1]
            if candidate.is_file():
                return candidate
        raise FileNotFoundError(f"No file in the current layout for {recorded!r}")
    return LITERATURE_DIR.joinpath(*parts[1:])


def private_data_root() -> Path:
    """Directory that stands in for the recorded ``private_data/`` prefix."""

    configured = os.environ.get(PRIVATE_ROOT_ENV, "").strip()
    if not configured:
        return REPO_ROOT / PRIVATE_DATA_PREFIX
    root = Path(configured).expanduser().resolve()
    if root == REPO_ROOT or REPO_ROOT in root.parents:
        raise ValueError(f"{PRIVATE_ROOT_ENV} must resolve outside the repository: {root}")
    return root


def resolve_private_path(recorded: str) -> Path:
    """Resolve a recorded ``private_data/...`` path against :func:`private_data_root`."""

    path = PurePosixPath(recorded)
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(path.parts) < 2
        or path.parts[0] != PRIVATE_DATA_PREFIX
    ):
        raise ValueError(f"Raw evidence path is not private and relative: {recorded}")
    return private_data_root().joinpath(*path.parts[1:])


def record_private_path(path: Path) -> str:
    """Return the recorded ``private_data/...`` form of a file under the private root."""

    relative = path.resolve().relative_to(private_data_root().resolve())
    return PurePosixPath(PRIVATE_DATA_PREFIX, *relative.parts).as_posix()


def display_path(path: Path) -> str:
    """Repository-relative POSIX path when possible, for messages."""

    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


class LargeArtifactError(ValueError):
    """A large artifact is missing, has the wrong size, or has the wrong SHA-256."""


def load_large_artifacts(path: Path = LARGE_ARTIFACTS_PATH) -> dict[str, dict[str, Any]]:
    """Load and validate ``LARGE_ARTIFACTS.json``; return the specs keyed by file name."""

    payload = read_json(path)
    if payload.get("schemaVersion") != LARGE_ARTIFACTS_SCHEMA:
        raise ValueError(f"{path} is not a {LARGE_ARTIFACTS_SCHEMA} document")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError(f"{path} lists no artifacts")
    specs: dict[str, dict[str, Any]] = {}
    for spec in artifacts:
        if not isinstance(spec, dict):
            raise ValueError(f"{path} has a non-object artifact entry")
        name = spec.get("name")
        if (
            not isinstance(name, str)
            or not name
            or name != PurePosixPath(name).name
            or name.startswith(".")
        ):
            raise ValueError(f"{path} has an invalid artifact name: {name!r}")
        size = spec.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError(f"{path}: {name} has an invalid byte count")
        if re.fullmatch(r"[0-9a-f]{64}", str(spec.get("sha256", ""))) is None:
            raise ValueError(f"{path}: {name} has an invalid sha256")
        url = spec.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError(f"{path}: {name} has no https download URL")
        if name in specs:
            raise ValueError(f"{path} lists {name} twice")
        specs[name] = dict(spec)
    return specs


def verify_large_artifact(path: Path, spec: Mapping[str, Any]) -> None:
    """Raise :class:`LargeArtifactError` unless ``path`` has the recorded size and hash."""

    if not path.is_file():
        raise LargeArtifactError(
            f"Large artifact {path.name} is missing from {display_path(path.parent)}; "
            f"run `{FETCH_LARGE_COMMAND}`"
        )
    size = path.stat().st_size
    if size != spec["bytes"]:
        raise LargeArtifactError(
            f"Large artifact {display_path(path)} has {size} bytes, expected {spec['bytes']}"
        )
    actual = sha256_file(path)
    if actual != spec["sha256"]:
        raise LargeArtifactError(
            f"Large artifact {display_path(path)} has sha256 {actual}, "
            f"expected {spec['sha256']}"
        )


def large_artifacts_present(directory: Path = LARGE_DIR) -> bool:
    """Return True when every recorded large artifact exists (not yet verified)."""

    return all((directory / name).is_file() for name in load_large_artifacts())


def normalize_doi(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
    return text.strip().lower()


def normalize_persistent_id(value: str) -> str:
    text = value.strip()
    if text.lower().startswith("doi:"):
        return f"DOI:{normalize_doi(text)}"
    if text.lower().startswith("arxiv:"):
        return f"arXiv:{text.split(':', 1)[1].strip()}"
    if text.lower().startswith("github:"):
        return f"GitHub:{text.split(':', 1)[1].strip().lower()}"
    if text.lower().startswith("acl:"):
        return f"ACL:{text.split(':', 1)[1].strip().upper()}"
    if text.lower().startswith("pmlr:"):
        return f"PMLR:{text.split(':', 1)[1].strip().lower()}"
    return text


def doi_from_persistent_id(value: str) -> str | None:
    normalized = normalize_persistent_id(value)
    if not normalized.startswith("DOI:"):
        return None
    return normalized.split(":", 1)[1]


def is_repository(row: dict[str, Any]) -> bool:
    return normalize_persistent_id(str(row["Persistent ID"])).startswith("GitHub:")


def _source_rows(payload: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"Expected rows[] in {path}")
    return [dict(row) for row in rows]


def load_corpus_rows() -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Merge 43 + 24 + 1 sources and apply the prior reviewed addendum.

    The source files remain untouched. Persistent IDs are normalized only in the
    derived canonical corpus so case-only differences cannot evade deduplication.
    """

    base = read_json(BASE_PATH)
    foundational = read_json(FOUNDATIONAL_PATH)
    latest = read_json(LATEST_PATH)
    addendum = read_json(ADDENDUM_PATH)

    columns = base.get("columns")
    if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
        raise ValueError("Base corpus has no valid columns list")
    if foundational.get("columns") != columns or latest.get("columns") != columns:
        raise ValueError("All literature sources must use the same 41-column schema")

    rows_with_origin: list[tuple[dict[str, Any], str]] = []
    rows_with_origin.extend((row, "existing-43") for row in _source_rows(base, BASE_PATH))
    rows_with_origin.extend(
        (row, "foundational-24")
        for row in _source_rows(foundational, FOUNDATIONAL_PATH)
    )
    latest_row = latest.get("row")
    if not isinstance(latest_row, dict):
        raise ValueError(f"Expected row object in {LATEST_PATH}")
    rows_with_origin.append((dict(latest_row), "arxiv-2607.26935"))

    updates = {
        normalize_persistent_id(str(update["Persistent ID"])): update
        for update in addendum.get("canonicalRowUpdates", [])
    }
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    origins: dict[str, str] = {}
    for raw, origin in rows_with_origin:
        missing = set(columns) - set(raw)
        extra = set(raw) - set(columns)
        if missing or extra:
            raise ValueError(
                f"Schema mismatch for {raw.get('Persistent ID')}: "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )
        persistent_id = normalize_persistent_id(str(raw["Persistent ID"]))
        if persistent_id in seen:
            raise ValueError(f"Duplicate normalized Persistent ID: {persistent_id}")
        seen.add(persistent_id)
        row = {column: raw[column] for column in columns}
        row["Persistent ID"] = persistent_id
        if persistent_id in updates:
            for field, value in updates[persistent_id].items():
                if field != "Persistent ID":
                    if field not in columns:
                        raise ValueError(f"Addendum update uses unknown column {field}")
                    row[field] = value
        row["Corpus Origin"] = origin
        merged.append(row)
        origins[persistent_id] = origin

    papers = [row for row in merged if not is_repository(row)]
    repositories = [row for row in merged if is_repository(row)]
    if len(merged) != EXPECTED_TOTAL:
        raise ValueError(f"Expected {EXPECTED_TOTAL} corpus rows, found {len(merged)}")
    if len(papers) != EXPECTED_PAPERS or len(repositories) != EXPECTED_REPOSITORIES:
        raise ValueError(
            f"Expected {EXPECTED_PAPERS} papers and {EXPECTED_REPOSITORIES} repositories; "
            f"found {len(papers)} and {len(repositories)}"
        )
    blocked_found = {
        doi_from_persistent_id(str(row["Persistent ID"]))
        for row in papers
        if doi_from_persistent_id(str(row["Persistent ID"])) in ACCESS_BLOCKED_DOIS
    }
    if blocked_found != ACCESS_BLOCKED_DOIS:
        raise ValueError(
            f"Blocked DOI set mismatch: expected {sorted(ACCESS_BLOCKED_DOIS)}, "
            f"found {sorted(blocked_found)}"
        )

    provenance = {
        "inputs": [
            {
                "path": recorded_path(path),
                "sha256": sha256_file(path),
            }
            for path in (BASE_PATH, FOUNDATIONAL_PATH, LATEST_PATH, ADDENDUM_PATH)
        ],
        "originCounts": {
            "existing-43": sum(1 for value in origins.values() if value == "existing-43"),
            "foundational-24": sum(
                1 for value in origins.values() if value == "foundational-24"
            ),
            "arxiv-2607.26935": sum(
                1 for value in origins.values() if value == "arxiv-2607.26935"
            ),
        },
    }
    return list(columns), merged, provenance


def slug_for_id(persistent_id: str) -> str:
    normalized = normalize_persistent_id(persistent_id)
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
