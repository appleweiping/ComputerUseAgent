"""Verify the archived research evidence: ``MANIFEST.sha256`` and ``INDEX.json``.

The evidence under ``research/evidence/`` and the generated literature files under
``research/literature/generated/`` are archival bytes. They are checked only against
``research/evidence/MANIFEST.sha256`` (``sha256sum`` format), never against current
source code. ``research/literature/generated/large/`` holds the optional large
literature downloads and is excluded.

``research/evidence/INDEX.json`` lists the headline claims. Every claim names a file
and an RFC 6901 JSON pointer whose value, after an optional derivation, must equal the
claim's ``value``. Claims stated only in prose carry ``json_pointer: null`` and a
``note``; when they also carry ``text``, that text must occur in the file.

``research/evidence/LEGACY_PATHS.json`` maps the ``results/...`` paths recorded inside
the archived evidence to their current locations. Every target must exist, and every
``results/...`` path recorded in an evidence JSON file must translate to an existing
path.

The manifest lists one ``<sha256>  <path>`` line per file, sorted by code point (the
order of ``LC_ALL=C sort``), with LF line endings and repository-relative paths.

Standard library only. Usage::

    python tools/ci/verify_evidence.py            # same as --check
    python tools/ci/verify_evidence.py --check    # verify manifest, index and legacy map
    python tools/ci/verify_evidence.py --write    # regenerate the manifest, then verify

With GNU coreutils the manifest can also be checked from the repository root with
``sha256sum -c research/evidence/MANIFEST.sha256``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

MANIFEST_PATH = "research/evidence/MANIFEST.sha256"
INDEX_PATH = "research/evidence/INDEX.json"
LEGACY_PATHS_PATH = "research/evidence/LEGACY_PATHS.json"
COVERED_ROOTS: tuple[str, ...] = ("research/evidence", "research/literature/generated")
EXCLUDED_PREFIXES: tuple[str, ...] = ("research/literature/generated/large/",)
EXCLUDED_FILES: frozenset[str] = frozenset({MANIFEST_PATH})

INDEX_SCHEMA_VERSION = "computer-use-agent-evidence-index-v1"
LEGACY_SCHEMA_VERSION = "computer-use-agent-legacy-paths-v1"

REQUIRED_CLAIM_KEYS = frozenset({"id", "claim", "file", "json_pointer", "value", "caveat"})
OPTIONAL_CLAIM_KEYS = frozenset({"section", "derive", "note", "display", "text"})
CLAIM_ID = re.compile(r"[a-z0-9][a-z0-9_.-]*")
MANIFEST_LINE = re.compile(r"([0-9a-f]{64}) [ *](.+)")
DERIVE_OPS = frozenset({"length", "sum", "union_length", "difference"})

_MISSING = object()


class EvidenceError(ValueError):
    """A claim, pointer or derivation could not be evaluated."""


# --------------------------------------------------------------- manifest --


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_excluded(relative: str) -> bool:
    return relative in EXCLUDED_FILES or any(relative.startswith(p) for p in EXCLUDED_PREFIXES)


def covered_files(root: Path) -> list[str]:
    """Return the sorted repository-relative POSIX paths the manifest must cover."""

    found: set[str] = set()
    for covered in COVERED_ROOTS:
        base = root / covered
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if not _is_excluded(relative):
                found.add(relative)
    return sorted(found)


def render_manifest(root: Path) -> str:
    """Render the manifest text: one ``<sha256>  <path>`` line per file, sorted, LF."""

    return "".join(f"{sha256_file(root / rel)}  {rel}\n" for rel in covered_files(root))


def write_manifest(root: Path) -> Path:
    target = root / MANIFEST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(render_manifest(root).encode("utf-8"))
    return target


def _valid_relative_path(relative: str) -> bool:
    if not relative or "\\" in relative or relative.startswith("/") or ":" in relative:
        return False
    parts = PurePosixPath(relative).parts
    return all(part not in ("", ".", "..") for part in parts)


def parse_manifest(data: bytes) -> tuple[dict[str, str], list[str]]:
    """Parse manifest bytes into ``{path: sha256}`` plus format problems."""

    problems: list[str] = []
    if b"\r" in data:
        problems.append(f"{MANIFEST_PATH}: contains CR characters; use LF line endings")
    if data and not data.endswith(b"\n"):
        problems.append(f"{MANIFEST_PATH}: does not end with a newline")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {}, [*problems, f"{MANIFEST_PATH}: is not valid UTF-8"]
    entries: dict[str, str] = {}
    order: list[str] = []
    for number, line in enumerate(text.replace("\r", "").split("\n"), start=1):
        if not line:
            continue
        match = MANIFEST_LINE.fullmatch(line)
        if match is None:
            problems.append(f"{MANIFEST_PATH}:{number}: malformed line")
            continue
        digest, relative = match.groups()
        if not _valid_relative_path(relative):
            problems.append(f"{MANIFEST_PATH}:{number}: invalid path {relative!r}")
            continue
        if relative in entries:
            problems.append(f"{MANIFEST_PATH}:{number}: duplicate entry {relative}")
            continue
        entries[relative] = digest
        order.append(relative)
    if order != sorted(order):
        problems.append(f"{MANIFEST_PATH}: entries are not sorted by path")
    return entries, problems


def check_manifest(root: Path) -> list[str]:
    """Check the manifest's format, completeness (both ways) and every hash."""

    target = root / MANIFEST_PATH
    if not target.is_file():
        return [f"{MANIFEST_PATH}: missing; run tools/ci/verify_evidence.py --write"]
    entries, problems = parse_manifest(target.read_bytes())
    actual = set(covered_files(root))
    listed = set(entries)
    for relative in sorted(listed - actual):
        if _is_excluded(relative) or not relative.startswith(tuple(f"{c}/" for c in COVERED_ROOTS)):
            problems.append(f"{MANIFEST_PATH}: lists a path outside the covered set: {relative}")
        else:
            problems.append(f"{MANIFEST_PATH}: lists a missing file: {relative}")
    for relative in sorted(actual - listed):
        problems.append(f"{MANIFEST_PATH}: does not list {relative}")
    for relative in sorted(actual & listed):
        if sha256_file(root / relative) != entries[relative]:
            problems.append(f"{MANIFEST_PATH}: sha256 mismatch for {relative}")
    return problems


