"""Build and validate a deterministic CycloneDX SBOM and the isolated Python audit plan.

The project identity (name, version, repository URL) is read from
``pyproject.toml``. Components come from:

* ``uv.lock`` - the product, test, lint and dev graph plus the ``x11``,
  ``research`` and ``notebooks`` extras (official PyPI index only);
* ``extension/package-lock.json`` - the extension's development graph;
* the frozen research manifests under ``research/`` (exact pins or hash
  locks kept beside each harness for provenance).

Each research graph is audited on its own: combining them would be wrong
(for example BrowserGym's host uses Playwright 1.44 while Skyvern's isolated
runtime requires Playwright 1.62).

Usage::

    python tools/ci/supply_chain.py --check
    python tools/ci/supply_chain.py --write [--sbom-output dist/NAME-VERSION.cdx.json]
    python tools/ci/supply_chain.py --print-python-audit-targets

The SBOM is generated at release time and attached to the GitHub Release; it is
not committed. It omits timestamps and serial numbers so two runs over the same
inputs produce identical bytes.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_NAME = "pyproject.toml"
UV_LOCK_NAME = "uv.lock"
OFFICIAL_INDEX_URL = "https://pypi.org/simple"
# Every npm lock entry must resolve from the public npm registry, so a lock
# regenerated behind a local mirror cannot slip into the repository.
OFFICIAL_NPM_REGISTRY = "https://registry.npmjs.org/"
PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)(?:\[(?P<extras>[A-Za-z0-9_,.-]+)\])?"
    r"==(?P<version>[^\s;]+)(?P<options>.*)$"
)
HASH_OPTION_RE = re.compile(r"(?:^|\s)--hash=sha256:(?P<digest>[a-f0-9]{64})(?=\s|$)")
MARKER_RE = re.compile(r"[A-Za-z0-9_.()'\"<>=!\-\s]+")
SPEC_PIN_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*==\s*(?P<version>[^\s;,]+)\s*(?:;.*)?$"
)
# Paths under research/ that are never dependency manifests of a live graph.
EXCLUDED_MANIFEST_PARTS = frozenset({"node_modules", "archive", "evidence", "generated", ".venv"})


class SupplyChainError(ValueError):
    """Raised when a dependency manifest cannot be inventoried safely."""


@dataclass(frozen=True)
class ProjectIdentity:
    name: str
    version: str
    repository: str | None = None

    @property
    def normalized_name(self) -> str:
        return _normalize(self.name)

    @property
    def root_ref(self) -> str:
        return f"pkg:pypi/{self.normalized_name}@{quote(self.version, safe='.-_~')}"


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def project_identity(root: Path = ROOT) -> ProjectIdentity:
    """Read the distribution name, version and repository URL from pyproject.toml."""

    path = root / PYPROJECT_NAME
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SupplyChainError(f"cannot read {path}: {error}") from error
    project = payload.get("project")
    if not isinstance(project, Mapping):
        raise SupplyChainError(f"{path} has no [project] table")
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise SupplyChainError(f"{path} must declare a static project name and version")
    urls = project.get("urls")
    repository = None
    if isinstance(urls, Mapping):
        candidate = urls.get("Repository") or urls.get("Homepage")
        repository = candidate if isinstance(candidate, str) else None
    return ProjectIdentity(name=name, version=version, repository=repository)


def _identity_for(root: Path) -> ProjectIdentity:
    # Fixture roots in tests may omit pyproject.toml; fall back to this repository.
    return project_identity(root if (root / PYPROJECT_NAME).is_file() else ROOT)


try:
    _REPOSITORY_IDENTITY = project_identity(ROOT)
except SupplyChainError:  # pragma: no cover - only when run outside a checkout
    _REPOSITORY_IDENTITY = ProjectIdentity("computer-use-agent", "0.0.0")
PROJECT_NAME = _REPOSITORY_IDENTITY.name
PROJECT_VERSION = _REPOSITORY_IDENTITY.version
ROOT_REF = _REPOSITORY_IDENTITY.root_ref


@dataclass(frozen=True)
class PythonAuditGroupSpec:
    id: str
    mode: str
    requirement_path: str
    direct_requirement_path: str | None
    target_environment: str


# The product graph (uv.lock) is audited with `uv export | pip-audit`; these are
# the frozen research graphs, each audited in isolation.
PYTHON_AUDIT_GROUP_SPECS: tuple[PythonAuditGroupSpec, ...] = (
    PythonAuditGroupSpec(
        "fp-agent",
        "locked",
        "research/fp_agent/requirements.lock",
        "research/fp_agent/requirements.txt",
        "cpython-3.12-cross-platform-markers",
    ),
    PythonAuditGroupSpec(
        "week1-orchestrator",
        "locked",
        "research/week1_miniwob/adapters/requirements-orchestrator.lock",
        "research/week1_miniwob/adapters/requirements-orchestrator.txt",
        "cpython-3.12-cross-platform-markers",
    ),
    PythonAuditGroupSpec(
        "week1-browser-use",
        "resolve",
        "research/week1_miniwob/adapters/requirements-browser-use.txt",
        None,
        "ci-linux-resolution-not-formal-runtime-lock",
    ),
    PythonAuditGroupSpec(
        "week1-skyvern",
        "resolve",
        "research/week1_miniwob/adapters/requirements-skyvern.txt",
        None,
        "ci-linux-resolution-not-formal-runtime-lock",
    ),
    PythonAuditGroupSpec(
        "scienceboard-host",
        "locked",
        "research/scienceboard/requirements-host.lock",
        "research/scienceboard/requirements-host.txt",
        "cpython-3.12-windows",
    ),
    PythonAuditGroupSpec(
        "historical-browser-use",
        "resolve",
        "research/pilots/browser_use/requirements.txt",
        None,
        "ci-linux-resolution-historical-only",
    ),
    PythonAuditGroupSpec(
        "week1-browsergym-public-entry",
        "resolve",
        "research/week1_miniwob/requirements.txt",
        None,
        "ci-linux-resolution-public-entry",
    ),
)


@dataclass(frozen=True)
class PythonPin:
    name: str
    extras: tuple[str, ...]
    version: str
    source: str
    marker: str | None = None
    hashes: tuple[str, ...] = ()

    @property
    def normalized_name(self) -> str:
        return _normalize(self.name)

    @property
    def requirement(self) -> str:
        suffix = f"[{','.join(self.extras)}]" if self.extras else ""
        marker = f"; {self.marker}" if self.marker else ""
        return f"{self.normalized_name}{suffix}=={self.version}{marker}"


@dataclass(frozen=True)
class NpmPin:
    name: str
    version: str
    source: str
    integrity: str | None = None


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _is_manifest_candidate(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts
    return path.is_file() and not any(part in EXCLUDED_MANIFEST_PARTS for part in parts)


def dependency_manifests(root: Path = ROOT) -> tuple[list[Path], list[Path]]:
    """Return the requirement-style Python manifests and npm lockfiles under ``root``."""

    research = root / "research"
    python_files: set[Path] = set()
    if research.is_dir():
        for pattern in ("*requirements*.txt", "*requirements*.lock"):
            python_files.update(
                path.resolve()
                for path in research.rglob(pattern)
                if _is_manifest_candidate(path, root)
            )
    npm_files: set[Path] = set()
    for candidate in (root / "extension" / "package-lock.json",):
        if candidate.is_file():
            npm_files.add(candidate.resolve())
    if research.is_dir():
        npm_files.update(
            path.resolve()
            for path in research.rglob("package-lock.json")
            if _is_manifest_candidate(path, root)
        )
    return sorted(python_files), sorted(npm_files)


def _logical_requirement_lines(path: Path) -> list[tuple[int, str]]:
    """Join pip/uv backslash continuations without accepting shell syntax."""

    logical: list[tuple[int, str]] = []
    parts: list[str] = []
    first_line = 0
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        continued = line.endswith("\\")
        fragment = line[:-1].rstrip() if continued else line
        if not fragment:
            raise SupplyChainError(f"{path}:{line_number} has an empty requirement continuation")
        if not parts:
            first_line = line_number
        parts.append(fragment)
        if not continued:
            logical.append((first_line, " ".join(parts)))
            parts = []
    if parts:
        raise SupplyChainError(f"{path}:{first_line} has an unterminated requirement continuation")
    return logical


def parse_python_manifest(
    path: Path,
    *,
    root: Path,
    seen: set[Path] | None = None,
) -> list[PythonPin]:
    """Parse an exact-pin requirements file (optionally hash locked, with ``-r`` includes)."""

    resolved = path.resolve()
    visited = set() if seen is None else seen
    if resolved in visited:
        return []
    if not resolved.is_file():
        raise SupplyChainError(f"missing requirement include: {_relative(resolved, root)}")
    visited.add(resolved)
    pins: list[PythonPin] = []
    for line_number, line in _logical_requirement_lines(resolved):
        if line.startswith(("-r ", "--requirement ")):
            include = line.split(maxsplit=1)[1]
            pins.extend(parse_python_manifest(resolved.parent / include, root=root, seen=visited))
            continue
        match = PIN_RE.fullmatch(line)
        if match is None:
            raise SupplyChainError(
                f"{_relative(resolved, root)}:{line_number} is not an exact direct pin: {line!r}"
            )
        options = match.group("options").strip()
        hashes = tuple(sorted(item.group("digest") for item in HASH_OPTION_RE.finditer(options)))
        options_without_hashes = HASH_OPTION_RE.sub(" ", options).strip()
        if "--" in options_without_hashes:
            raise SupplyChainError(
                f"{_relative(resolved, root)}:{line_number} has an unsupported or malformed option"
            )
        marker: str | None = None
        if options_without_hashes:
            if not options_without_hashes.startswith(";"):
                raise SupplyChainError(
                    f"{_relative(resolved, root)}:{line_number} has trailing non-marker content"
                )
            marker = options_without_hashes[1:].strip()
        if len(hashes) != len(set(hashes)):
            raise SupplyChainError(
                f"{_relative(resolved, root)}:{line_number} repeats a SHA-256 hash"
            )
        if marker is not None and MARKER_RE.fullmatch(marker) is None:
            raise SupplyChainError(
                f"{_relative(resolved, root)}:{line_number} has an unsafe environment marker"
            )
        extras = tuple(sorted(filter(None, (match.group("extras") or "").lower().split(","))))
        pins.append(
            PythonPin(
                name=match.group("name"),
                extras=extras,
                version=match.group("version"),
                source=_relative(resolved, root),
                marker=marker,
                hashes=hashes,
            )
        )
    return pins


def _load_uv_lock(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SupplyChainError(f"cannot read {path.name}: {error}") from error


def parse_uv_lock(path: Path, *, root: Path) -> list[PythonPin]:
    """Return one pin per registry package in ``uv.lock`` (the project itself is skipped).

    Fails closed when a package comes from anything but the official PyPI index
    or when a registry package carries no SHA-256 artifact hash.
    """

    payload = _load_uv_lock(path)
    packages = payload.get("package")
    if not isinstance(packages, list) or not packages:
        raise SupplyChainError(f"{path.name} lists no packages")
    source_name = _relative(path, root)
    pins: list[PythonPin] = []
    for package in packages:
        if not isinstance(package, Mapping):
            raise SupplyChainError(f"{path.name} has a malformed [[package]] entry")
        name = package.get("name")
        version = package.get("version")
        source = package.get("source")
        if not isinstance(name, str) or not isinstance(source, Mapping):
            raise SupplyChainError(f"{path.name} has a package without name or source")
        if "registry" not in source:
            # editable/virtual/path entries are the project itself, not a dependency.
            continue
        if source.get("registry") != OFFICIAL_INDEX_URL:
            raise SupplyChainError(f"{path.name}: {name} resolves from a non-official index")
        if not isinstance(version, str) or not version:
            raise SupplyChainError(f"{path.name}: {name} has no exact version")
        artifacts: list[Mapping[str, Any]] = []
        sdist = package.get("sdist")
        if isinstance(sdist, Mapping):
            artifacts.append(sdist)
        wheels = package.get("wheels")
        if isinstance(wheels, list):
            artifacts.extend(item for item in wheels if isinstance(item, Mapping))
        hashes = sorted(
            {
                str(item["hash"]).split(":", 1)[1]
                for item in artifacts
                if isinstance(item.get("hash"), str) and str(item["hash"]).startswith("sha256:")
            }
        )
        if not hashes:
            raise SupplyChainError(f"{path.name}: {name}=={version} has no SHA-256 artifact hash")
        pins.append(
            PythonPin(
                name=name,
                extras=(),
                version=version,
                source=source_name,
                hashes=tuple(hashes),
            )
        )
    return pins


def _pyproject_exact_pins(root: Path) -> dict[str, str]:
    payload = tomllib.loads((root / PYPROJECT_NAME).read_text(encoding="utf-8"))
    specifiers: list[str] = []
    project = payload.get("project", {})
    specifiers.extend(project.get("dependencies", []))
    for values in project.get("optional-dependencies", {}).values():
        specifiers.extend(values)
    for values in payload.get("dependency-groups", {}).values():
        specifiers.extend(item for item in values if isinstance(item, str))
    pins: dict[str, str] = {}
    for specifier in specifiers:
        match = SPEC_PIN_RE.fullmatch(specifier)
        if match is not None:
            pins[_normalize(match.group("name"))] = match.group("version")
    return pins


def validate_uv_lock(root: Path = ROOT) -> list[PythonPin]:
    """Check that uv.lock matches pyproject.toml and is fully hash pinned.

    * the lock's own project entry has the pyproject name and version;
    * every exact ``==`` pin declared in pyproject (dependencies, extras and
      dependency groups) is locked at exactly that version;
    * every locked package resolves from the official index with a SHA-256 hash.
    """

    lock_path = root / UV_LOCK_NAME
    if not lock_path.is_file():
        raise SupplyChainError(f"missing {UV_LOCK_NAME}; run `uv lock`")
    identity = project_identity(root)
    payload = _load_uv_lock(lock_path)
    project_entries = [
        package
        for package in payload.get("package", [])
        if isinstance(package, Mapping)
        and isinstance(package.get("source"), Mapping)
        and ("editable" in package["source"] or "virtual" in package["source"])
        and _normalize(str(package.get("name", ""))) == identity.normalized_name
    ]
    if len(project_entries) != 1:
        raise SupplyChainError(f"{UV_LOCK_NAME} does not lock the project {identity.name}")
    if project_entries[0].get("version") != identity.version:
        raise SupplyChainError(
            f"{UV_LOCK_NAME} locks {identity.name} {project_entries[0].get('version')}, "
            f"pyproject.toml declares {identity.version}; run `uv lock`"
        )
    pins = parse_uv_lock(lock_path, root=root)
    locked: dict[str, set[str]] = {}
    for pin in pins:
        locked.setdefault(pin.normalized_name, set()).add(pin.version)
    mismatches = sorted(
        f"{name}=={version} (locked: {sorted(locked.get(name, set()))})"
        for name, version in _pyproject_exact_pins(root).items()
        if version not in locked.get(name, set())
    )
    if mismatches:
        raise SupplyChainError(f"{UV_LOCK_NAME} does not preserve exact pins: {mismatches}")
    return pins


def collect_python_pins(root: Path = ROOT) -> list[PythonPin]:
    python_files, _ = dependency_manifests(root)
    pins = [pin for path in python_files for pin in parse_python_manifest(path, root=root)]
    lock_path = root / UV_LOCK_NAME
    if lock_path.is_file():
        pins.extend(parse_uv_lock(lock_path, root=root))
    unique = {
        (
            pin.normalized_name,
            pin.extras,
            pin.version,
            pin.marker,
            pin.source,
            pin.hashes,
        ): pin
        for pin in pins
    }
    return sorted(
        unique.values(),
        key=lambda pin: (
            pin.normalized_name,
            pin.version,
            pin.extras,
            pin.marker or "",
            pin.source,
            pin.hashes,
        ),
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_lock_pair(direct_path: Path, lock_path: Path, *, root: Path) -> None:
    direct = parse_python_manifest(direct_path, root=root)
    locked = parse_python_manifest(lock_path, root=root)
    versions: dict[str, set[str]] = {}
    for pin in locked:
        versions.setdefault(pin.normalized_name, set()).add(pin.version)
    mismatches = [
        pin.requirement for pin in direct if versions.get(pin.normalized_name) != {pin.version}
    ]
    if mismatches:
        raise SupplyChainError(
            f"{_relative(lock_path, root)} does not preserve direct pins: {mismatches}"
        )


def _coerce_audit_spec(
    value: PythonAuditGroupSpec | tuple[str, str, str, str | None],
) -> PythonAuditGroupSpec:
    if isinstance(value, PythonAuditGroupSpec):
        return value
    if len(value) != 4:
        raise SupplyChainError("Python audit group spec must contain exactly four fields")
    return PythonAuditGroupSpec(*value, target_environment="test-unspecified")


def build_python_audit_groups(
    root: Path = ROOT,
    *,
    specs: Sequence[
        PythonAuditGroupSpec | tuple[str, str, str, str | None]
    ] = PYTHON_AUDIT_GROUP_SPECS,
) -> dict[str, Any]:
    """Build the closed list of mutually isolated research audit graphs."""

    groups: list[dict[str, Any]] = []
    ids: set[str] = set()
    paths: set[str] = set()
    for raw_spec in specs:
        spec = _coerce_audit_spec(raw_spec)
        group_id = spec.id
        mode = spec.mode
        relative = spec.requirement_path
        direct_relative = spec.direct_requirement_path
        if group_id in ids or relative in paths:
            raise SupplyChainError("Python audit group IDs and requirement paths must be unique")
        if mode not in {"locked", "resolve"}:
            raise SupplyChainError(f"invalid Python audit mode for {group_id}: {mode}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,80}", spec.target_environment):
            raise SupplyChainError(f"invalid Python audit target environment for {group_id}")
        requirement_path = (root / relative).resolve()
        try:
            requirement_path.relative_to(root.resolve())
        except ValueError as error:
            raise SupplyChainError(f"Python audit group escapes root: {relative}") from error
        # Parsing enforces exact direct pins and safe local -r includes before
        # this path can become an executable CI audit target.
        pins = parse_python_manifest(requirement_path, root=root)
        if not pins:
            raise SupplyChainError(f"Python audit group {group_id} has no pinned requirements")
        hashed = [bool(pin.hashes) for pin in pins]
        if any(hashed) and not all(hashed):
            raise SupplyChainError(
                f"Python audit group {group_id} mixes hashed and unhashed requirements"
            )
        hash_policy = "all_requirements_sha256" if all(hashed) else "not_provided"
        direct_sha256: str | None = None
        if direct_relative is not None:
            direct_path = (root / direct_relative).resolve()
            _validate_lock_pair(direct_path, requirement_path, root=root)
            direct_sha256 = _file_sha256(direct_path)
        elif mode == "locked":
            raise SupplyChainError(f"locked Python audit group {group_id} has no direct spec")
        if mode == "locked" and hash_policy != "all_requirements_sha256":
            raise SupplyChainError(
                f"locked Python audit group {group_id} must hash every requirement"
            )
        ids.add(group_id)
        paths.add(relative)
        groups.append(
            {
                "id": group_id,
                "mode": mode,
                "requirement_path": relative,
                "requirement_sha256": _file_sha256(requirement_path),
                "direct_requirement_path": direct_relative,
                "direct_requirement_sha256": direct_sha256,
                "target_environment": spec.target_environment,
                "hash_policy": hash_policy,
            }
        )
    return {
        "schema_version": "cui-python-audit-groups-v2",
        "index_url": OFFICIAL_INDEX_URL,
        "groups": groups,
    }


def audit_groups_bytes(root: Path = ROOT) -> bytes:
    return (json.dumps(build_python_audit_groups(root), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _npm_name(package_path: str, package: Mapping[str, object]) -> str:
    explicit = package.get("name")
    if isinstance(explicit, str) and explicit:
        return explicit
    marker = package_path.rsplit("node_modules/", 1)[-1]
    if marker.startswith("@"):
        parts = marker.split("/")
        return "/".join(parts[:2])
    return marker.split("/", 1)[0]


def parse_npm_lock(path: Path, *, root: Path) -> list[NpmPin]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SupplyChainError(f"{_relative(path, root)} is not valid JSON: {error}") from error
    packages = payload.get("packages") if isinstance(payload, Mapping) else None
    if not isinstance(packages, Mapping):
        raise SupplyChainError(f"{_relative(path, root)} lacks a package-lock packages map")
    pins: list[NpmPin] = []
    for package_path, raw_package in packages.items():
        if not package_path or "node_modules/" not in str(package_path):
            continue
        if not isinstance(raw_package, Mapping) or raw_package.get("link") is True:
            continue
        version = raw_package.get("version")
        if not isinstance(version, str) or not version:
            raise SupplyChainError(
                f"{_relative(path, root)} package {package_path!r} has no exact version"
            )
        resolved = raw_package.get("resolved")
        # Bundled dependencies ship inside their parent's tarball and carry no URL.
        if raw_package.get("inBundle") is not True and not (
            isinstance(resolved, str) and resolved.startswith(OFFICIAL_NPM_REGISTRY)
        ):
            raise SupplyChainError(
                f"{_relative(path, root)} package {package_path!r} does not resolve from "
                f"{OFFICIAL_NPM_REGISTRY} (resolved: {resolved!r})"
            )
        integrity = raw_package.get("integrity")
        pins.append(
            NpmPin(
                name=_npm_name(str(package_path), raw_package),
                version=version,
                source=_relative(path, root),
                integrity=integrity if isinstance(integrity, str) else None,
            )
        )
    return sorted(pins, key=lambda pin: (pin.name.lower(), pin.version, pin.source))


def collect_npm_pins(root: Path = ROOT) -> list[NpmPin]:
    _, npm_files = dependency_manifests(root)
    pins = [pin for path in npm_files for pin in parse_npm_lock(path, root=root)]
    unique = {(pin.name, pin.version, pin.source, pin.integrity): pin for pin in pins}
    return sorted(unique.values(), key=lambda pin: (pin.name.lower(), pin.version, pin.source))


def audit_requirements_bytes(pins: Iterable[PythonPin]) -> bytes:
    """Render a sorted, deduplicated inventory of every exact Python pin."""

    requirements = sorted({pin.requirement for pin in pins})
    header = (
        "# Generated by tools/ci/supply_chain.py. An inventory of every exact Python\n"
        "# pin in the repository, not one installable environment: conflicting\n"
        "# versions from isolated research graphs are intentional.\n"
    )
    return (header + "\n".join(requirements) + "\n").encode("utf-8")


def _sha512_from_integrity(integrity: str | None) -> str | None:
    if not integrity or not integrity.startswith("sha512-"):
        return None
    try:
        return base64.b64decode(integrity[7:], validate=True).hex()
    except (ValueError, binascii.Error):
        return None


def build_sbom(root: Path = ROOT, *, identity: ProjectIdentity | None = None) -> dict[str, Any]:
    """Build a deterministic CycloneDX 1.6 document for every locked component."""

    project = identity or _identity_for(root)
    property_name = f"{project.normalized_name}:source-manifests"
    components: dict[tuple[str, str, str], dict[str, Any]] = {}
    sources: dict[tuple[str, str, str], set[str]] = {}
    for pin in collect_python_pins(root):
        key = ("pypi", pin.normalized_name, pin.version)
        purl = f"pkg:pypi/{pin.normalized_name}@{quote(pin.version, safe='.-_~')}"
        components.setdefault(
            key,
            {
                "type": "library",
                "bom-ref": purl,
                "name": pin.normalized_name,
                "version": pin.version,
                "purl": purl,
            },
        )
        sources.setdefault(key, set()).add(pin.source)
    for npm_pin in collect_npm_pins(root):
        encoded_name = quote(npm_pin.name, safe="/-._~")
        key = ("npm", npm_pin.name.lower(), npm_pin.version)
        purl = f"pkg:npm/{encoded_name}@{quote(npm_pin.version, safe='.-_~')}"
        component = components.setdefault(
            key,
            {
                "type": "library",
                "bom-ref": purl,
                "name": npm_pin.name,
                "version": npm_pin.version,
                "purl": purl,
            },
        )
        sha512 = _sha512_from_integrity(npm_pin.integrity)
        if sha512 is not None:
            component["hashes"] = [{"alg": "SHA-512", "content": sha512}]
        sources.setdefault(key, set()).add(npm_pin.source)
    ordered_components: list[dict[str, Any]] = []
    for key in sorted(components):
        component = dict(components[key])
        component["properties"] = [{"name": property_name, "value": ";".join(sorted(sources[key]))}]
        ordered_components.append(component)
    refs = sorted(str(component["bom-ref"]) for component in ordered_components)
    root_component: dict[str, Any] = {
        "type": "application",
        "bom-ref": project.root_ref,
        "name": project.normalized_name,
        "version": project.version,
        "purl": project.root_ref,
        "licenses": [{"license": {"id": "MIT"}}],
    }
    if project.repository:
        root_component["externalReferences"] = [{"type": "vcs", "url": project.repository}]
    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {"component": root_component},
        "components": ordered_components,
        "dependencies": [{"ref": project.root_ref, "dependsOn": refs}],
    }


def validate_sbom(payload: Mapping[str, Any]) -> None:
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.6":
        raise SupplyChainError("SBOM is not CycloneDX 1.6")
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        raise SupplyChainError("SBOM has no metadata")
    if "serialNumber" in payload or "timestamp" in metadata:
        raise SupplyChainError("reproducible SBOM must not contain random/time-derived fields")
    root_component = metadata.get("component")
    root_ref = root_component.get("bom-ref") if isinstance(root_component, Mapping) else None
    if not isinstance(root_ref, str) or not root_ref:
        raise SupplyChainError("SBOM metadata does not name the root component")
    raw_components = payload.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise SupplyChainError("SBOM has no components")
    raw_refs = [
        component.get("bom-ref") for component in raw_components if isinstance(component, Mapping)
    ]
    if (
        len(raw_refs) != len(raw_components)
        or not all(isinstance(ref, str) for ref in raw_refs)
        or len(set(raw_refs)) != len(raw_refs)
    ):
        raise SupplyChainError("SBOM component references are missing or duplicated")
    refs = [str(ref) for ref in raw_refs]
    if payload.get("dependencies") != [{"ref": root_ref, "dependsOn": sorted(refs)}]:
        raise SupplyChainError("SBOM root dependency graph does not cover every component")

    # Validate against the schema bundled with the locked CycloneDX library. All
    # referenced schemas are registered locally, so validation never fetches
    # mutable network content.
    try:
        import cyclonedx.schema
        from jsonschema import Draft7Validator
        from referencing import Registry, Resource
    except ImportError as error:  # pragma: no cover - CI installs the dev group
        raise SupplyChainError(
            "install the dev dependency group (`uv sync --group dev`) to validate the SBOM"
        ) from error
    resource_dir = Path(cyclonedx.schema.__file__).resolve().parent / "_res"
    schema = json.loads((resource_dir / "bom-1.6.SNAPSHOT.schema.json").read_text(encoding="utf-8"))
    registry: Any = Registry()
    for name in ("spdx.SNAPSHOT.schema.json", "jsf-0.82.SNAPSHOT.schema.json"):
        resource = json.loads((resource_dir / name).read_text(encoding="utf-8"))
        registry = registry.with_resource(
            f"http://cyclonedx.org/schema/{name}", Resource.from_contents(resource)
        )
    errors = sorted(Draft7Validator(schema, registry=registry).iter_errors(payload), key=str)
    if errors:
        raise SupplyChainError(f"CycloneDX schema validation failed: {errors[0].message}")


def sbom_bytes(root: Path = ROOT) -> bytes:
    payload = build_sbom(root)
    validate_sbom(payload)
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def default_sbom_path(root: Path = ROOT) -> Path:
    identity = project_identity(root)
    return root / "dist" / f"{identity.normalized_name}-{identity.version}.cdx.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write the SBOM (and optional extras)")
    mode.add_argument("--check", action="store_true", help="build and validate without writing")
    mode.add_argument("--print-python-audit-targets", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--sbom-output", type=Path, help="default: dist/<name>-<version>.cdx.json")
    parser.add_argument("--audit-groups-output", type=Path, help="also write the audit plan JSON")
    parser.add_argument("--audit-output", type=Path, help="also write the pin inventory text")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        validate_uv_lock(root)
        audit_groups = build_python_audit_groups(root)
        if args.print_python_audit_targets:
            for group in audit_groups["groups"]:
                print(f"{group['id']}\t{group['mode']}\t{group['requirement_path']}")
            return 0
        sbom_data = sbom_bytes(root)
        if args.write:
            sbom_path = args.sbom_output or default_sbom_path(root)
            sbom_path.parent.mkdir(parents=True, exist_ok=True)
            sbom_path.write_bytes(sbom_data)
            print(f"wrote {sbom_path}")
            if args.audit_groups_output:
                args.audit_groups_output.parent.mkdir(parents=True, exist_ok=True)
                args.audit_groups_output.write_bytes(
                    (json.dumps(audit_groups, indent=2, sort_keys=True) + "\n").encode("utf-8")
                )
                print(f"wrote {args.audit_groups_output}")
            if args.audit_output:
                args.audit_output.parent.mkdir(parents=True, exist_ok=True)
                args.audit_output.write_bytes(audit_requirements_bytes(collect_python_pins(root)))
                print(f"wrote {args.audit_output}")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}")
        return 1
    component_count = len(json.loads(sbom_data)["components"])
    print(
        f"PASS: deterministic CycloneDX SBOM for {project_identity(root).name} has "
        f"{component_count} components; "
        f"{len(audit_groups['groups'])} isolated research audit groups"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
