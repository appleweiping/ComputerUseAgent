"""Build the ComputerUseAgent Trace Recorder ZIP from the extension runtime files.

Output: ``dist/computer-use-agent-recorder-<version>.zip`` where ``<version>`` is
read from ``extension/app/manifest.json`` (the single version source).

Only runtime files and the legal notices are packaged, with ``manifest.json`` at
the archive root (the layout the Chrome Web Store expects; extract it to a folder
for "Load unpacked"):

* ``LICENSE`` and ``NOTICE``, copied from the repository root (never from
  ``extension/app``) so every distributed copy carries the MIT copyright and
  permission notice
* ``manifest.json``
* ``src/*.js``
* ``popup/``, ``dashboard/``, ``privacy/``: ``*.html``, ``*.css``, ``*.js``
* ``icons/*.png``

Every file under ``extension/app`` must match that allowlist; anything else
(notes, source maps, tests, links, a stray ``LICENSE``) fails the build instead
of being skipped silently. The archive is byte-for-byte reproducible across
runs and platforms: entries are sorted, stored without compression (zlib output
differs between builds), carry a fixed timestamp and permissions, and text files
(including ``LICENSE`` and ``NOTICE``) are packaged with LF line endings
regardless of the checkout's line-ending settings.

Usage::

    python tools/package_extension.py [--output-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = REPOSITORY_ROOT / "extension" / "app"
DEFAULT_OUTPUT_DIRECTORY = REPOSITORY_ROOT / "dist"
PACKAGE_STEM = "computer-use-agent-recorder"

# Top-level entry -> allowed file suffixes ("" marks a single file entry).
RUNTIME_ALLOWLIST: dict[str, frozenset[str]] = {
    "manifest.json": frozenset({""}),
    "src": frozenset({".js"}),
    "popup": frozenset({".html", ".css", ".js"}),
    "dashboard": frozenset({".html", ".css", ".js"}),
    "privacy": frozenset({".html", ".css", ".js"}),
    "icons": frozenset({".png"}),
}
TEXT_SUFFIXES = frozenset({".js", ".html", ".css", ".json"})
# Copied from the repository root (``legal_root``) to the archive root.
LEGAL_FILES = ("LICENSE", "NOTICE")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ZIP_FILE_MODE = 0o100644


class PackagingError(ValueError):
    """Raised when the extension tree cannot be packaged safely."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(reparse_flag and attributes & reparse_flag)


def _is_allowed(relative: PurePosixPath) -> bool:
    parts = relative.parts
    if len(parts) == 1:
        return RUNTIME_ALLOWLIST.get(parts[0]) == frozenset({""})
    suffixes = RUNTIME_ALLOWLIST.get(parts[0])
    if not suffixes or "" in suffixes:
        return False
    return all(not part.startswith(".") for part in parts) and relative.suffix in suffixes


def read_version(source: Path = SOURCE_DIRECTORY) -> str:
    try:
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PackagingError(f"cannot read extension manifest: {error}") from error
    version = manifest.get("version") if isinstance(manifest, dict) else None
    parts = version.split(".") if isinstance(version, str) else []
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise PackagingError(f"manifest version must be MAJOR.MINOR.PATCH, got {version!r}")
    return str(version)


def archive_name(version: str) -> str:
    return f"{PACKAGE_STEM}-{version}.zip"


def runtime_files(source: Path = SOURCE_DIRECTORY) -> list[tuple[str, Path]]:
    """Return ``(archive name, path)`` pairs, sorted, after validating the whole tree."""

    if not source.is_dir():
        raise PackagingError(f"extension source directory is missing: {source}")
    if _is_link_or_reparse(source):
        raise PackagingError("extension source directory must not be a link or reparse point")
    root = source.resolve(strict=True)
    selected: list[tuple[str, Path]] = []
    unexpected: list[str] = []
    for path in sorted(source.rglob("*")):
        relative = PurePosixPath(path.relative_to(source).as_posix())
        if _is_link_or_reparse(path):
            raise PackagingError(f"extension source contains a link/reparse point: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PackagingError(f"extension source entry is not a regular file: {relative}")
        try:
            path.resolve(strict=True).relative_to(root)
        except ValueError as error:
            raise PackagingError(
                f"extension source entry escapes the source root: {relative}"
            ) from error
        if _is_allowed(relative):
            selected.append((relative.as_posix(), path))
        else:
            unexpected.append(relative.as_posix())
    if unexpected:
        raise PackagingError(
            "extension/app contains files outside the runtime allowlist: " + ", ".join(unexpected)
        )
    names = {name for name, _path in selected}
    if "manifest.json" not in names:
        raise PackagingError("extension/app/manifest.json is missing")
    return sorted(selected)


def legal_files(legal_root: Path = REPOSITORY_ROOT) -> list[tuple[str, Path]]:
    """Return the ``(archive name, path)`` pairs for ``LEGAL_FILES``; all must exist."""

    selected: list[tuple[str, Path]] = []
    for name in LEGAL_FILES:
        path = legal_root / name
        if not path.exists() and not path.is_symlink():
            raise PackagingError(f"{name} is missing from {legal_root}")
        if _is_link_or_reparse(path) or not path.is_file():
            raise PackagingError(f"{name} must be a regular file, not a link or directory")
        selected.append((name, path))
    return selected


def _payload(name: str, path: Path) -> bytes:
    data = path.read_bytes()
    if name in LEGAL_FILES or PurePosixPath(name).suffix in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    return data


def build_archive(
    source: Path = SOURCE_DIRECTORY,
    output_directory: Path = DEFAULT_OUTPUT_DIRECTORY,
    legal_root: Path = REPOSITORY_ROOT,
) -> Path:
    files = sorted(runtime_files(source) + legal_files(legal_root))
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / archive_name(read_version(source))
    temporary_path = output_path.with_name(output_path.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary_path, mode="w", compression=zipfile.ZIP_STORED) as archive:
            for name, path in files:
                info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = ZIP_FILE_MODE << 16
                archive.writestr(info, _payload(name, path))
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    verify_archive(output_path, [name for name, _path in files])
    return output_path


def verify_archive(path: Path, expected_names: Sequence[str]) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if names != sorted(expected_names) or len(names) != len(set(names)):
            raise PackagingError("built archive does not match the runtime allowlist")
        for name in names:
            parts = PurePosixPath(name).parts
            allowed = name in LEGAL_FILES or _is_allowed(PurePosixPath(name))
            if name.startswith("/") or ".." in parts or not allowed:
                raise PackagingError(f"built archive contains an unsafe entry: {name}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the ComputerUseAgent Trace Recorder ZIP from extension/app."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
        help="directory for the ZIP (default: dist/ in the repository root)",
    )
    arguments = parser.parse_args(argv)
    try:
        archive = build_archive(SOURCE_DIRECTORY, arguments.output_dir)
    except PackagingError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(archive) as opened:
        count = len(opened.namelist())
    print(f"archive={archive}")
    print(f"files={count}")
    print(f"bytes={archive.stat().st_size}")
    print(f"sha256={sha256_file(archive)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
