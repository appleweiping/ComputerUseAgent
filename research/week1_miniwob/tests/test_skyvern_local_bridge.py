from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from research.week1_miniwob.adapters import setup_environments
from research.week1_miniwob.adapters import skyvern_adapter
from research.week1_miniwob.adapters import skyvern_local_bridge as bridge
from research.week1_miniwob.protocol import build_matrix, load_manifest


ROOT = Path(__file__).resolve().parents[1]


class SkyvernLocalBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        manifest = load_manifest(ROOT / "benchmark_manifest.json")
        cls.attempt = copy.deepcopy(
            next(row for row in build_matrix(manifest) if row["framework"]["id"] == "skyvern")
        )
        cls.attempt["_runtime"] = {
            "official_target": {
                "target_id": "official-target",
                "url": "http://127.0.0.1:8123/miniwob/click-checkboxes.html",
            },
            "step_alignment": {
                "supported": False,
                "support_status": "not_supported",
                "hook_type": "none",
                "max_steps": 30,
            },
        }

    def request(self, operation: str = "run") -> dict:
        return skyvern_adapter._bridge_request(
            operation,
            copy.deepcopy(self.attempt),
            "Click the requested checkboxes",
            "ws://127.0.0.1:9222/devtools/browser/test",
        )

    def test_request_is_pinned_and_fails_closed_on_protocol_drift(self) -> None:
        request = self.request()
        validated = bridge.validate_request(request, "run")
        self.assertEqual(validated["model"], "gpt-5.5")
        self.assertEqual(validated["max_actions"], 30)
        self.assertEqual(validated["timeout_seconds"], 480)
        self.assertEqual(validated["seed"], 20260808)
        self.assertEqual(validated["step_alignment"]["support_status"], "not_supported")
        for key, value, code in (
            ("model", "other", "model_mismatch"),
            ("max_actions", 31, "limit_mismatch"),
            ("timeout_seconds", 481, "limit_mismatch"),
            ("seed", 1, "seed_mismatch"),
        ):
            changed = self.request()
            changed[key] = value
            with self.assertRaises(bridge.BridgeError) as caught:
                bridge.validate_request(changed, "run")
            self.assertEqual(caught.exception.code, code)

    def test_exact_target_guard_accepts_only_one_same_target(self) -> None:
        class Session:
            async def send(self, _method: str) -> dict:
                return {"targetInfo": {"targetId": "official-target"}}

            async def detach(self) -> None:
                return None

        class Page:
            url = "http://127.0.0.1:8123/miniwob/click-checkboxes.html"

        class Browser:
            def __init__(self) -> None:
                self.pages = [Page()]

            async def new_cdp_session(self, _page: Page) -> Session:
                return Session()

        expected = self.request()["official_target"]
        browser = Browser()
        observed = asyncio.run(bridge._assert_official_target(browser, expected))
        self.assertIs(observed, browser.pages[0])
        browser.pages.append(Page())
        with self.assertRaisesRegex(bridge.BridgeError, "expected one page"):
            asyncio.run(bridge._assert_official_target(browser, expected))

    def test_guard_detects_url_drift_without_generating_input(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.url = "http://127.0.0.1:8123/miniwob/click-checkboxes.html"

        class Browser:
            def __init__(self, page: Page) -> None:
                self.pages = [page]

        async def scenario() -> None:
            page = Page()
            browser = Browser(page)
            stop = asyncio.Event()
            with mock.patch.object(bridge, "TARGET_GUARD_INTERVAL_SECONDS", 0.001):
                task = asyncio.create_task(
                    bridge._target_guard(browser, page, self.request()["official_target"], stop)
                )
                await asyncio.sleep(0)
                page.url = "http://127.0.0.1:8123/miniwob/other.html"
                with self.assertRaises(bridge.BridgeError) as caught:
                    await asyncio.wait_for(task, timeout=0.1)
            self.assertEqual(caught.exception.code, "official_target_changed")

        asyncio.run(scenario())

    def test_framework_operation_is_raced_against_target_guard_and_deadline(self) -> None:
        async def scenario() -> None:
            never = asyncio.Event()
            guard = asyncio.create_task(asyncio.sleep(0.001))
            with self.assertRaisesRegex(bridge.BridgeError, "guard stopped"):
                await bridge._await_or_target_guard(never.wait(), guard, timeout=1)

            guard = asyncio.create_task(never.wait())
            try:
                with self.assertRaises(TimeoutError):
                    await bridge._await_or_target_guard(never.wait(), guard, timeout=0.001)
            finally:
                guard.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await guard

        asyncio.run(scenario())

    def test_bridge_invocation_is_shell_free_private_and_timeout_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)
            fake_python = private_root / "environments" / "skyvern-local" / "Scripts" / "python.exe"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_bytes(b"")
            timeout_error = subprocess.TimeoutExpired(
                cmd=[str(fake_python)], timeout=500, stderr=b"private framework log"
            )
            with mock.patch.object(skyvern_adapter, "_skyvern_python", return_value=fake_python), mock.patch.object(
                skyvern_adapter.subprocess, "run", side_effect=timeout_error
            ) as invoked, mock.patch.dict(
                os.environ, {"CUI_WEEK1_PRIVATE_ROOT": str(private_root)}, clear=False
            ):
                result = skyvern_adapter._invoke_bridge(self.request(), timeout=500)
            self.assertTrue(result["timed_out"])
            args, kwargs = invoked.call_args
            self.assertEqual(args[0][:3], [str(fake_python), "-X", "utf8"])
            self.assertFalse(kwargs.get("shell", False))
            self.assertTrue(Path(kwargs["cwd"]).is_relative_to(private_root.resolve()))
            self.assertEqual(kwargs["env"]["PYTHONUTF8"], "1")
            self.assertTrue(Path(kwargs["env"]["TMP"]).is_relative_to(private_root.resolve()))

    def test_self_test_runs_from_private_cwd_and_detects_repository_writes(self) -> None:
        payload = {
            "schema_version": "cui-week1-skyvern-local-self-test-v1",
            "ready": True,
            "checks": [{"id": "embedded-lifecycle", "status": "pass", "detail": "closed"}],
            "repository_writes_performed": False,
            "private_state_may_change": True,
            "writes_performed": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)
            fake_python = private_root / "environments" / "skyvern-local" / "Scripts" / "python.exe"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_bytes(b"")
            completed = subprocess.CompletedProcess([str(fake_python)], 0, json.dumps(payload), "")
            with mock.patch.object(skyvern_adapter, "_skyvern_python", return_value=fake_python), mock.patch.object(
                skyvern_adapter, "_repository_state", side_effect=[(("a", 1, 1),), (("a", 1, 1),)]
            ), mock.patch.object(
                skyvern_adapter.subprocess, "run", return_value=completed
            ) as invoked, mock.patch.dict(
                os.environ, {"CUI_WEEK1_PRIVATE_ROOT": str(private_root)}, clear=False
            ):
                result = skyvern_adapter.bridge_self_test()
            self.assertTrue(result["ready"])
            self.assertFalse(result["repository_writes_performed"])
            self.assertTrue(result["private_state_may_change"])
            self.assertTrue(result["writes_performed"])
            _args, kwargs = invoked.call_args
            self.assertTrue(Path(kwargs["cwd"]).is_relative_to(private_root.resolve()))
            self.assertEqual(kwargs["timeout"], 120)

    def test_setup_uses_separate_orchestrator_and_skyvern_local_environments(self) -> None:
        self.assertEqual(
            setup_environments.FRAMEWORK_ENVIRONMENTS["skyvern"],
            ("orchestrator", "skyvern-local"),
        )
        skyvern_requirements = setup_environments.ENVIRONMENT_REQUIREMENTS["skyvern-local"].read_text(
            encoding="utf-8"
        )
        orchestrator_requirements = setup_environments.ENVIRONMENT_REQUIREMENTS["orchestrator"].read_text(
            encoding="utf-8"
        )
        self.assertIn("skyvern[local]==1.0.47", skyvern_requirements)
        self.assertIn("playwright==1.62.0", skyvern_requirements)
        self.assertNotIn("browsergym", skyvern_requirements.lower())
        self.assertIn("requirements-common.txt", orchestrator_requirements)
        common_requirements = (ROOT / "adapters" / "requirements-common.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("browsergym-miniwob==0.14.1", common_requirements)


if __name__ == "__main__":
    unittest.main()