# ----------------------------------------------------------- JSON pointer --


def resolve_pointer(document: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 JSON pointer against a parsed JSON document."""

    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise EvidenceError(f"JSON pointer {pointer!r} must be empty or start with '/'")
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise EvidenceError(f"JSON pointer {pointer!r}: key {token!r} not found")
            current = current[token]
        elif isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token) or int(token) >= len(current):
                raise EvidenceError(f"JSON pointer {pointer!r}: index {token!r} out of range")
            current = current[int(token)]
        else:
            raise EvidenceError(f"JSON pointer {pointer!r}: cannot descend into a scalar")
    return current


def _hashable_items(value: Any, pointer: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvidenceError(f"derivation needs an array at {pointer!r}")
    items: list[Any] = []
    for item in value:
        if isinstance(item, (dict, list)):
            raise EvidenceError(f"derivation needs scalar array items at {pointer!r}")
        items.append(item)
    return items


def _pointer_list(spec: Mapping[str, Any], key: str) -> list[str]:
    pointers = spec.get(key)
    if (
        not isinstance(pointers, list)
        or not pointers
        or not all(isinstance(p, str) for p in pointers)
    ):
        raise EvidenceError(f"derivation {spec.get('op')!r} needs a non-empty {key!r} list")
    return list(pointers)


def apply_derivation(document: Any, pointer: str, spec: Mapping[str, Any]) -> Any:
    """Derive a value from the pointer target (see README for the operations)."""

    op = spec.get("op")
    allowed = {"op"} | {"union_length": {"with"}, "difference": {"minus"}}.get(str(op), set())
    if op not in DERIVE_OPS:
        raise EvidenceError(f"unknown derivation {op!r}")
    if set(spec) - allowed:
        raise EvidenceError(f"derivation {op!r} has unknown keys {sorted(set(spec) - allowed)}")
    base = resolve_pointer(document, pointer)
    if op == "length":
        if not isinstance(base, (list, dict, str)):
            raise EvidenceError(f"'length' needs an array, object or string at {pointer!r}")
        return len(base)
    if op == "sum":
        if not isinstance(base, list) or not all(
            isinstance(x, (int, float)) and not isinstance(x, bool) for x in base
        ):
            raise EvidenceError(f"'sum' needs an array of numbers at {pointer!r}")
        return sum(base)
    if op == "union_length":
        union = set(_hashable_items(base, pointer))
        for other in _pointer_list(spec, "with"):
            union |= set(_hashable_items(resolve_pointer(document, other), other))
        return len(union)
    remaining = set(_hashable_items(base, pointer))
    for other in _pointer_list(spec, "minus"):
        remaining -= set(_hashable_items(resolve_pointer(document, other), other))
    return sorted(remaining)


def values_equal(expected: Any, actual: Any) -> bool:
    """JSON equality: numbers compare numerically, booleans and null only to themselves."""

    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return expected == actual
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(
            values_equal(e, a) for e, a in zip(expected, actual, strict=True)
        )
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(
            values_equal(expected[k], actual[k]) for k in expected
        )
    return type(expected) is type(actual) and expected == actual


# ------------------------------------------------------------------ index --


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_claim_shape(claim: Any, position: int) -> list[str]:
    where = f"{INDEX_PATH}: claims[{position}]"
    if not isinstance(claim, dict):
        return [f"{where}: must be an object"]
    problems: list[str] = []
    missing = REQUIRED_CLAIM_KEYS - set(claim)
    unknown = set(claim) - REQUIRED_CLAIM_KEYS - OPTIONAL_CLAIM_KEYS
    if missing:
        problems.append(f"{where}: missing keys {sorted(missing)}")
    if unknown:
        problems.append(f"{where}: unknown keys {sorted(unknown)}")
    claim_id = claim.get("id")
    if not isinstance(claim_id, str) or CLAIM_ID.fullmatch(claim_id) is None:
        problems.append(f"{where}: id must match {CLAIM_ID.pattern}")
    for key in ("claim", "caveat", "file"):
        if key in claim and (not isinstance(claim[key], str) or not claim[key].strip()):
            problems.append(f"{where}: {key!r} must be a non-empty string")
    for key in ("section", "note", "display", "text"):
        if key in claim and (not isinstance(claim[key], str) or not claim[key].strip()):
            problems.append(f"{where}: {key!r} must be a non-empty string when present")
    pointer = claim.get("json_pointer", _MISSING)
    if pointer is not _MISSING and pointer is not None and not isinstance(pointer, str):
        problems.append(f"{where}: json_pointer must be a string or null")
    if pointer is None:
        if "note" not in claim:
            problems.append(f"{where}: a claim with json_pointer null needs a 'note'")
        if "derive" in claim:
            problems.append(f"{where}: 'derive' needs a json_pointer")
    elif "text" in claim:
        problems.append(f"{where}: 'text' is only checked for claims with json_pointer null")
    if "derive" in claim and not isinstance(claim["derive"], dict):
        problems.append(f"{where}: 'derive' must be an object")
    return problems


def check_index(root: Path) -> list[str]:
    """Check INDEX.json: shape, unique ids, and that every pointer yields its value."""

    target = root / INDEX_PATH
    if not target.is_file():
        return [f"{INDEX_PATH}: missing"]
    try:
        index = _load_json(target)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return [f"{INDEX_PATH}: invalid JSON: {error}"]
    if not isinstance(index, dict):
        return [f"{INDEX_PATH}: top level must be an object"]
    problems: list[str] = []
    if index.get("schema_version") != INDEX_SCHEMA_VERSION:
        problems.append(f"{INDEX_PATH}: schema_version must be {INDEX_SCHEMA_VERSION!r}")
    claims = index.get("claims")
    if not isinstance(claims, list) or not claims:
        return [*problems, f"{INDEX_PATH}: 'claims' must be a non-empty array"]

    seen: set[str] = set()
    documents: dict[str, Any] = {}
    for position, claim in enumerate(claims):
        shape = _check_claim_shape(claim, position)
        if shape:
            problems.extend(shape)
            continue
        claim_id = claim["id"]
        where = f"{INDEX_PATH}: {claim_id}"
        if claim_id in seen:
            problems.append(f"{where}: duplicate id")
            continue
        seen.add(claim_id)
        relative = claim["file"]
        if not _valid_relative_path(relative):
            problems.append(f"{where}: file must be a repository-relative POSIX path")
            continue
        path = root / relative
        if not path.is_file():
            problems.append(f"{where}: file not found: {relative}")
            continue
        pointer = claim["json_pointer"]
        if pointer is None:
            text = claim.get("text")
            if text is not None and text not in path.read_text(encoding="utf-8"):
                problems.append(f"{where}: text {text!r} not found in {relative}")
            continue
        if relative not in documents:
            try:
                documents[relative] = _load_json(path)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                problems.append(f"{where}: {relative} is not valid JSON: {error}")
                documents[relative] = _MISSING
                continue
        document = documents[relative]
        if document is _MISSING:
            problems.append(f"{where}: {relative} is not valid JSON")
            continue
        try:
            derive = claim.get("derive")
            actual = (
                apply_derivation(document, pointer, derive)
                if derive is not None
                else resolve_pointer(document, pointer)
            )
        except EvidenceError as error:
            problems.append(f"{where}: {error}")
            continue
        if not values_equal(claim["value"], actual):
            problems.append(
                f"{where}: {relative}#{pointer} is {json.dumps(actual)[:200]}, "
                f"INDEX states {json.dumps(claim['value'])[:200]}"
            )
    return problems


# ------------------------------------------------------------ legacy paths --


def load_legacy_prefixes(root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """Return ``(old, new)`` prefix pairs, longest ``old`` first."""

    data = _load_json(root / LEGACY_PATHS_PATH)
    pairs = [(str(item["old"]), str(item["new"])) for item in data["prefixes"]]
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def translate_legacy_path(
    recorded: str,
    root: Path = REPO_ROOT,
    prefixes: Sequence[tuple[str, str]] | None = None,
) -> str:
    """Translate a ``results/...`` path recorded in archived evidence to its current path.

    Paths that match no legacy prefix are returned unchanged. ``prefixes`` defaults to
    the pairs in ``LEGACY_PATHS.json`` under ``root``.
    """

    pairs = load_legacy_prefixes(root) if prefixes is None else prefixes
    for old, new in pairs:
        if recorded == old or (old.endswith("/") and recorded.startswith(old)):
            return new + recorded[len(old) :]
        if old.endswith("/") and recorded == old[:-1]:
            return new[:-1]
    return recorded


def check_legacy_paths(root: Path) -> list[str]:
    target = root / LEGACY_PATHS_PATH
    if not target.is_file():
        return [f"{LEGACY_PATHS_PATH}: missing"]
    try:
        data = _load_json(target)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return [f"{LEGACY_PATHS_PATH}: invalid JSON: {error}"]
    problems: list[str] = []
    if not isinstance(data, dict) or data.get("schema_version") != LEGACY_SCHEMA_VERSION:
        return [f"{LEGACY_PATHS_PATH}: schema_version must be {LEGACY_SCHEMA_VERSION!r}"]
    prefixes = data.get("prefixes")
    if not isinstance(prefixes, list) or not prefixes:
        return [f"{LEGACY_PATHS_PATH}: 'prefixes' must be a non-empty array"]
    seen: set[str] = set()
    for position, item in enumerate(prefixes):
        where = f"{LEGACY_PATHS_PATH}: prefixes[{position}]"
        if not isinstance(item, dict) or set(item) != {"old", "new"}:
            problems.append(f"{where}: must be an object with exactly 'old' and 'new'")
            continue
        old, new = item["old"], item["new"]
        if not all(isinstance(v, str) and _valid_relative_path(v.rstrip("/")) for v in (old, new)):
            problems.append(f"{where}: 'old' and 'new' must be repository-relative POSIX paths")
            continue
        if old in seen:
            problems.append(f"{where}: duplicate 'old' {old}")
        seen.add(old)
        if old.endswith("/") != new.endswith("/"):
            problems.append(f"{where}: 'old' and 'new' must both be directories or both files")
            continue
        destination = root / new.rstrip("/")
        if not (destination.is_dir() if new.endswith("/") else destination.is_file()):
            problems.append(f"{where}: target does not exist: {new}")
    if problems:
        return problems
    return check_recorded_legacy_paths(root)


RECORDED_LEGACY_PATH = re.compile(r'"(results/[^"\s]+)"')


def check_recorded_legacy_paths(root: Path) -> list[str]:
    """Every ``results/...`` path recorded in evidence JSON must translate to a real path."""

    problems: list[str] = []
    evidence = root / "research/evidence"
    own = {root / INDEX_PATH, root / LEGACY_PATHS_PATH}
    prefixes = load_legacy_prefixes(root)
    for path in sorted(evidence.rglob("*.json")) if evidence.is_dir() else []:
        if path in own or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for recorded in sorted(set(RECORDED_LEGACY_PATH.findall(text))):
            current = translate_legacy_path(recorded, root, prefixes)
            if current == recorded or not (root / current).exists():
                problems.append(
                    f"{path.relative_to(root).as_posix()}: recorded path {recorded} does not "
                    f"resolve through {LEGACY_PATHS_PATH}"
                )
    return problems


# -------------------------------------------------------------------- CLI --


def verify(root: Path) -> list[str]:
    return [*check_manifest(root), *check_index(root), *check_legacy_paths(root)]


def _count_claims(root: Path) -> int:
    try:
        claims = _load_json(root / INDEX_PATH).get("claims", [])
    except (OSError, ValueError, AttributeError):
        return 0
    return len(claims) if isinstance(claims, list) else 0


def _report(problems: Iterable[str], root: Path) -> int:
    problems = list(problems)
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if problems:
        print(f"evidence verification failed: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(
        f"evidence OK: {len(covered_files(root))} files match {MANIFEST_PATH}; "
        f"{_count_claims(root)} INDEX claims resolve"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify only (default)")
    mode.add_argument("--write", action="store_true", help="regenerate the manifest, then verify")
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root (default: this checkout)"
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.write:
        target = write_manifest(root)
        print(f"wrote {target.relative_to(root).as_posix()} ({len(covered_files(root))} files)")
    return _report(verify(root), root)


if __name__ == "__main__":
    raise SystemExit(main())
