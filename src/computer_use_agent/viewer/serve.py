"""Serve the privacy-projected trace/result viewer on loopback only."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from computer_use_agent.viewer.privacy import build_view_state

#: Packaged static assets (read through importlib.resources, never the CWD).
ROOT: Traversable = resources.files("computer_use_agent.viewer").joinpath("static")
STATIC_FILES = MappingProxyType(
    {
        "/": ROOT.joinpath("index.html"),
        "/index.html": ROOT.joinpath("index.html"),
        "/app.js": ROOT.joinpath("app.js"),
        "/styles.css": ROOT.joinpath("styles.css"),
    }
)
#: Fixed extension -> media type map; the platform ``mimetypes`` registry is
#: deliberately not consulted (on Windows it can come from the registry).
MEDIA_TYPES = MappingProxyType(
    {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
    }
)
DEFAULT_PORT = 8765


class _ViewerServer(ThreadingHTTPServer):
    """Loopback server that refuses a port another listener already holds.

    On Windows ``SO_REUSEADDR`` (inherited from ``HTTPServer``) lets a second
    socket bind a port that is already listening: a second viewer would start
    silently while the first keeps answering its URL with other traces. Bind
    exclusively there so an in-use port raises ``OSError`` as on Linux.
    """

    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if os.name == "nt" and exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


def media_type_for(name: str) -> str:
    """Return the media type for a served asset name (``application/octet-stream`` if unknown)."""

    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return MEDIA_TYPES.get(suffix, "application/octet-stream")


def _parse_named_path(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Expected NAME=PATH")
    name, raw_path = value.split("=", 1)
    name = " ".join(name.split())
    if not name or not raw_path:
        raise argparse.ArgumentTypeError("Expected non-empty NAME=PATH")
    return name, Path(raw_path)


REQUEST_TIMEOUT_SECONDS = 30.0
MAX_DISCARDED_BODY_BYTES = 64 * 1024


def make_handler(state: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    state_bytes = (
        json.dumps(state, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    assets = {
        route: (resource.read_bytes(), media_type_for(resource.name))
        for route, resource in STATIC_FILES.items()
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "CUAViewer/1"
        # Socket timeout for each request, so a stalled client cannot hold a
        # worker thread forever.
        timeout = REQUEST_TIMEOUT_SECONDS

        def _headers(
            self, *, content_type: str, length: int, status: HTTPStatus = HTTPStatus.OK
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), clipboard-read=(), clipboard-write=()",
            )
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'; connect-src 'self'; img-src 'self' data:; "
                "script-src 'self'; style-src 'self'",
            )
            self.end_headers()

        def _plain(self, body: bytes, status: HTTPStatus) -> None:
            self._headers(content_type="text/plain; charset=utf-8", length=len(body), status=status)
            self.wfile.write(body)

        def do_GET(self) -> None:
            if not self._host_is_loopback():
                self._plain(b"Misdirected request\n", HTTPStatus.MISDIRECTED_REQUEST)
                return
            path = urlsplit(self.path).path
            if path == "/api/state":
                self._headers(
                    content_type="application/json; charset=utf-8", length=len(state_bytes)
                )
                self.wfile.write(state_bytes)
                return
            asset = assets.get(path)
            if asset is None:
                self._plain(b"Not found\n", HTTPStatus.NOT_FOUND)
                return
            body, media_type = asset
            self._headers(content_type=media_type, length=len(body))
            self.wfile.write(body)

        def _discard_body(self) -> None:
            """Read and drop a small request body before answering a rejected request.

            Closing a connection whose request body is still unread makes the OS
            send a TCP reset (always on Windows), and the client can then lose the
            response. Larger bodies are not read; the connection is just closed.
            """

            self.close_connection = True
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if 0 < length <= MAX_DISCARDED_BODY_BYTES:
                self.rfile.read(length)

        def do_POST(self) -> None:
            self._discard_body()
            if not self._host_is_loopback():
                self._plain(b"Misdirected request\n", HTTPStatus.MISDIRECTED_REQUEST)
                return
            self._plain(b"Read-only viewer\n", HTTPStatus.METHOD_NOT_ALLOWED)

        def log_message(self, format: str, *args: Any) -> None:
            # Request URLs can contain user-controlled text. The viewer exposes
            # only fixed routes, so suppress access logs by default.
            return

        def _host_is_loopback(self) -> bool:
            address = self.server.server_address
            port = address[1] if isinstance(address, tuple) else None
            host = self.headers.get("Host", "")
            accepted = {f"127.0.0.1:{port}", f"localhost:{port}"}
            return port is not None and host.lower() in accepted

    return Handler


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trace",
        action="append",
        type=_parse_named_path,
        required=True,
        help="repeatable NAME=PATH",
    )
    parser.add_argument(
        "--validator",
        action="append",
        type=_parse_named_path,
        default=[],
        help="repeatable NAME=PATH matched to a trace name",
    )
    parser.add_argument("--result", type=Path, help="cui-detector-analysis-v2 result.json")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"loopback port (default {DEFAULT_PORT}; 0 picks a free port)",
    )
    parser.add_argument("--open-browser", action="store_true")
    return parser


def serve(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    validator_pairs = args.validator
    validator_names = [name for name, _ in validator_pairs]
    if len(validator_names) != len(set(validator_names)):
        parser.error("validator names must be unique")
    validators = dict(validator_pairs)
    unknown = sorted(set(validators) - {name for name, _ in args.trace})
    if unknown:
        parser.error(f"validators do not match a trace name: {unknown}")
    try:
        state = build_view_state(args.trace, validators=validators, result_path=args.result)
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        server = _ViewerServer(("127.0.0.1", args.port), make_handler(state))
    except OSError as error:
        print(
            f"error: port {args.port} is unavailable ({error}); use --port 0 for a free port",
            file=sys.stderr,
        )
        return 1
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"Privacy-projected read-only viewer: {url}", flush=True)
    print("Press Ctrl+C to stop; no trace path or raw event payload is served.", flush=True)
    if args.open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    return serve(parser.parse_args(argv), parser)


if __name__ == "__main__":
    raise SystemExit(main())
