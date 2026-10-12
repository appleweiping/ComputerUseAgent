"""Install the exact MiniWoB source archive in a private directory.

Run from the repository root: ``python -m research.week1_miniwob.adapters.setup_miniwob``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from .miniwob_source import (
    PINNED_COMMIT,
    PINNED_HTML_TREE_SHA256,
    PINNED_STATIC_TREE_SHA256,
    SOURCE_MARKER_SCHEMA,
    default_source_root,
    html_tree_sha256,
    verify_source_root,
)


ARCHIVE_URL = f"https://api.github.com/repos/Farama-Foundation/miniwob-plusplus/zipball/{PINNED_COMMIT}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=default_source_root())
    parser.add_argument("--archive", type=Path, help="Use an already-downloaded GitHub ZIP")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def download_archive(destination: Path) -> str:
    digest = hashlib.sha256()
    request = Request(ARCHIVE_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "cui-week1-setup/1"})
    with urlopen(request, timeout=60) as response, destination.open("xb") as stream:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            stream.write(block)
    return digest.hexdigest()


def archive_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_extract(archive: Path, staging: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        roots = {Path(member.filename).parts[0] for member in members if Path(member.filename).parts}
        if len(roots) != 1:
            raise RuntimeError("GitHub archive must contain exactly one top-level directory")
        staging_resolved = staging.resolve()
        for member in members:
            target = (staging / member.filename).resolve()
            try:
                target.relative_to(staging_resolved)
            except ValueError as error:
                raise RuntimeError("archive contains a path outside the staging directory") from error
        bundle.extractall(staging)
    return staging / next(iter(roots))


def main() -> int:
    args = parse_args()
    if args.destination is None:
        print("Set CUI_WEEK1_PRIVATE_ROOT/CUI_WEEK1_MINIWOB_ROOT or pass --destination.", file=sys.stderr)
        return 2
    destination = args.destination.expanduser().resolve()
    plan = {
        "schema_version": "cui-week1-miniwob-install-plan-v1",
        "destination": str(destination),
        "commit": PINNED_COMMIT,
        "archive_url": ARCHIVE_URL,
        "writes_performed": False,
    }
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    if destination.exists():
        verified = verify_source_root(destination)
        plan.update(
            {
                "already_present": True,
                "html_tree_sha256": verified["html_tree_sha256"],
                "static_tree_sha256": verified["static_tree_sha256"],
            }
        )
        print(json.dumps(plan, indent=2, default=str))
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cui-miniwob-", dir=destination.parent) as temporary:
        temporary_root = Path(temporary)
        archive = args.archive.expanduser().resolve() if args.archive else temporary_root / "source.zip"
        archive_hash = archive_sha256(archive) if args.archive else download_archive(archive)
        extracted_root = safe_extract(archive, temporary_root / "extracted")
        html_root = extracted_root / "miniwob" / "html" / "miniwob"
        tree_hash = html_tree_sha256(html_root)
        if tree_hash != PINNED_HTML_TREE_SHA256:
            raise RuntimeError(
                f"downloaded HTML tree is {tree_hash}, expected pinned {PINNED_HTML_TREE_SHA256}"
            )
        static_root = extracted_root / "miniwob" / "html"
        static_tree_hash = html_tree_sha256(static_root)
        if static_tree_hash != PINNED_STATIC_TREE_SHA256:
            raise RuntimeError(
                "downloaded static asset tree is "
                f"{static_tree_hash}, expected pinned {PINNED_STATIC_TREE_SHA256}"
            )
        marker = {
            "schema_version": SOURCE_MARKER_SCHEMA,
            "source": ARCHIVE_URL,
            "commit": PINNED_COMMIT,
            "archive_sha256": archive_hash,
            "html_tree_sha256": tree_hash,
            "static_tree_sha256": static_tree_hash,
        }
        (extracted_root / ".cui-week1-source.json").write_text(
            json.dumps(marker, indent=2) + "\n", encoding="utf-8"
        )
        shutil.move(str(extracted_root), str(destination))
    verified = verify_source_root(destination)
    plan.update(
        {
            "writes_performed": True,
            "source_kind": verified["source_kind"],
            "archive_sha256": archive_hash,
            "html_tree_sha256": verified["html_tree_sha256"],
            "static_tree_sha256": verified["static_tree_sha256"],
        }
    )
    print(json.dumps(plan, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
