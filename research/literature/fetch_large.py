"""Fetch and verify the large literature artifacts recorded in LARGE_ARTIFACTS.json.

The citation graph (``citation_graph.json``) and the assertion table
(``citation_assertions.csv``) are generated outputs that are too large to commit.
``research/literature/generated/LARGE_ARTIFACTS.json`` records each file's name, byte
count, SHA-256 and an immutable download URL (the source repository at a pinned
commit). This command places them in the git-ignored
``research/literature/generated/large/`` directory, where the citation builder and the
literature tests look for them.

Usage (from the repository root)::

    python -m research.literature.fetch_large                  # download over HTTPS
    python -m research.literature.fetch_large --from-dir DIR   # copy from a local directory
    python -m research.literature.fetch_large --verify-only    # check what is already there

The command is idempotent: a file that is already present with the recorded size and
SHA-256 is left untouched. A present file with the wrong bytes is replaced. Every new
file is written to a temporary name, verified, and only then renamed into place, so a
failed or interrupted run never leaves an unverified artifact behind.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from research.literature.scripts.corpus_common import (
    LARGE_ARTIFACTS_PATH,
    LARGE_DIR,
    LargeArtifactError,
    display_path,
    load_large_artifacts,
    sha256_file,
    verify_large_artifact,
)

USER_AGENT = "ComputerUseAgent-fetch-large/1.0 (+https://github.com/appleweiping/ComputerUseAgent)"
#: Hosts that may serve the recorded artifacts (redirect targets included).
ALLOWED_HOSTS = frozenset(
    {
        "raw.githubusercontent.com",
        "media.githubusercontent.com",
        "objects.githubusercontent.com",
        "github.com",
    }
)
CHUNK_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_ATTEMPTS = 3


class FetchError(RuntimeError):
    """An artifact could not be fetched and verified."""


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise FetchError(f"Refusing to download from a non-allowlisted URL: {url}")
    if parsed.username or parsed.password:
        raise FetchError("Refusing a download URL with embedded credentials")


def _open_url(url: str, timeout: float) -> Any:
    """Open ``url`` for streaming; separated so tests can substitute a fake opener."""

    request = Request(url, headers={"User-Agent": USER_AGENT})
    return urlopen(request, timeout=timeout)  # noqa: S310 - URL is allowlisted above


def _stream_to_verified_file(source: BinaryIO, destination: Path, spec: Mapping[str, Any]) -> None:
    """Copy ``source`` to a temporary file, verify it, then atomically replace."""

    expected_bytes = int(spec["bytes"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    digest = hashlib.sha256()
    written = 0
    try:
        with temporary.open("wb") as handle:
            while True:
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > expected_bytes:
                    raise LargeArtifactError(
                        f"{spec['name']} exceeds the recorded {expected_bytes} bytes"
                    )
                digest.update(chunk)
                handle.write(chunk)
        if written != expected_bytes:
            raise LargeArtifactError(
                f"{spec['name']} has {written} bytes, expected {expected_bytes}"
            )
        if digest.hexdigest() != spec["sha256"]:
            raise LargeArtifactError(
                f"{spec['name']} has sha256 {digest.hexdigest()}, expected {spec['sha256']}"
            )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_from_directory(spec: Mapping[str, Any], from_dir: Path, destination: Path) -> None:
    source = from_dir / str(spec["name"])
    if not source.is_file():
        raise FetchError(f"{spec['name']} is not in {from_dir}")
    if source.resolve() == destination.resolve():
        raise FetchError("--from-dir must not be the destination directory")
    with source.open("rb") as handle:
        _stream_to_verified_file(handle, destination, spec)


def _download(
    spec: Mapping[str, Any],
    destination: Path,
    *,
    timeout: float,
    attempts: int,
    opener: Callable[[str, float], Any],
) -> None:
    url = str(spec["url"])
    _check_url(url)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with opener(url, timeout) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                _check_url(final_url)
                length = response.headers.get("Content-Length") if response.headers else None
                if length is not None and int(length) != int(spec["bytes"]):
                    raise LargeArtifactError(
                        f"{spec['name']}: server reports {length} bytes, expected {spec['bytes']}"
                    )
                _stream_to_verified_file(response, destination, spec)
                return
        except LargeArtifactError:
            raise
        except (OSError, ValueError) as exc:  # URLError and HTTPError are OSErrors
            last_error = exc
            if attempt < attempts:
                time.sleep(min(2**attempt, 10))
    raise FetchError(f"Download of {spec['name']} failed after {attempts} attempts: {last_error}")


def fetch(
    specs: Mapping[str, Mapping[str, Any]],
    destination_dir: Path = LARGE_DIR,
    *,
    from_dir: Path | None = None,
    names: Iterable[str] | None = None,
    verify_only: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_ATTEMPTS,
    opener: Callable[[str, float], Any] = _open_url,
) -> dict[str, dict[str, Any]]:
    """Make every selected artifact present and verified in ``destination_dir``.

    Returns ``{name: {"status": ..., "bytes": ..., "sha256": ...}}`` where status is
    ``present`` (already verified, untouched), ``copied``, ``downloaded`` or, when a
    wrong file was overwritten, ``replaced-by-copy`` / ``replaced-by-download``.
    Raises :class:`FetchError` or :class:`LargeArtifactError` on the first failure.
    """

    selected = list(names) if names else sorted(specs)
    unknown = sorted(set(selected) - set(specs))
    if unknown:
        raise FetchError(f"Unknown large artifact(s): {', '.join(unknown)}")
    results: dict[str, dict[str, Any]] = {}
    for name in selected:
        spec = specs[name]
        destination = destination_dir / name
        replaced = False
        if destination.exists():
            try:
                verify_large_artifact(destination, spec)
            except LargeArtifactError:
                if verify_only:
                    raise
                replaced = True
            else:
                results[name] = {
                    "status": "present",
                    "bytes": spec["bytes"],
                    "sha256": spec["sha256"],
                }
                continue
        elif verify_only:
            raise LargeArtifactError(f"{name} is missing from {display_path(destination_dir)}")
        for stale in destination_dir.glob(f".{name}.*.part"):
            stale.unlink()  # left behind by an interrupted earlier run
        if from_dir is not None:
            _copy_from_directory(spec, from_dir, destination)
            status = "copied"
        else:
            _download(spec, destination, timeout=timeout, attempts=attempts, opener=opener)
            status = "downloaded"
        # Re-read the renamed file: the result must describe the bytes on disk.
        if sha256_file(destination) != spec["sha256"]:
            raise LargeArtifactError(f"{name} changed after it was written")
        if replaced:
            status = {"copied": "replaced-by-copy", "downloaded": "replaced-by-download"}[status]
        results[name] = {"status": status, "bytes": spec["bytes"], "sha256": spec["sha256"]}
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m research.literature.fetch_large",
        description=__doc__.split("\n\n", 1)[0],
    )
    parser.add_argument(
        "--from-dir",
        type=Path,
        help="Copy the files from this local directory instead of downloading them",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=LARGE_DIR,
        help="Destination directory (default: research/literature/generated/large)",
    )
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        help="Fetch only this artifact (repeatable; default: all recorded artifacts)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify files already in the destination; never download or copy",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=LARGE_ARTIFACTS_PATH,
        help="LARGE_ARTIFACTS.json to read (default: the tracked one)",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.attempts < 1:
        build_parser().error("--attempts must be at least 1")
    if args.from_dir is not None and args.verify_only:
        build_parser().error("--from-dir and --verify-only are mutually exclusive")
    try:
        specs = load_large_artifacts(args.manifest)
        results = fetch(
            specs,
            args.dest,
            from_dir=args.from_dir,
            names=args.name or None,
            verify_only=args.verify_only,
            timeout=args.timeout,
            attempts=args.attempts,
        )
    except (FetchError, LargeArtifactError, OSError, ValueError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "status": "pass",
                "destination": display_path(args.dest),
                "artifacts": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
