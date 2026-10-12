"""Assert that every release-facing version string is the same.

The canonical version is ``[project].version`` in ``pyproject.toml``. It must
equal:

* ``extension/app/manifest.json`` ``version`` (what Chrome installs);
* ``extension/package.json`` ``version`` (and ``extension/package-lock.json``);
* ``CITATION.cff`` ``version``;
* the literal fallback ``__version__`` in ``src/computer_use_agent/__init__.py``,
  when the package keeps one;
* with ``--tag vX.Y.Z`` (or ``refs/tags/vX.Y.Z``): the release tag, and
  ``CHANGELOG.md`` must then contain a ``## [X.Y.Z]`` section.

Versions must be plain ``MAJOR.MINOR.PATCH``: Chrome accepts only dotted
integers in ``manifest.json``, so pre-release labels are not used.

Usage::

    python tools/ci/check_versions.py
    python tools/ci/check_versions.py --tag v0.5.0
"""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
CFF_VERSION_RE = re.compile(r"""^version:\s*["']?(?P<version>[^"'\s#]+)["']?\s*(?:#.*)?$""", re.M)
INIT_VERSION_RE = re.compile(r"""^\s*__version__\s*=\s*["'](?P<version>[^"']+)["']""", re.M)


class VersionCheckError(ValueError):
    """Raised when a version source is missing or unreadable."""


@dataclass(frozen=True)
class VersionSource:
    label: str
    version: str | None
    error: str | None = None


def _json_version(path: Path, *keys: str) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    value: object = payload
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise VersionCheckError(f"{path.name} has no {'.'.join(keys)}")
        value = value[key]
    if not isinstance(value, str):
        raise VersionCheckError(f"{path.name} {'.'.join(keys)} is not a string")
    return value


def _regex_version(path: Path, pattern: re.Pattern[str]) -> str:
    match = pattern.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise VersionCheckError(f"{path.name} declares no version")
    return match.group("version")


def _pyproject_version(path: Path) -> str:
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    version = payload.get("project", {}).get("version")
    if not isinstance(version, str):
        raise VersionCheckError("pyproject.toml has no static [project].version")
    return version


def _read(label: str, reader: Callable[..., str], *args: object) -> VersionSource:
    try:
        version = reader(*args)
    except FileNotFoundError:
        return VersionSource(label, None, "file not found")
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        return VersionSource(label, None, str(error))
    return VersionSource(label, version)


def collect_versions(root: Path = ROOT) -> list[VersionSource]:
    """Read every version source; the first entry is the canonical one."""

    sources = [
        _read("pyproject.toml [project].version", _pyproject_version, root / "pyproject.toml"),
        _read(
            "extension/app/manifest.json version",
            _json_version,
            root / "extension" / "app" / "manifest.json",
            "version",
        ),
        _read(
            "extension/package.json version",
            _json_version,
            root / "extension" / "package.json",
            "version",
        ),
        _read("CITATION.cff version", _regex_version, root / "CITATION.cff", CFF_VERSION_RE),
    ]
    # Optional: only checked while the package keeps a literal fallback version.
    init = root / "src" / "computer_use_agent" / "__init__.py"
    if init.is_file() and INIT_VERSION_RE.search(init.read_text(encoding="utf-8")):
        sources.append(
            _read(
                "src/computer_use_agent/__init__.py fallback __version__",
                _regex_version,
                init,
                INIT_VERSION_RE,
            )
        )
    lock = root / "extension" / "package-lock.json"
    if lock.is_file():
        sources.append(_read("extension/package-lock.json version", _json_version, lock, "version"))
        sources.append(
            _read(
                'extension/package-lock.json packages[""].version',
                _json_version,
                lock,
                "packages",
                "",
                "version",
            )
        )
    return sources


def normalize_tag(tag: str) -> str:
    value = tag.strip()
    value = value.removeprefix("refs/tags/")
    if not value.startswith("v"):
        raise VersionCheckError(f"release tags must look like vX.Y.Z, got {tag!r}")
    return value[1:]


def check(root: Path = ROOT, *, tag: str | None = None) -> list[str]:
    """Return a list of problems (empty when every version agrees)."""

    sources = collect_versions(root)
    problems: list[str] = []
    canonical = sources[0].version
    if canonical is None:
        return [f"{sources[0].label}: {sources[0].error}"]
    if VERSION_RE.fullmatch(canonical) is None:
        problems.append(f"{sources[0].label}: {canonical!r} is not MAJOR.MINOR.PATCH")
    for source in sources[1:]:
        if source.version is None:
            problems.append(f"{source.label}: {source.error}")
        elif source.version != canonical:
            problems.append(f"{source.label}: {source.version!r} != {canonical!r}")
    if tag is not None:
        try:
            tag_version = normalize_tag(tag)
        except VersionCheckError as error:
            problems.append(str(error))
        else:
            if tag_version != canonical:
                problems.append(f"tag {tag!r}: {tag_version!r} != {canonical!r}")
            changelog = root / "CHANGELOG.md"
            heading = re.compile(rf"^## \[{re.escape(canonical)}\]", re.M)
            if (
                not changelog.is_file()
                or heading.search(changelog.read_text(encoding="utf-8")) is None
            ):
                problems.append(f"CHANGELOG.md has no '## [{canonical}]' section")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--tag", help="release tag, e.g. v0.5.0 or refs/tags/v0.5.0")
    parser.add_argument("--json", action="store_true", help="print the sources as JSON")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    sources = collect_versions(root)
    if args.json:
        print(
            json.dumps(
                [{"source": s.label, "version": s.version, "error": s.error} for s in sources],
                indent=2,
            )
        )
    else:
        for source in sources:
            print(f"{source.version or 'MISSING':>10}  {source.label}")
    problems = check(root, tag=args.tag)
    if problems:
        for problem in problems:
            print(f"ERROR {problem}")
        return 1
    suffix = f" and tag {args.tag}" if args.tag else ""
    print(f"PASS: every version source{suffix} is {sources[0].version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
