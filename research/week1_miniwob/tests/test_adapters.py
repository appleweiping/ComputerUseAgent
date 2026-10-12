from __future__ import annotations

import asyncio
import copy
import contextlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

from research.week1_miniwob.adapters import common
from research.week1_miniwob.adapters import browser_use_adapter
from research.week1_miniwob.adapters import browser_use_compat
from research.week1_miniwob.adapters import dispatch
from research.week1_miniwob.adapters import miniwob_source
from research.week1_miniwob.adapters.common import AdapterError, FrameworkRun
from research.week1_miniwob.protocol import build_matrix, load_manifest, validate_adapter_result
from research.week1_miniwob import runner
from research.week1_miniwob.tests import provider_entitlement_fixture
from research.week1_miniwob.runner import resolve_adapter_command


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
# The live extension is newer than the frozen 0.4.0 pin; tests that exercise
# framework filtering isolate that check (it has dedicated tests in test_checkout.py).
FROZEN_RECORDER_PASS = {"id": "recorder-extension", "status": "pass", "detail": "version 0.4.0"}


def _valid_png_bytes(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanlines = b"".join(b"\x00" + (b"\x00" * width * 4) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class AdapterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_manifest(ROOT / "benchmark_manifest.json")
        cls.attempts = build_matrix(cls.manifest)

    def attempt(self, framework: str = "browser-use") -> dict:
        return copy.deepcopy(next(row for row in self.attempts if row["framework"]["id"] == framework))

    def test_repository_default_commands_are_clone_relative_and_shell_free(self) -> None:
        for framework in self.manifest["frameworks"]:
            with mock.patch.dict(os.environ, {framework["adapter_command_env"]: ""}, clear=False):
                command, source = resolve_adapter_command(framework)
            self.assertEqual(source, "repository-default")
            self.assertEqual(command[0], sys.executable)
            self.assertEqual(len(command), 3)
            self.assertFalse(any(token in command for token in ("powershell", "cmd", "/c", "-Command")))
            self.assertTrue((REPO_ROOT / command[1]).is_file())
            self.assertEqual(command[2], framework["id"])

    def test_dependency_freeze_is_private_digest_only_and_live_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / "browser-use"
            environment.mkdir()
            python = environment / "python.exe"
            python.write_bytes(b"")
            secret_line = "private-package @ file:///Z:/PRIVATE-SENTINEL"
            normalized = f"alpha==1.0\n{secret_line}\n"
            digest = common.hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            (environment / common.DEPENDENCY_FREEZE_FILENAME).write_text(
                normalized, encoding="utf-8", newline="\n"
            )
            (environment / common.DEPENDENCY_FREEZE_DIGEST_FILENAME).write_text(
                digest + "\n", encoding="ascii", newline="\n"
            )
            completed = subprocess.CompletedProcess(
                [str(python), "-m", "pip", "freeze"], 0, normalized, ""
            )
            with mock.patch.object(
                common,
                "_dependency_environments",
                return_value=[("browser-use", environment, python)],
            ), mock.patch.object(common.subprocess, "run", return_value=completed):
                evidence = common.verify_dependency_freezes(
                    "browser-use", compare_live_environment=True
                )
            serialized = json.dumps(evidence)
            self.assertNotIn("PRIVATE-SENTINEL", serialized)
            self.assertEqual(evidence[0]["environment"], "browser-use")
            self.assertEqual(evidence[0]["artifact"]["sha256"], digest)

            drifted = subprocess.CompletedProcess(
                [str(python), "-m", "pip", "freeze"], 0, "alpha==2.0\n", ""
            )
            with mock.patch.object(
                common,
                "_dependency_environments",
                return_value=[("browser-use", environment, python)],
            ), mock.patch.object(common.subprocess, "run", return_value=drifted), self.assertRaises(
                AdapterError
            ) as caught:
                common.verify_dependency_freezes("browser-use", compare_live_environment=True)
            self.assertEqual(caught.exception.code, "dependency_environment_drift")

    def test_pinned_miniwob_source_starts_a_real_loopback_task_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            static_root = root / "miniwob" / "html"
            html_root = static_root / "miniwob"
            html_root.mkdir(parents=True)
            for task_file in miniwob_source.TASK_FILES:
                (html_root / task_file).write_text(
                    '<!doctype html><html><script src="../core/core.js"></script>'
                    '<script src="../common/ui_utils.js"></script><body>MiniWoB</body></html>',
                    encoding="utf-8",
                )
            for asset_file in miniwob_source.REQUIRED_ASSET_FILES:
                asset = static_root / asset_file
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_text(f"/* pinned test asset: {asset_file} */", encoding="utf-8")
            tree_hash = miniwob_source.html_tree_sha256(html_root)
            static_tree_hash = miniwob_source.html_tree_sha256(static_root)
            (root / ".cui-week1-source.json").write_text(
                json.dumps(
                    {
                        "schema_version": miniwob_source.SOURCE_MARKER_SCHEMA,
                        "commit": miniwob_source.PINNED_COMMIT,
                        "html_tree_sha256": tree_hash,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(miniwob_source.MiniwobSourceError, "expected pinned"):
                miniwob_source.verify_source_root(root)
            with mock.patch.object(
                miniwob_source, "PINNED_HTML_TREE_SHA256", tree_hash
            ), mock.patch.object(
                miniwob_source, "PINNED_STATIC_TREE_SHA256", static_tree_hash
            ):
                with miniwob_source.PinnedMiniwobServer(root) as server:
                    self.assertTrue(server.url.startswith("http://127.0.0.1:"))
                    self.assertTrue(server.url.endswith("/miniwob/"))
                    self.assertEqual(server.source["commit"], miniwob_source.PINNED_COMMIT)
                    from urllib.request import urlopen

                    with urlopen(server.url + "../core/core.js", timeout=3) as response:
                        self.assertEqual(
                            response.read(),
                            (static_root / "core" / "core.js").read_bytes(),
                        )
                    with miniwob_source.VerifiedMiniwobEndpoint(root, server.url) as endpoint:
                        self.assertEqual(endpoint.url, server.url)
                core_asset = static_root / "core" / "core.js"
                original_core = core_asset.read_text(encoding="utf-8")
                core_asset.write_text("/* tampered shared core */", encoding="utf-8")
                with self.assertRaisesRegex(
                    miniwob_source.MiniwobSourceError, "static asset tree hash"
                ):
                    miniwob_source.verify_source_root(root)
                core_asset.write_text(original_core, encoding="utf-8")
                (html_root / miniwob_source.TASK_FILES[0]).write_text(
                    "<!doctype html><html>tampered</html>", encoding="utf-8"
                )
                with self.assertRaisesRegex(miniwob_source.MiniwobSourceError, "differs"):
                    miniwob_source.verify_source_root(root)

    def test_pinned_miniwob_server_fails_closed_when_shared_assets_are_not_served(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            static_root = Path(temporary)
            html_root = static_root / "miniwob"
            html_root.mkdir()
            for task_file in miniwob_source.TASK_FILES:
                (html_root / task_file).write_text("<!doctype html>", encoding="utf-8")
            for asset_file in miniwob_source.REQUIRED_ASSET_FILES:
                asset = static_root / asset_file
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_text("asset", encoding="utf-8")
            source = {"html_root": html_root, "static_root": static_root}
            with mock.patch.object(
                miniwob_source, "urlopen", side_effect=OSError("404 core.js")
            ):
                with self.assertRaisesRegex(
                    miniwob_source.MiniwobSourceError, "could not load"
                ):
                    miniwob_source._verify_endpoint_resources(
                        source,
                        "http://127.0.0.1:1234/miniwob/",
                        timeout_seconds=1,
                        user_agent="test",
                    )

    def test_orchestrator_python_does_not_gate_isolated_framework_packages(self) -> None:
        diagnostic = json.dumps(
            {
                "ready": True,
                "checks": [
                    {"id": "miniwob-pinned-source", "status": "pass", "detail": "pinned"},
                    {"id": "miniwob-loopback", "status": "pass", "detail": "reachable"},
                ],
            }
        )

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["git", "status"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 0, diagnostic, "")

        with mock.patch.object(runner.shutil, "which", side_effect=lambda name: name), mock.patch.object(
            runner.subprocess, "run", side_effect=fake_run
        ), mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": "present"}, clear=False
        ), mock.patch.object(runner, "recorder_extension_check", return_value=FROZEN_RECORDER_PASS):
            preflight = runner.run_preflight(self.manifest, framework_ids={"browser-use"})
        check_ids = {row["id"] for row in preflight["checks"]}
        self.assertNotIn("gymnasium", check_ids)
        self.assertNotIn("browsergym-miniwob", check_ids)
        self.assertTrue(preflight["ready"])

    def test_filtered_preflight_checks_only_the_selected_framework(self) -> None:
        diagnostic = json.dumps(
            {
                "ready": True,
                "checks": [
                    {"id": "miniwob-pinned-source", "status": "pass", "detail": "pinned"},
                    {"id": "miniwob-loopback", "status": "pass", "detail": "reachable"},
                ],
            }
        )
        adapter_calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["git", "status"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            adapter_calls.append(command)
            return subprocess.CompletedProcess(command, 0, diagnostic, "")

        with mock.patch.object(runner.shutil, "which", side_effect=lambda name: name), mock.patch.object(
            runner.subprocess, "run", side_effect=fake_run
        ), mock.patch.dict(os.environ, {"OPENAI_API_KEY": "present"}, clear=False), mock.patch.object(
            runner, "recorder_extension_check", return_value=FROZEN_RECORDER_PASS
        ):
            preflight = runner.run_preflight(self.manifest, framework_ids={"browser-use"})
        self.assertTrue(preflight["ready"])
        self.assertEqual(preflight["framework_scope"], ["browser-use"])
        self.assertEqual(len(adapter_calls), 1)
        self.assertIn("browser-use", adapter_calls[0])
        selected = [self.attempt("browser-use")]
        labeled = runner.label_partial_attempts(selected, "week2-e2e")
        self.assertEqual(labeled[0]["formal_attempt_id"], selected[0]["attempt_id"])
        self.assertTrue(labeled[0]["attempt_id"].startswith("partial__week2-e2e__"))
        self.assertEqual(selected[0]["attempt_id"], self.attempt("browser-use")["attempt_id"])

    def test_skyvern_preflight_uses_embedded_local_bridge_not_service_credentials(self) -> None:
        from research.week1_miniwob.adapters import skyvern_adapter

        local = {
            "ready": True,
            "checks": [{"id": "embedded-lifecycle", "status": "pass", "detail": "closed"}],
        }
        with mock.patch.object(skyvern_adapter, "bridge_self_test", return_value=local):
            payload = common.common_diagnostics("skyvern")
        check_ids = {row["id"] for row in payload["checks"]}
        self.assertIn("skyvern-local-embedded-lifecycle", check_ids)
        self.assertNotIn("skyvern-base-url", check_ids)
        self.assertNotIn("skyvern-service-health", check_ids)
        self.assertNotIn("skyvern-api-key", check_ids)

    def test_environment_override_remains_an_argv_array(self) -> None:
        framework = self.manifest["frameworks"][0]
        override = [sys.executable, "custom.py", "--flag"]
        with mock.patch.dict(os.environ, {framework["adapter_command_env"]: json.dumps(override)}):
            command, source = resolve_adapter_command(framework)
        self.assertEqual(command, override)
        self.assertTrue(source.startswith("environment:"))

    def test_attempt_validation_fails_closed_on_protocol_drift(self) -> None:
        attempt = self.attempt()
        common.validate_attempt(attempt, "browser-use")
        for path, value, message in (
            (("model",), "another-model", "model"),
            (("limits", "max_actions"), 31, "30 actions"),
            (("browser", "viewport"), {"width": 1280, "height": 720}, "332x214"),
            (("browser", "product_version"), "150.0.0.0", "151.0.7922.108"),
            (("task", "environment_id"), "browsergym/miniwob.click-button", "unsupported"),
        ):
            changed = copy.deepcopy(attempt)
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.assertRaisesRegex(AdapterError, message):
                common.validate_attempt(changed, "browser-use")

    def test_diagnostics_never_echo_secret_values(self) -> None:
        sentinel = "DO-NOT-PRINT-THIS-SECRET"
        from research.week1_miniwob.adapters import skyvern_adapter

        local = {"ready": False, "checks": []}
        with mock.patch.object(skyvern_adapter, "bridge_self_test", return_value=local), mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": sentinel, "SKYVERN_API_KEY": sentinel},
            clear=False,
        ):
            payload = common.common_diagnostics("skyvern")
        serialized = json.dumps(payload)
        self.assertNotIn(sentinel, serialized)
        self.assertFalse(payload["secrets_printed"])
        self.assertTrue(payload["writes_performed"])
        self.assertFalse(payload["repository_writes_performed"])
        self.assertTrue(payload["private_state_may_change"])

    def test_compact_action_history_drops_reasoning_and_page_text(self) -> None:
        rows = common.compact_actions(
            [{"type": "click", "action": "click button", "reasoning": "private chain", "pageText": "entire page"}]
        )
        self.assertEqual(rows, [{"index": 1, "type": "click", "fields": ["type"]}])

    def test_browser_host_refuses_a_stale_profile(self) -> None:
        attempt = self.attempt()
        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)
            common.BrowserHost(attempt, private_root)
            with self.assertRaisesRegex(AdapterError, "private artifact directory already exists"):
                common.BrowserHost(attempt, private_root)
        second = self.attempt()
        second["attempt_id"] = f"{second['attempt_id']}-profile-only"
        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)
            (private_root / "profiles" / second["attempt_id"]).mkdir(parents=True)
            with self.assertRaisesRegex(AdapterError, "profile path already exists"):
                common.BrowserHost(second, private_root)

    def test_browsergym_timer_contract_is_explicitly_milliseconds(self) -> None:
        class MillisecondTask:
            def __init__(self, episode_max_time: int = 1_000_000) -> None:
                """episode max time in milliseconds"""

        class SecondsTask:
            def __init__(self, episode_max_time: int = 1_000_000) -> None:
                """episode max time in seconds"""

        common.assert_miniwob_timer_is_milliseconds(MillisecondTask)
        with self.assertRaisesRegex(AdapterError, "millisecond"):
            common.assert_miniwob_timer_is_milliseconds(SecondsTask)

    def test_async_driver_worker_is_safe_inside_a_running_event_loop(self) -> None:
        caller_thread = threading.get_ident()

        async def inner() -> str:
            self.assertIsNotNone(asyncio.get_running_loop())
            self.assertNotEqual(threading.get_ident(), caller_thread)
            return threading.current_thread().name

        async def caller() -> str:
            caller_loop = asyncio.get_running_loop()
            result = common.run_coroutine_in_worker(inner)
            self.assertIs(asyncio.get_running_loop(), caller_loop)
            return result

        self.assertTrue(asyncio.run(caller()).startswith("cui-agent-driver"))
        self.assertFalse(
            any(thread.name.startswith("cui-agent-driver") for thread in threading.enumerate())
        )

    def test_async_driver_worker_propagates_exceptions_and_stops_thread(self) -> None:
        async def raising() -> None:
            raise RuntimeError("worker-exception-sentinel")

        with self.assertRaisesRegex(RuntimeError, "worker-exception-sentinel"):
            common.run_coroutine_in_worker(raising)
        self.assertFalse(
            any(thread.name.startswith("cui-agent-driver") for thread in threading.enumerate())
        )

    def test_browser_use_external_cdp_patch_skips_only_known_call_and_restores(self) -> None:
        class FakeWebSocket:
            def __init__(self) -> None:
                self.sent = 0

            async def send(self, message: str) -> None:
                self.sent += 1
                if json.loads(message).get("sessionId") == "raise":
                    raise RuntimeError("resume-exception-sentinel")

        class FakeCDPClient:
            def __init__(self) -> None:
                self.msg_id = 0
                self.pending_requests: dict[int, asyncio.Future] = {}
                self.ws = FakeWebSocket()

        class FakeRuntimeClient:
            def __init__(self) -> None:
                self._client = FakeCDPClient()

            async def runIfWaitingForDebugger(
                self, params: None = None, session_id: str | None = None
            ) -> dict:
                raise AssertionError("the exact bounded sender must replace this method")

        class FakeBrowserSession:
            def __init__(self, runtime: FakeRuntimeClient) -> None:
                self.runtime = runtime

            async def get_or_create_cdp_session(
                self, target_id: str | None = None, focus: bool = True
            ) -> dict:
                return await self.runtime.runIfWaitingForDebugger(session_id=target_id)

        class FakeScreenshotWatchdog:
            async def on_ScreenshotEvent(self, _event: object) -> str:
                return "screenshot"

        original_get = FakeBrowserSession.get_or_create_cdp_session
        original_resume = FakeRuntimeClient.runIfWaitingForDebugger
        original_screenshot = FakeScreenshotWatchdog.on_ScreenshotEvent

        async def scenario() -> tuple[dict, dict, browser_use_compat.CompatibilityStats]:
            runtime = FakeRuntimeClient()
            browser = FakeBrowserSession(runtime)
            with browser_use_compat._external_cdp_patch(
                FakeBrowserSession,
                FakeRuntimeClient,
                FakeScreenshotWatchdog,
                expected_target={"target_id": "target-official", "url": "https://example.test/task"},
                timeout_seconds=0.01,
            ) as stats:
                skipped = await browser.get_or_create_cdp_session("focused-session")
                self.assertEqual(runtime._client.ws.sent, 0)
                bounded = await runtime.runIfWaitingForDebugger(session_id="event-session")
                self.assertEqual(runtime._client.ws.sent, 1)
                self.assertEqual(runtime._client.pending_requests, {})
                with self.assertRaisesRegex(RuntimeError, "resume-exception-sentinel"):
                    await runtime.runIfWaitingForDebugger(session_id="raise")
                self.assertEqual(runtime._client.ws.sent, 2)
                self.assertEqual(runtime._client.pending_requests, {})
                return skipped, bounded, stats

        skipped, bounded, stats = asyncio.run(scenario())
        self.assertEqual(skipped, {})
        self.assertEqual(bounded, {})
        self.assertEqual(stats.skipped_unconditional_resumes, 1)
        self.assertEqual(stats.bounded_resume_timeouts, 1)
        self.assertIs(FakeBrowserSession.get_or_create_cdp_session, original_get)
        self.assertIs(FakeRuntimeClient.runIfWaitingForDebugger, original_resume)
        self.assertIs(FakeScreenshotWatchdog.on_ScreenshotEvent, original_screenshot)

    def test_screenshot_watchdog_patch_normalizes_and_reattests_exact_target(self) -> None:
        expected = {
            "target_id": "target-official",
            "url": "https://example.test/task",
        }

        class Target:
            target_id = expected["target_id"]
            target_type = "page"
            url = expected["url"]

        class RootClient:
            def __init__(self, state: str) -> None:
                self.state = state
                self.calls: list[str] = []

            async def send_raw(
                self, method: str, params: dict | None = None, session_id: str | None = None
            ) -> dict:
                self.calls.append(method)
                if method in {"Browser.getWindowForTarget", "Browser.getWindowBounds"}:
                    return {
                        "windowId": 7,
                        "bounds": {
                            "windowState": self.state,
                            "left": 10,
                            "top": 20,
                            "width": 500,
                            "height": 400,
                        },
                    }
                if method == "Browser.setWindowBounds":
                    requested = (params or {}).get("bounds") or {}
                    if "windowState" in requested:
                        self.state = requested["windowState"]
                    return {}
                if method == "Page.bringToFront":
                    self.assert_page_session = session_id
                    return {}
                if method == "Target.getTargetInfo":
                    return {
                        "targetInfo": {
                            "targetId": expected["target_id"],
                            "url": expected["url"],
                            "type": "page",
                        }
                    }
                raise AssertionError(f"unexpected safe method {method}")

        class PageSession:
            session_id = "session-official"

            def __init__(self, client: RootClient) -> None:
                self.cdp_client = client

        class FakeBrowserSession:
            def __init__(self, state: str) -> None:
                self.cdp_client = RootClient(state)
                self.target = Target()

            def get_focused_target(self) -> Target:
                return self.target

            def get_page_targets(self) -> list[Target]:
                return [self.target]

            async def get_or_create_cdp_session(
                self, target_id: str | None = None, focus: bool = True
            ) -> PageSession:
                self.assert_target = target_id
                self.assert_focus = focus
                return PageSession(self.cdp_client)

        class FakeRuntimeClient:
            async def runIfWaitingForDebugger(
                self, params: None = None, session_id: str | None = None
            ) -> dict:
                return {}

        class FakeScreenshotWatchdog:
            original_calls = 0

            def __init__(self, browser_session: FakeBrowserSession) -> None:
                self.browser_session = browser_session

            async def on_ScreenshotEvent(self, _event: object) -> str:
                type(self).original_calls += 1
                if self.browser_session.cdp_client.state != "normal":
                    raise AssertionError("original capture saw a non-normal window")
                return "captured"

        original_screenshot = FakeScreenshotWatchdog.on_ScreenshotEvent

        async def scenario(state: str) -> browser_use_compat.CompatibilityStats:
            browser = FakeBrowserSession(state)
            watchdog = FakeScreenshotWatchdog(browser)
            with browser_use_compat._external_cdp_patch(
                FakeBrowserSession,
                FakeRuntimeClient,
                FakeScreenshotWatchdog,
                expected_target=expected,
            ) as stats:
                self.assertEqual(await watchdog.on_ScreenshotEvent(object()), "captured")
                self.assertEqual(browser.cdp_client.state, "normal")
                self.assertEqual(browser.assert_target, expected["target_id"])
                self.assertTrue(browser.assert_focus)
                self.assertIn("Page.bringToFront", browser.cdp_client.calls)
                self.assertIn("Target.getTargetInfo", browser.cdp_client.calls)
                return stats

        for state in ("minimized", "maximized", "fullscreen"):
            with self.subTest(state=state):
                stats = asyncio.run(scenario(state))
                self.assertEqual(stats.screenshot_window_checks, 1)
                self.assertEqual(stats.window_normalizations, 1)
                self.assertEqual(stats.bring_to_front_calls, 1)
                self.assertIs(FakeScreenshotWatchdog.on_ScreenshotEvent, original_screenshot)

    def test_screenshot_watchdog_patch_fails_closed_on_target_drift(self) -> None:
        expected = {"target_id": "target-official", "url": "https://example.test/task"}

        class Target:
            target_id = expected["target_id"]
            target_type = "page"
            url = expected["url"]

        class RootClient:
            async def send_raw(
                self, method: str, params: dict | None = None, session_id: str | None = None
            ) -> dict:
                if method == "Browser.getWindowForTarget":
                    return {
                        "windowId": 9,
                        "bounds": {
                            "windowState": "normal",
                            "left": 0,
                            "top": 0,
                            "width": 500,
                            "height": 400,
                        },
                    }
                if method == "Page.bringToFront":
                    return {}
                if method == "Target.getTargetInfo":
                    return {
                        "targetInfo": {
                            "targetId": expected["target_id"],
                            "url": "https://example.test/drifted",
                            "type": "page",
                        }
                    }
                raise AssertionError(f"unexpected safe method {method}")

        class PageSession:
            session_id = "session-official"

            def __init__(self, client: RootClient) -> None:
                self.cdp_client = client

        class FakeBrowserSession:
            def __init__(self) -> None:
                self.cdp_client = RootClient()
                self.target = Target()

            def get_focused_target(self) -> Target:
                return self.target

            def get_page_targets(self) -> list[Target]:
                return [self.target]

            async def get_or_create_cdp_session(
                self, target_id: str | None = None, focus: bool = True
            ) -> PageSession:
                return PageSession(self.cdp_client)

        class FakeRuntimeClient:
            async def runIfWaitingForDebugger(
                self, params: None = None, session_id: str | None = None
            ) -> dict:
                return {}

        class FakeScreenshotWatchdog:
            original_calls = 0

            def __init__(self, browser_session: FakeBrowserSession) -> None:
                self.browser_session = browser_session

            async def on_ScreenshotEvent(self, _event: object) -> str:
                type(self).original_calls += 1
                return "must-not-run"

        async def scenario() -> None:
            watchdog = FakeScreenshotWatchdog(FakeBrowserSession())
            with browser_use_compat._external_cdp_patch(
                FakeBrowserSession,
                FakeRuntimeClient,
                FakeScreenshotWatchdog,
                expected_target=expected,
            ):
                with self.assertRaisesRegex(
                    browser_use_compat.BrowserUseCompatibilityError,
                    "target changed",
                ):
                    await watchdog.on_ScreenshotEvent(object())

        asyncio.run(scenario())
        self.assertEqual(FakeScreenshotWatchdog.original_calls, 0)

    def test_browser_use_patch_rejects_concurrent_installation(self) -> None:
        class FakeBrowserSession:
            async def get_or_create_cdp_session(
                self, target_id: str | None = None, focus: bool = True
            ) -> object:
                return object()

        class FakeRuntimeClient:
            async def runIfWaitingForDebugger(
                self, params: None = None, session_id: str | None = None
            ) -> dict:
                return {}

        class FakeScreenshotWatchdog:
            async def on_ScreenshotEvent(self, _event: object) -> str:
                return "captured"

        expected = {"target_id": "target-official", "url": "https://example.test/task"}
        with browser_use_compat._external_cdp_patch(
            FakeBrowserSession,
            FakeRuntimeClient,
            FakeScreenshotWatchdog,
            expected_target=expected,
        ):
            with self.assertRaisesRegex(
                browser_use_compat.BrowserUseCompatibilityError,
                "already active",
            ):
                with browser_use_compat._external_cdp_patch(
                    FakeBrowserSession,
                    FakeRuntimeClient,
                    FakeScreenshotWatchdog,
                    expected_target=expected,
                ):
                    self.fail("concurrent compatibility patch unexpectedly installed")

    def test_browser_use_compat_source_guard_rejects_drift(self) -> None:
        def sample(value: int = 1) -> int:
            return value

        source_file = Path(__file__).name
        signature = str(browser_use_compat.inspect.signature(sample))
        source_hash = browser_use_compat._source_sha256(sample)
        exact = browser_use_compat.SourceGuard(source_file, signature, source_hash)
        verified = browser_use_compat._verify_callable("sample", sample, exact)
        self.assertEqual(verified["source_sha256"], source_hash)
        drifted = browser_use_compat.SourceGuard(source_file, signature, "0" * 64)
        with self.assertRaisesRegex(browser_use_compat.BrowserUseCompatibilityError, "source hash changed"):
            browser_use_compat._verify_callable("sample", sample, drifted)
        screenshot_guard = browser_use_compat.SOURCE_GUARDS[
            "ScreenshotWatchdog.on_ScreenshotEvent"
        ]
        self.assertEqual(
            screenshot_guard.module_suffix,
            "browser_use/browser/watchdogs/screenshot_watchdog.py",
        )
        self.assertEqual(
            screenshot_guard.source_sha256,
            "2e64a87fd57d7d399ed099cc712ccb62f59d8c55b83c5f114d18e63424d21063",
        )

    def test_resume_timeout_cleanup_preserves_concurrent_screenshot_request(self) -> None:
        async def scenario() -> None:
            loop = asyncio.get_running_loop()
            old_request = loop.create_future()
            concurrent_screenshot = loop.create_future()

            class FakeClient:
                def __init__(self) -> None:
                    self.msg_id = 10
                    self.pending_requests = {10: old_request}
                    self.ws = FakeWebSocket(self)

            class FakeWebSocket:
                def __init__(self, client: FakeClient) -> None:
                    self.client = client

                async def send(self, _message: str) -> None:
                    # This request races after the resume request has reserved
                    # ID 11. It must remain live when only ID 11 times out.
                    self.client.msg_id += 1
                    self.client.pending_requests[self.client.msg_id] = concurrent_screenshot

            client = FakeClient()
            result, cleaned = await browser_use_compat._bounded_exact_resume_send(
                client,
                params=None,
                session_id="session-test",
                timeout_seconds=0.01,
            )
            self.assertIsNone(result)
            self.assertTrue(cleaned)
            self.assertEqual(set(client.pending_requests), {10, 12})
            self.assertFalse(concurrent_screenshot.cancelled())
            self.assertFalse(concurrent_screenshot.done())

            # Identity mismatch fails closed rather than deleting a request
            # that reused the same numeric ID for a different future.
            replacement = loop.create_future()
            client.pending_requests[13] = replacement
            self.assertFalse(
                browser_use_compat._cleanup_exact_pending_request(
                    client, 13, loop.create_future()
                )
            )
            self.assertIs(client.pending_requests[13], replacement)

        asyncio.run(scenario())

    def test_browser_use_framework_stdout_is_reserved_for_protocol_json(self) -> None:
        sentinel = "BROWSER-USE-LOG-SENTINEL"

        async def noisy_run(*_args: object) -> FrameworkRun:
            print(sentinel)
            return FrameworkRun([], 0, False)

        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()
        with mock.patch.object(browser_use_adapter, "_run", noisy_run), contextlib.redirect_stdout(
            captured_stdout
        ), contextlib.redirect_stderr(captured_stderr):
            outcome = browser_use_adapter.run(self.attempt(), "task", "ws://127.0.0.1/test")
        self.assertIsInstance(outcome, FrameworkRun)
        self.assertEqual(captured_stdout.getvalue(), "")
        self.assertIn(sentinel, captured_stderr.getvalue())

    def test_raising_driver_still_stops_recorder_and_retains_private_evidence(self) -> None:
        attempt = self.attempt()
        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)

            class FakeServer:
                url = "http://127.0.0.1:8123/"

                def __enter__(self) -> "FakeServer":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

            class FakeHost:
                cdp_url = "ws://127.0.0.1/devtools/browser/test"
                browser_version = "Chrome/151.0.7922.108"
                environment_guard = {
                    "requested_locale": "en-US",
                    "requested_timezone": "UTC",
                    "observed_locale": "en-US",
                    "observed_timezone": "UTC",
                    "capture_stability_verified": True,
                    "capture_stability_switch_count": 3,
                    "native_window_occlusion_disabled": True,
                    "verified": True,
                }

                def __init__(self, _attempt: dict, root: Path):
                    self.attempt_root = root / "week1" / attempt["attempt_id"]
                    self.profile_root = root / "profiles" / attempt["attempt_id"]
                    self.attempt_root.mkdir(parents=True, exist_ok=True)
                    self.profile_root.mkdir(parents=True, exist_ok=True)
                    self.episode_guard_baseline = {
                        "target_id_verified": True,
                        "loader_id_sha256": "a" * 64,
                        "document_token_sha256": "b" * 64,
                        "time_origin_ms": 1000,
                        "episode_started_at_ms": 1100,
                        "viewport": {"width": 332, "height": 214},
                        "device_pixel_ratio": 1,
                        "locale": "en-US",
                        "timezone": "UTC",
                    }
                    self.episode_guard_final = copy.deepcopy(self.episode_guard_baseline)
                    self.episode_guard_attestations = []

                def __enter__(self) -> "FakeHost":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def start_recorder(self, _instruction: str) -> dict:
                    return {"session_id": "session-test"}

                def set_recorder_step(self, step_id: str | None) -> dict:
                    return {"step_id": step_id}

                def arm_episode_guard(self, _target: dict) -> dict:
                    return {"armed": True, "baseline": self.episode_guard_baseline}

                def assert_episode_guard(self) -> dict:
                    result = {
                        "stable": True,
                        "current": self.episode_guard_final,
                        "attestation_sequence": len(self.episode_guard_attestations) + 1,
                    }
                    self.episode_guard_attestations.append(result)
                    return result

                def disarm_episode_guard(self, *, require_stable: bool = False) -> dict:
                    return {
                        "disarmed": True,
                        "violation_count": 0,
                        "final_attestation_sequence": len(self.episode_guard_attestations),
                    }

                def stop_recorder(self) -> dict:
                    trace = json.loads(
                        (REPO_ROOT / "examples" / "traces" / "minimal-session-v2.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    trace["metadata"]["session_id"] = "session-test"
                    trace["metadata"]["scope_origin"] = "http://127.0.0.1:8123"
                    trace["metadata"]["actor_type"] = "agent"
                    trace["metadata"]["source_class"] = "cui_agent"
                    trace["events"][0]["url"] = (
                        "http://127.0.0.1:8123/miniwob/click-checkboxes.html"
                    )
                    trace["events"][0]["viewport"] = {
                        "width": 332,
                        "height": 214,
                        "device_pixel_ratio": 1,
                    }
                    trace["events"][0]["type"] = "resize"
                    trace["events"][0]["is_trusted"] = True
                    trace["summary"]["by_type"] = {"resize": 1}
                    (self.attempt_root / "recorder_trace.json").write_text(
                        json.dumps(trace), encoding="utf-8"
                    )
                    return {
                        "session_id": "session-test",
                        "status": "complete",
                        "event_count": 1,
                    }

            class FakeEpisode:
                instruction = "private generated task text"
                initial_info = {"seeded": True}
                official_target = {
                    "target_id": "target-official",
                    "url": "http://127.0.0.1:8123/miniwob/click-checkboxes.html",
                }

                def __init__(self, *_args: object):
                    self.host = _args[1]

                def __enter__(self) -> "FakeEpisode":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def validate(self) -> tuple[float, bool, dict]:
                    return 0.0, False, {"message": "not complete"}

                def save_final_screenshot(self, destination: Path) -> None:
                    destination.write_bytes(_valid_png_bytes(332, 214))

            def raising_driver(*_args: object) -> FrameworkRun:
                raise RuntimeError("driver sentinel failure")

            def framework_failure_driver(*_args: object) -> FrameworkRun:
                return FrameworkRun(
                    action_history=[],
                    action_count=0,
                    completed=False,
                    error="private upstream provider response must not be published",
                    artifacts={"framework_step_count": 4, "framework_error_count": 4},
                )

            captured_stderr = io.StringIO()
            dependency_descriptor = {
                "artifact_id": "private-dependency-freeze-0123456789abcdef",
                "retained_private": True,
                "bytes": 128,
                "sha256": "c" * 64,
            }
            with mock.patch.object(common, "require_fast_attempt_ready"), mock.patch.object(
                common,
                "verify_dependency_freezes",
                return_value=[{"environment": "browser-use", "artifact": dependency_descriptor}],
            ), mock.patch.object(common, "PinnedMiniwobServer", return_value=FakeServer()
            ), mock.patch.object(common, "BrowserHost", FakeHost), mock.patch.object(
                common, "OfficialMiniwobEpisode", FakeEpisode
            ), mock.patch.dict(
                os.environ, {"CUI_WEEK1_PRIVATE_ROOT": str(private_root)}, clear=False
            ), contextlib.redirect_stderr(captured_stderr):
                result = common.run_formal_adapter(attempt, "browser-use", raising_driver)
                with mock.patch.object(
                    common,
                    "validate_formal_trace",
                    side_effect=AdapterError(
                        "recorder_trace_invalid",
                        "private trace validation detail must not be published",
                    ),
                ):
                    framework_failure_result = common.run_formal_adapter(
                        attempt, "browser-use", framework_failure_driver
                    )

            validate_adapter_result(result, attempt)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], "framework_driver_exception")
            self.assertEqual(result["recorder"]["status"], "complete")
            self.assertEqual(result["recorder"]["event_count"], 1)
            self.assertTrue(result["recorder"]["trace_artifact"]["retained_private"])
            self.assertTrue(result["artifacts"]["final_screenshot"]["retained_private"])
            self.assertTrue(result["provenance_complete"])
            self.assertFalse(result["artifact_bundle_complete"])
            self.assertFalse(result["formal_matrix_eligible"])
            self.assertIn("driver sentinel failure", captured_stderr.getvalue())
            self.assertNotIn(str(private_root), json.dumps(result))
            validate_adapter_result(framework_failure_result, attempt)
            self.assertEqual(framework_failure_result["status"], "error")
            self.assertEqual(framework_failure_result["error"]["code"], "framework_error")
            self.assertEqual(
                framework_failure_result["error"]["secondary_codes"],
                ["recorder_trace_invalid"],
            )
            serialized_failure = json.dumps(framework_failure_result)
            self.assertNotIn("upstream provider response", serialized_failure)
            self.assertNotIn("trace validation detail", serialized_failure)

    def test_error_result_is_contract_valid_and_does_not_claim_reward(self) -> None:
        attempt = self.attempt()
        result = common.error_result(attempt, "browser-use", AdapterError("missing", "dependency missing"))
        validate_adapter_result(result, attempt)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["validator"]["reward"])
        self.assertFalse(result["provenance_complete"])

    def test_dispatcher_missing_environment_returns_valid_error_artifact(self) -> None:
        attempt = self.attempt("skyvern")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = dispatch.missing_result(json.dumps(attempt), "skyvern", "environment missing")
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        validate_adapter_result(result, attempt)
        self.assertEqual(result["error"]["code"], "isolated_environment_missing")
        self.assertIsNone(result["validator"]["reward"])

    def test_public_attempt_record_excludes_private_paths_stderr_and_page_text(self) -> None:
        attempt = self.attempt()
        sentinel = "PAGE-TEXT-SENTINEL-DO-NOT-PUBLISH"
        raw_result = common.error_result(attempt, "browser-use", AdapterError("framework_failed", sentinel))
        raw_result["agent"]["action_history"] = [
            {"type": "click", "description": sentinel, "pageText": sentinel, "path": r"Z:\private\trace.json"}
        ]
        raw_result["validator"]["task_info"] = {"message": sentinel}
        raw_result["recorder"]["trace_path"] = r"Z:\private\trace.json"
        raw_result["artifacts"] = {"framework": {"message": sentinel, "path": r"Z:\private\agent.log"}}
        sanitized_candidate = copy.deepcopy(raw_result)
        sanitized_candidate["recorder"].pop("trace_path")
        sanitized = runner.public_adapter_result(sanitized_candidate)
        self.assertNotIn(sentinel, json.dumps(sanitized))
        self.assertNotIn(r"Z:\private", json.dumps(sanitized))
        completed = subprocess.CompletedProcess(
            ["python"], 0, json.dumps(raw_result), f"{sentinel}\nZ:\\private\\agent.log\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_root = root / "private"
            public_root = root / "public"
            entitlement, _ = provider_entitlement_fixture()
            with mock.patch.object(runner, "_run_adapter_process", return_value=completed), mock.patch.dict(
                os.environ, {"CUI_WEEK1_PRIVATE_ROOT": str(private_root)}, clear=False
            ):
                record = runner.execute_attempt(
                    attempt,
                    public_root,
                    source_provenance={
                        "commit": "c" * 40,
                        "clean": True,
                        "manifest_sha256": attempt["manifest_sha256"],
                    },
                    provider_entitlement=entitlement,
                )
            serialized = json.dumps(record)
            self.assertNotIn(sentinel, serialized)
            self.assertNotIn(str(private_root), serialized)
            self.assertNotIn("trace_path", serialized)
            self.assertNotIn('"events"', serialized)
            private_logs = list((private_root / "week1" / "orchestrator_logs").glob("*.stderr.log"))
            self.assertEqual(len(private_logs), 1)
            self.assertIn(sentinel, private_logs[0].read_text(encoding="utf-8"))
            public_file = public_root / "attempts" / f"{attempt['attempt_id']}.json"
            self.assertEqual(json.loads(public_file.read_text(encoding="utf-8")), record)

    def test_requirements_split_the_known_rich_conflict(self) -> None:
        browser_use = (ROOT / "adapters" / "requirements-browser-use.txt").read_text(encoding="utf-8")
        skyvern = (ROOT / "adapters" / "requirements-skyvern.txt").read_text(encoding="utf-8")
        aggregate = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("browser-use==0.11.5", browser_use)
        self.assertIn("pydantic-settings==2.14.2", browser_use)
        self.assertNotIn("skyvern", browser_use)
        self.assertIn("skyvern[local]==1.0.47", skyvern)
        self.assertIn("playwright==1.62.0", skyvern)
        self.assertNotIn("requirements-common", skyvern)
        self.assertNotIn("browser-use", skyvern)
        self.assertNotIn("browser-use==", aggregate)
        self.assertNotIn("skyvern==", aggregate)

    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_node_bridge_sources_parse(self) -> None:
        node = shutil.which("node")
        assert node
        for source in (common.NODE_HOST,):
            completed = subprocess.run(
                [node, "--check", str(source)],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        content_source = (REPO_ROOT / "extension" / "app" / "src" / "content.js").read_text(
            encoding="utf-8"
        )
        host_source = common.NODE_HOST.read_text(encoding="utf-8")
        self.assertIn('const INDICATOR_ID = "cui-trace-recorder-indicator"', content_source)
        self.assertIn('#cui-trace-recorder-indicator', host_source)
        self.assertIn("async function establishSingleTaskPage(browser)", host_source)
        self.assertIn("taskPage = await establishSingleTaskPage(browser)", host_source)
        self.assertNotIn("taskPage = await browser.newPage()", host_source)


if __name__ == "__main__":
    unittest.main()
