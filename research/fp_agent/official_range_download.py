"""Resume-safe downloader for the official FP-Agent OSF raw artifact.

Only the author-linked OSF API/download action is used. The OSF node is reached
through the share link published in the upstream FP-Agent README; that link,
its access query and every redirect URL are used in memory only and are never
logged or persisted. Chunks, logs, and the ledger live exclusively under the
private data root (``--data-root`` or ``$CUA_PRIVATE_ROOT/fp_agent``), which
must resolve outside the repository checkout.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research.fp_agent.paths import (
    USER_AGENT,
    PrivatePathError,
    require_data_root,
    require_upstream_repo,
)
from research.fp_agent.retrospective_features import RAW_FILENAME, sha256

OSF_NODE = "j6b5p"
OSF_FILES_API = f"https://api.osf.io/v2/nodes/{OSF_NODE}/files/osfstorage/"
EXPECTED_BYTES = 4_256_528_751
EXPECTED_SHA256 = "8acb0a5cecc7b2a71221ebcc189ba9d517e986774ed7cf1ad878d6463070acf4"
EXPECTED_MD5_ETAG = "60b25bf913e3370e1c32b96ee85dd95d"
DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024
DEFAULT_WORKERS = 8
ALLOWED_REDIRECT_HOSTS = frozenset({"osf.io", "files.osf.io", "storage.googleapis.com"})
# The upstream README links the OSF node as https://osf.io/<node>/...?<access query>.
SHARE_LINK = re.compile(r"https://osf\.io/" + OSF_NODE + r"(?![0-9a-z])[^\s)\]>\"'`]*")


@dataclass(frozen=True)
class DownloadLayout:
    """Every file the downloader touches, all inside the private data root."""

    data_root: Path

    @property
    def final_path(self) -> Path:
        return self.data_root / RAW_FILENAME

    @property
    def linear_partial(self) -> Path:
        return self.data_root / f"{RAW_FILENAME}.part"

    @property
    def parts_dir(self) -> Path:
        return self.data_root / "raw_dataset.parts"

    @property
    def ledger_path(self) -> Path:
        return self.data_root / "raw_dataset.range-ledger.json"


@dataclass(frozen=True)
class Chunk:
    index: int
    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    def path(self, directory: Path) -> Path:
        return directory / f"part-{self.index:05d}.bin"


def chunk_plan(total: int = EXPECTED_BYTES, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> list[Chunk]:
    if total <= 0 or chunk_bytes <= 0:
        raise ValueError("total and chunk_bytes must be positive")
    return [
        Chunk(index, start, min(total - 1, start + chunk_bytes - 1))
        for index, start in enumerate(range(0, total, chunk_bytes))
    ]


def parse_content_range(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", value.strip())
    if not match:
        raise ValueError("Malformed Content-Range")
    start, end, total = (int(item) for item in match.groups())
    return start, end, total


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        host = urllib.parse.urlparse(newurl).hostname
        if host not in ALLOWED_REDIRECT_HOSTS:
            raise urllib.error.HTTPError(newurl, code, "Unexpected redirect host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def share_link_query(upstream_readme: Path) -> str:
    """Return the access query of the upstream README's OSF share link.

    The query is forwarded unchanged to the OSF API and is never stored. An
    empty string means the link carries no query (a public node).
    """

    text = upstream_readme.read_text(encoding="utf-8")
    links = SHARE_LINK.findall(text)
    if not links:
        raise RuntimeError(f"the upstream README links no OSF share for node {OSF_NODE}")
    queries = {urllib.parse.urlsplit(link).query for link in links}
    with_query = sorted(query for query in queries if query)
    if len(with_query) > 1:
        raise RuntimeError("the upstream README links ambiguous OSF shares")
    return with_query[0] if with_query else ""


def official_metadata(upstream_repo: Path) -> dict[str, Any]:
    query = share_link_query(upstream_repo / "README.md")
    url = f"{OSF_FILES_API}?{query}" if query else OSF_FILES_API
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        # Never echo the request URL: it carries the share query.
        raise RuntimeError(f"OSF metadata request failed with HTTP {error.code}") from None
    rows = [row for row in payload["data"] if row["attributes"]["name"] == RAW_FILENAME]
    if len(rows) != 1:
        raise RuntimeError("Official OSF raw artifact metadata is missing or ambiguous")
    row = rows[0]
    attributes = row["attributes"]
    if attributes["size"] != EXPECTED_BYTES:
        raise ValueError("Official OSF byte count changed")
    if attributes["extra"]["hashes"]["sha256"] != EXPECTED_SHA256:
        raise ValueError("Official OSF SHA-256 changed")
    return {"download_action": row["links"]["download"], "bytes": attributes["size"]}


def _download_chunk(
    chunk: Chunk, download_action: str, parts_dir: Path, *, max_attempts: int = 10
) -> dict[str, Any]:
    path = chunk.path(parts_dir)
    if path.is_file() and path.stat().st_size == chunk.size:
        return {"index": chunk.index, "bytes": chunk.size, "sha256": sha256(path), "reused": True}
    partial = path.with_suffix(".partial")
    if partial.exists() and partial.stat().st_size > chunk.size:
        raise ValueError(f"Chunk {chunk.index} partial is oversized")
    opener = urllib.request.build_opener(RestrictedRedirect())
    for attempt in range(max_attempts):
        existing = partial.stat().st_size if partial.exists() else 0
        if existing == chunk.size:
            os.replace(partial, path)
            return {
                "index": chunk.index,
                "bytes": chunk.size,
                "sha256": sha256(path),
                "reused": False,
            }
        requested_start = chunk.start + existing
        request = urllib.request.Request(
            download_action,
            headers={
                "Range": f"bytes={requested_start}-{chunk.end}",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with opener.open(request, timeout=120) as response:
                if response.status != 206:
                    raise ValueError(f"Chunk {chunk.index} did not receive HTTP 206")
                observed = parse_content_range(response.headers.get("Content-Range", ""))
                if observed != (requested_start, chunk.end, EXPECTED_BYTES):
                    raise ValueError(f"Chunk {chunk.index} Content-Range mismatch")
                expected_response = chunk.end - requested_start + 1
                if int(response.headers.get("Content-Length", -1)) != expected_response:
                    raise ValueError(f"Chunk {chunk.index} Content-Length mismatch")
                with partial.open("ab") as handle:
                    shutil.copyfileobj(response, handle, length=1024 * 1024)
            if partial.stat().st_size != chunk.size:
                raise OSError(f"Chunk {chunk.index} ended early")
            os.replace(partial, path)
            return {
                "index": chunk.index,
                "bytes": chunk.size,
                "sha256": sha256(path),
                "reused": False,
            }
        except (OSError, ValueError, urllib.error.URLError):
            if attempt + 1 == max_attempts:
                raise
            time.sleep(min(60, 2**attempt))
    raise AssertionError("unreachable")


def _seed_first_chunk(plan: list[Chunk], layout: DownloadLayout) -> bool:
    first = plan[0]
    target = first.path(layout.parts_dir)
    linear = layout.linear_partial
    if target.exists() or not linear.is_file() or linear.stat().st_size < first.size:
        return False
    temporary = target.with_suffix(".partial")
    with linear.open("rb") as source, temporary.open("wb") as destination:
        remaining = first.size
        while remaining:
            block = source.read(min(1024 * 1024, remaining))
            if not block:
                raise OSError("Linear partial ended while seeding first chunk")
            destination.write(block)
            remaining -= len(block)
    os.replace(temporary, target)
    return True


def _write_ledger(
    layout: DownloadLayout,
    rows: list[dict[str, Any]],
    *,
    status: str,
    started_at: str,
    chunk_bytes: int,
    workers: int,
) -> None:
    payload = {
        "status": status,
        "started_at": started_at,
        "updated_at_epoch": time.time(),
        "expected_bytes": EXPECTED_BYTES,
        "expected_sha256": EXPECTED_SHA256,
        "chunk_bytes": chunk_bytes,
        "workers": workers,
        "completed_chunks": sorted(rows, key=lambda row: row["index"]),
    }
    temporary = layout.ledger_path.with_suffix(".partial")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, layout.ledger_path)


def assemble(plan: list[Chunk], parts_dir: Path, final_path: Path) -> dict[str, Any]:
    assembling = final_path.with_suffix(".assembling")
    with assembling.open("wb") as output:
        for chunk in plan:
            path = chunk.path(parts_dir)
            if not path.is_file() or path.stat().st_size != chunk.size:
                raise ValueError(f"Chunk {chunk.index} is absent or wrong-sized")
            with path.open("rb") as source:
                shutil.copyfileobj(source, output, length=8 * 1024 * 1024)
    if assembling.stat().st_size != EXPECTED_BYTES:
        raise ValueError("Assembled raw byte count mismatch")
    digest = sha256(assembling)
    if digest != EXPECTED_SHA256:
        raise ValueError("Assembled raw SHA-256 mismatch; final file was not installed")
    os.replace(assembling, final_path)
    return {"bytes": final_path.stat().st_size, "sha256": digest}


def download(
    *,
    data_root: Path,
    upstream_repo: Path,
    workers: int = DEFAULT_WORKERS,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> dict[str, Any]:
    if not 1 <= workers <= 16:
        raise ValueError("workers must be between 1 and 16")
    layout = DownloadLayout(require_data_root(data_root))
    layout.data_root.mkdir(parents=True, exist_ok=True)
    layout.parts_dir.mkdir(parents=True, exist_ok=True)
    final_path = layout.final_path
    if final_path.is_file() and final_path.stat().st_size == EXPECTED_BYTES:
        digest = sha256(final_path)
        if digest != EXPECTED_SHA256:
            raise ValueError("Existing final raw artifact has the wrong SHA-256")
        return {"status": "complete", "bytes": EXPECTED_BYTES, "sha256": digest, "reused": True}
    metadata = official_metadata(upstream_repo)
    plan = chunk_plan(chunk_bytes=chunk_bytes)
    seeded = _seed_first_chunk(plan, layout)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows: list[dict[str, Any]] = []
    lock = threading.Lock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_download_chunk, chunk, metadata["download_action"], layout.parts_dir): (
                chunk
            )
            for chunk in plan
        }
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            with lock:
                rows.append(row)
                _write_ledger(
                    layout,
                    rows,
                    status="downloading",
                    started_at=started_at,
                    chunk_bytes=chunk_bytes,
                    workers=workers,
                )
                if len(rows) % 8 == 0 or len(rows) == len(plan):
                    progress = {
                        "status": "downloading",
                        "completed": len(rows),
                        "total": len(plan),
                        "bytes": sum(item["bytes"] for item in rows),
                    }
                    print(json.dumps(progress), flush=True)
    assembled = assemble(plan, layout.parts_dir, final_path)
    _write_ledger(
        layout,
        rows,
        status="complete",
        started_at=started_at,
        chunk_bytes=chunk_bytes,
        workers=workers,
    )
    return {"status": "complete", "seeded_from_linear_partial": seeded, **assembled}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="private FP-Agent data root (default: $CUA_PRIVATE_ROOT/fp_agent)",
    )
    parser.add_argument(
        "--upstream-repo",
        type=Path,
        default=None,
        help="FP-Agent checkout whose README links the OSF share "
        "(default: $CUA_FP_AGENT_UPSTREAM_REPO)",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--chunk-mib", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        data_root = require_data_root(args.data_root)
        upstream_repo = require_upstream_repo(args.upstream_repo)
    except PrivatePathError as error:
        parser.error(str(error))
    result = download(
        data_root=data_root,
        upstream_repo=upstream_repo,
        workers=args.workers,
        chunk_bytes=args.chunk_mib * 1024 * 1024,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
