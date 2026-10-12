from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from computer_use_agent.viewer.serve import _ViewerServer, make_handler


def _state():
    return {
        "schema_version": "cui-local-view-v1",
        "privacy_mode": "projected_relative_timeline_no_raw_event_payloads",
        "traces": [
            {
                "name": "fixture",
                "role": "agent",
                "task_ref": "0123456789",
                "capture_adapter": "browser_mv3",
                "status": "complete",
                "failure_reason": None,
                "event_count": 0,
                "duration_ms": 0.0,
                "modality_counts": {},
                "event_type_counts": {},
                "timeline": [],
                "validator": None,
            }
        ],
        "detector_result": None,
    }


def test_loopback_handler_is_read_only_and_hardened():
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_state()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/state", timeout=3) as response:
            payload = json.loads(response.read())
            assert payload["schema_version"] == "cui-local-view-v1"
            assert response.headers["Cache-Control"] == "no-store, max-age=0"
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]
            assert response.headers.get("Access-Control-Allow-Origin") is None
            assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
        hostile = urllib.request.Request(f"{base}/api/state", headers={"Host": "attacker.example"})
        try:
            urllib.request.urlopen(hostile, timeout=3)
        except urllib.error.HTTPError as error:
            assert error.code == 421
        else:
            raise AssertionError("DNS-rebinding-style Host header was accepted")
        # A rejected body is drained first, so the client always receives the 405
        # instead of a connection reset (Windows resets on close with unread data).
        for body in (b"x", b"x" * 32_768):
            request = urllib.request.Request(f"{base}/api/state", data=body, method="POST")
            try:
                urllib.request.urlopen(request, timeout=3)
            except urllib.error.HTTPError as error:
                assert error.code == 405
            else:
                raise AssertionError("Read-only viewer accepted POST")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_static_application_uses_no_remote_assets_or_inner_html():
    from computer_use_agent.viewer import serve

    javascript = (serve.ROOT / "app.js").read_text(encoding="utf-8")
    html = (serve.ROOT / "index.html").read_text(encoding="utf-8")
    assert "innerHTML" not in javascript
    assert 'fetch("/api/state"' in javascript
    assert "https://" not in javascript
    assert "http://" not in html and "https://" not in html


def _get(base: str, path: str) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=3) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def test_static_assets_use_the_fixed_media_type_map():
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_state()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        expected = {
            "/": "text/html; charset=utf-8",
            "/index.html": "text/html; charset=utf-8",
            "/app.js": "text/javascript; charset=utf-8",
            "/styles.css": "text/css; charset=utf-8",
        }
        for path, media_type in expected.items():
            status, headers, body = _get(base, path)
            assert status == 200, path
            assert headers["Content-Type"] == media_type
            assert body
        status, headers, _ = _get(base, "/../pyproject.toml")
        assert status == 404
        status, _, _ = _get(base, "/missing.png")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_media_type_map_is_fixed():
    from computer_use_agent.viewer.serve import media_type_for

    assert media_type_for("app.js") == "text/javascript; charset=utf-8"
    assert media_type_for("INDEX.HTML") == "text/html; charset=utf-8"
    assert media_type_for("styles.css") == "text/css; charset=utf-8"
    assert media_type_for("data.json") == "application/octet-stream"
    assert media_type_for("README") == "application/octet-stream"


def test_viewer_renders_the_documented_result_sections():
    from computer_use_agent.viewer import serve

    javascript = (serve.ROOT / "app.js").read_text(encoding="utf-8")
    html = (serve.ROOT / "index.html").read_text(encoding="utf-8")
    for field in ("bootstrap_intervals_95", "task_descriptive", "trajectory_artifact"):
        assert f"result.{field}" in javascript
    for element_id in ("bootstrap-intervals", "descriptive-table", "trajectory-binding"):
        assert f'id="{element_id}"' in html


def test_viewer_cli_rejects_bad_arguments(tmp_path, capsys):
    import pytest

    from computer_use_agent.viewer.serve import main

    trace = tmp_path / "trace.json"
    with pytest.raises(SystemExit):
        main(["--trace", "no-equals-sign"])
    with pytest.raises(SystemExit):
        main(["--trace", f"a={trace}", "--port", "70000"])
    with pytest.raises(SystemExit):
        main(["--trace", f"a={trace}", "--validator", f"b={trace}"])
    with pytest.raises(SystemExit):
        main(["--trace", f"a={trace}", "--validator", f"a={trace}", "--validator", f"a={trace}"])
    assert main(["--trace", f"a={trace}", "--port", "0"]) == 1
    assert "error:" in capsys.readouterr().err


def test_viewer_server_refuses_a_port_that_is_already_listening():
    # On Windows SO_REUSEADDR would let a second viewer bind silently while
    # the first one keeps answering the URL; the viewer must fail instead.
    first = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_state()))
    try:
        port = first.server_address[1]
        with pytest.raises(OSError):
            _ViewerServer(("127.0.0.1", port), make_handler(_state())).server_close()
    finally:
        first.server_close()


def test_viewer_cli_reports_an_unavailable_port(tmp_path, capsys):
    from computer_use_agent.viewer.serve import main

    trace = tmp_path / "trace.json"
    trace.write_text(
        (
            Path(__file__).resolve().parents[2] / "examples" / "traces" / "minimal-session-v2.json"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    first = _ViewerServer(("127.0.0.1", 0), make_handler(_state()))
    try:
        port = first.server_address[1]
        assert main(["--trace", f"a={trace}", "--port", str(port)]) == 1
        assert f"port {port} is unavailable" in capsys.readouterr().err
    finally:
        first.server_close()
