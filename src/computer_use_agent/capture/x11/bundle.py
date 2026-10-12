"""Deterministic, standard-library zipapp for the X11 guest recorder.

The ScienceBoard guest runs the recorder with its system ``/usr/bin/python3``
and cannot install this package. The host therefore deploys exactly one file,
``computer-use-agent-x11-recorder.pyz``, which contains the import-light package
root, the shared trace core and the X11 capture modules::

    computer_use_agent/__init__.py
    computer_use_agent/trace/*.py
    computer_use_agent/capture/__init__.py
    computer_use_agent/capture/x11/*.py

and a root ``__main__.py`` that dispatches to
:mod:`computer_use_agent.capture.x11.__main__`. In the guest::

    python3 computer-use-agent-x11-recorder.pyz doctor --control-dir ... --data-root ...
    python3 computer-use-agent-x11-recorder.pyz record --doctor

The archive is byte-for-byte reproducible: members are sorted, stored without
compression, carry a fixed 1980-01-01 timestamp, fixed Unix permissions and a
fixed creator system, and every source file is normalized to LF line endings.
Two builds from the same sources therefore have the same SHA-256 on every OS,
which lets the host bind the deployed bytes by hash.

Everything in the bundle must run on the guest's system Python 3.10 with the
standard library only (python-xlib, baked into the guest snapshot, is imported
lazily by the recorder). ``tests/capture/test_bundle.py`` enforces both.

Build on the host::

    python -m computer_use_agent.capture.x11.bundle --output path/to/dir-or-file.pyz
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import secrets
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

BUNDLE_NAME = "computer-use-agent-x11-recorder.pyz"
INTERPRETER_LINE = b"#!/usr/bin/env python3\n"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644
ZIP_CREATOR_UNIX = 3
# The pinned guest (Ubuntu 22.04) runs Python 3.10. The bundled modules use only
# 3.10 standard-library names (tests/capture/test_bundle.py rejects newer ones,
# including ``datetime.UTC``); the entry point still backfills ``datetime.UTC`` as a
# safety net before importing the package.
ROOT_MAIN = (
    '"""Entry point of the ComputerUseAgent X11 guest bundle (generated; do not edit)."""\n'
    "\n"
    "import datetime\n"
    "import sys\n"
    "\n"
    'if not hasattr(datetime, "UTC"):  # Python 3.10 guest\n'
    "    datetime.UTC = datetime.timezone.utc\n"
    "\n"
    "from computer_use_agent.capture.x11.__main__ import main  # noqa: E402\n"
    "\n"
    "sys.exit(main())\n"
)
GUEST_MINIMUM_PYTHON = (3, 10)

# Package directories copied into the bundle, relative to the package root, and
# whether every module in the directory ships (``*``) or only ``__init__.py``.
_BUNDLED_DIRECTORIES: tuple[tuple[str, bool], ...] = (
    ("", False),
    ("trace", True),
    ("capture", False),
    ("capture/x11", True),
)
PACKAGE_NAME = "computer_use_agent"


@dataclass(frozen=True)
class BuiltBundle:
    """Location and identity of one built guest bundle."""

    path: Path
    sha256: str
    size_bytes: int
    members: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.path.name,
            "path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "members": list(self.members),
        }


def package_root() -> Path:
    """Return the on-disk ``computer_use_agent`` directory used as the source."""

    root = Path(__file__).resolve().parents[2]
    if not (root / "__init__.py").is_file() or root.name != PACKAGE_NAME:
        raise RuntimeError(
            "The guest bundle can only be built from an on-disk computer_use_agent package"
        )
    return root


def bundle_sources(root: Path | None = None) -> list[tuple[str, Path]]:
    """Return ``(archive name, source path)`` pairs in deterministic order."""

    source_root = Path(root) if root is not None else package_root()
    members: list[tuple[str, Path]] = []
    for relative, every_module in _BUNDLED_DIRECTORIES:
        directory = source_root.joinpath(*Path(relative).parts)
        candidates = sorted(directory.glob("*.py")) if every_module else [directory / "__init__.py"]
        for path in candidates:
            if not path.is_file():
                raise FileNotFoundError(f"Guest bundle source is missing: {path}")
            name = "/".join((PACKAGE_NAME, *Path(relative).parts, path.name))
            members.append((name, path))
    members.sort(key=lambda item: item[0])
    return members


def _normalized(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = ZIP_CREATOR_UNIX
    info.external_attr = FILE_MODE << 16
    return info


def bundle_bytes(root: Path | None = None) -> bytes:
    """Build the complete archive in memory and return its bytes."""

    entries: list[tuple[str, bytes]] = [("__main__.py", ROOT_MAIN.encode("utf-8"))]
    entries.extend((name, _normalized(path.read_bytes())) for name, path in bundle_sources(root))
    entries.sort(key=lambda item: item[0])
    buffer = io.BytesIO()
    buffer.write(INTERPRETER_LINE)
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in entries:
            archive.writestr(_zip_info(name), data)
    return buffer.getvalue()


def bundle_members(root: Path | None = None) -> tuple[str, ...]:
    """Return every archive member name, including the root ``__main__.py``."""

    return tuple(sorted(["__main__.py", *(name for name, _ in bundle_sources(root))]))


def _destination(output_path: str | os.PathLike[str]) -> Path:
    destination = Path(output_path)
    if destination.is_dir():
        destination = destination / BUNDLE_NAME
    if destination.suffix != ".pyz":
        raise ValueError(f"Guest bundle output must be a .pyz file: {destination}")
    return destination.resolve()


def build(output_path: str | os.PathLike[str], *, root: Path | None = None) -> BuiltBundle:
    """Write the deterministic guest bundle and return its identity.

    ``output_path`` is the ``.pyz`` file to write, or an existing directory in
    which ``computer-use-agent-x11-recorder.pyz`` is written. An existing file is
    replaced atomically.
    """

    destination = _destination(output_path)
    payload = bundle_bytes(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return BuiltBundle(
        path=destination,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        members=bundle_members(root),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m computer_use_agent.capture.x11.bundle",
        description="Build the deterministic X11 guest recorder zipapp.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help=f"output .pyz file, or an existing directory to receive {BUNDLE_NAME}",
    )
    args = parser.parse_args(None if argv is None else list(argv))
    try:
        built = build(args.output)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"bundle build failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(built.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
