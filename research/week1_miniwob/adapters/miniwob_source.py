"""Pinned MiniWoB source verification and adapter-owned loopback serving."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


PINNED_COMMIT = "7fd85d71a4b60325c6585396ec4f48377d049838"
PINNED_HTML_TREE_SHA256 = "b6541e36c70d6621294edfe0ca3bae7794fb9cfb9def2829dfcda4fd284b3925"
PINNED_STATIC_TREE_SHA256 = "9883c9117804c0865920b3313021a0bd0cccc094f2f707678f6c3a72b3af4f3a"
SOURCE_MARKER_SCHEMA = "cui-week1-miniwob-source-v1"
TASK_FILES = ("click-checkboxes.html", "enter-text.html", "choose-list.html")
REQUIRED_ASSET_FILES = (
    "core/core.css",
    "core/core.js",
    "core/d3.v3.min.js",
    "common/ui_utils.js",
    "core/jquery-ui/jquery-ui.min.css",
    "core/jquery-ui/external/jquery/jquery.js",
    "core/jquery-ui/jquery-ui.min.js",
)


class MiniwobSourceError(RuntimeError):
    pass


def default_source_root() -> Path | None:
    explicit = os.environ.get("CUI_WEEK1_MINIWOB_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    private = os.environ.get("CUI_WEEK1_PRIVATE_ROOT")
    if private:
        return Path(private).expanduser().resolve() / "sources" / "miniwob-plusplus-7fd85d71"
    return None


def html_tree_sha256(html_root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in html_root.rglob("*") if path.is_file())
    if not files:
        raise MiniwobSourceError("MiniWoB html tree is empty")
    for path in files:
        relative = path.relative_to(html_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def verify_source_root(root: Path, expected_commit: str = PINNED_COMMIT) -> dict[str, Any]:
    root = root.expanduser().resolve()
    static_root = root / "miniwob" / "html"
    html_root = static_root / "miniwob"
    missing = [name for name in TASK_FILES if not (html_root / name).is_file()]
    if missing:
        raise MiniwobSourceError(f"official MiniWoB task files are missing: {', '.join(missing)}")
    missing_assets = [name for name in REQUIRED_ASSET_FILES if not (static_root / name).is_file()]
    if missing_assets:
        raise MiniwobSourceError(
            f"official MiniWoB shared assets are missing: {', '.join(missing_assets)}"
        )

    git = shutil.which("git")
    if (root / ".git").exists() and git:
        commit = subprocess.run(
            [git, "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if commit.returncode != 0:
            raise MiniwobSourceError("could not read the MiniWoB Git commit")
        observed_commit = commit.stdout.strip().lower()
        if observed_commit != expected_commit:
            raise MiniwobSourceError(f"MiniWoB commit is {observed_commit}, expected {expected_commit}")
        status = subprocess.run(
            [
                git,
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                "miniwob/html",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            raise MiniwobSourceError("MiniWoB html tree has local modifications")
        source_kind = "git_checkout"
        tree_hash = html_tree_sha256(html_root)
    else:
        marker_path = root / ".cui-week1-source.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MiniwobSourceError("source is neither a Git checkout nor a verified archive install") from error
        if marker.get("schema_version") != SOURCE_MARKER_SCHEMA or marker.get("commit") != expected_commit:
            raise MiniwobSourceError("MiniWoB archive provenance marker does not match the pinned commit")
        expected_tree = marker.get("html_tree_sha256")
        if not isinstance(expected_tree, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_tree):
            raise MiniwobSourceError("MiniWoB archive provenance marker has no valid html tree hash")
        tree_hash = html_tree_sha256(html_root)
        if tree_hash != expected_tree:
            raise MiniwobSourceError("MiniWoB html tree differs from the verified archive install")
        source_kind = "verified_github_archive"
    if tree_hash != PINNED_HTML_TREE_SHA256:
        raise MiniwobSourceError(
            f"MiniWoB html tree hash is {tree_hash}, expected pinned {PINNED_HTML_TREE_SHA256}"
        )
    static_tree_hash = html_tree_sha256(static_root)
    if static_tree_hash != PINNED_STATIC_TREE_SHA256:
        raise MiniwobSourceError(
            "MiniWoB static asset tree hash is "
            f"{static_tree_hash}, expected pinned {PINNED_STATIC_TREE_SHA256}"
        )

    return {
        "root": root,
        "static_root": static_root,
        "html_root": html_root,
        "commit": expected_commit,
        "source_kind": source_kind,
        "html_tree_sha256": tree_hash,
        "static_tree_sha256": static_tree_hash,
    }


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


class PinnedMiniwobServer:
    """Serve the verified static benchmark on an ephemeral loopback port."""

    def __init__(self, source_root: Path):
        self.source = verify_source_root(source_root)
        # Task pages live one directory below their shared ``core`` and
        # ``common`` assets.  Serving the task directory itself makes relative
        # references such as ``../core/core.js`` return 404 and leaves the
        # BrowserGym page without its required global ``core`` object.
        handler = partial(_QuietHandler, directory=str(self.source["static_root"]))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread: threading.Thread | None = None
        self.url = f"http://127.0.0.1:{self.server.server_port}/miniwob/"

    def __enter__(self) -> "PinnedMiniwobServer":
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        try:
            _verify_endpoint_resources(
                self.source,
                self.url,
                timeout_seconds=3,
                user_agent="cui-week1-loopback-self-test/1",
            )
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=3)


class VerifiedMiniwobEndpoint:
    """Validate an explicit HTTP override against the pinned local task bytes."""

    def __init__(self, source_root: Path, url: str):
        self.source = verify_source_root(source_root)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MiniwobSourceError("MINIWOB_URL override must be an HTTP(S) origin")
        self.url = url if url.endswith("/") else f"{url}/"

    def __enter__(self) -> "VerifiedMiniwobEndpoint":
        _verify_endpoint_resources(
            self.source,
            self.url,
            timeout_seconds=5,
            user_agent="cui-week1-endpoint-verifier/1",
        )
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        return None


def _verify_endpoint_resources(
    source: dict[str, Any],
    task_base_url: str,
    *,
    timeout_seconds: float,
    user_agent: str,
) -> None:
    """Byte-verify task pages and the shared assets they load.

    ``task_base_url`` is deliberately the ``.../miniwob/`` task directory.
    Shared resources therefore resolve one URL level above it, matching the
    relative paths in the pinned official HTML.
    """

    resources = [
        (task_file, source["html_root"] / task_file)
        for task_file in TASK_FILES
    ]
    resources.extend(
        (f"../{asset_file}", source["static_root"] / asset_file)
        for asset_file in REQUIRED_ASSET_FILES
    )
    for relative_url, local_path in resources:
        expected = local_path.read_bytes()
        request = Request(
            urljoin(task_base_url, relative_url),
            headers={"User-Agent": user_agent},
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                observed = response.read()
                status = int(response.getcode())
        except Exception as error:
            raise MiniwobSourceError(
                f"MiniWoB endpoint could not load {relative_url}: {type(error).__name__}: {error}"
            ) from error
        if status != 200 or hashlib.sha256(observed).digest() != hashlib.sha256(expected).digest():
            raise MiniwobSourceError(f"MiniWoB endpoint content mismatch for {relative_url}")
