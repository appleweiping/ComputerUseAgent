"""Focused regression tests for exactly-once action-history compaction."""

from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from contextlib import contextmanager
from unittest import mock

from research.week1_miniwob.adapters import browser_use_adapter, common


class ActionHistoryPrivacyTests(unittest.TestCase):
    def test_compaction_infers_one_top_level_action_and_never_retains_values(self) -> None:
        secret = "DO-NOT-RETAIN-this typed value-or-selector"
        raw = [
            {"click": {"index": 7, "selector": secret}},
            {"type_text": {"text": secret}},
            {"done": {"success": True, "text": secret}},
        ]

        compacted = common.compact_actions(raw)

        self.assertEqual([row.get("type") for row in compacted], ["click", "type_text", "done"])
        self.assertEqual([row["fields"] for row in compacted], [["click"], ["type_text"], ["done"]])
        self.assertNotIn(secret, repr(compacted))
        self.assertNotIn("selector", repr(compacted))

    def test_compaction_is_idempotent_and_ambiguous_action_keys_are_not_guessed(self) -> None:
        raw = [
            {"click": {"index": 1}, "done": {"success": True}},
            {"type": "click", "selector": "private", "timestamp_ms": 12345},
        ]

        once = common.compact_actions(raw)

        self.assertNotIn("type", once[0])
        self.assertEqual(common.compact_actions(once), once)
        self.assertEqual(once[1], {"index": 2, "type": "click", "fields": ["selector", "timestamp_ms", "type"]})
        self.assertNotIn("12345", repr(once))
        self.assertNotIn("private", repr(once))

    def test_browser_use_framework_run_keeps_raw_model_actions(self) -> None:
        raw_actions = [{"click": {"index": 2, "selector": "private"}}]

        class FakeHistory:
            def model_actions(self):
                return raw_actions

            def is_done(self):
                return True

            def number_of_steps(self):
                return 1

            def errors(self):
                return [None]

            def is_successful(self):
                return True

        class FakeAgent:
            def __init__(self, **_kwargs):
                self.history = FakeHistory()

            async def run(self, **_kwargs):
                return self.history

        class FakeBrowserSession:
            def __init__(self, **_kwargs):
                self.agent_focus_target_id = "target-official"

            async def start(self):
                return None

            async def get_or_create_cdp_session(self, target_id, focus=False):
                self.agent_focus_target_id = target_id
                return object()

            async def get_tabs(self):
                return [types.SimpleNamespace(target_id="target-official", url="http://127.0.0.1:8123/task.html")]

            async def get_current_target_info(self):
                return {
                    "targetId": self.agent_focus_target_id,
                    "url": "http://127.0.0.1:8123/task.html",
                }

            async def stop(self):
                return None

        class FakeChatOpenAI:
            def __init__(self, **_kwargs):
                pass

        compatibility = types.SimpleNamespace(
            skipped_unconditional_resumes=0,
            bounded_resume_timeouts=0,
            screenshot_window_checks=0,
            window_normalizations=0,
            bring_to_front_calls=0,
        )

        @contextmanager
        def fake_compatibility(*_args):
            yield compatibility

        fake_module = types.SimpleNamespace(
            Agent=FakeAgent,
            BrowserSession=FakeBrowserSession,
            ChatOpenAI=FakeChatOpenAI,
        )
        attempt = {
            "model": "gpt-5.5",
            "seed": 20260808,
            "limits": {"timeout_seconds": 480, "max_actions": 30},
            "_runtime": {
                "official_target": {
                    "target_id": "target-official",
                    "url": "http://127.0.0.1:8123/task.html",
                },
                "step_alignment": {
                    "endpoint": "http://127.0.0.1:39123/v1/step",
                    "bearer_token": "a" * 48,
                    "framework": "browser-use",
                    "max_steps": 30,
                },
            },
        }
        with (
            mock.patch.dict(sys.modules, {"browser_use": fake_module}),
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-placeholder"}, clear=False),
            mock.patch.object(
                browser_use_adapter,
                "browser_use_external_cdp_compatibility",
                fake_compatibility,
            ),
            mock.patch.object(
                browser_use_adapter,
                "_verify_step_hook_contract",
                return_value={"verified": True, "source_hash_count": 2},
            ),
        ):
            result = asyncio.run(browser_use_adapter._run(attempt, "instruction", "http://127.0.0.1:9222"))

        self.assertIs(result.action_history, raw_actions)
        self.assertEqual(result.action_count, 1)
        self.assertEqual(result.artifacts["screenshot_window_checks"], 0)
        self.assertEqual(result.artifacts["window_normalizations"], 0)
        self.assertEqual(result.artifacts["bring_to_front_calls"], 0)

        class FailedHistory(FakeHistory):
            def is_done(self):
                return False

            def errors(self):
                return ["private provider/status response"]

            def is_successful(self):
                return False

        class FailedAgent(FakeAgent):
            def __init__(self, **_kwargs):
                self.history = FailedHistory()

        failed_module = types.SimpleNamespace(
            Agent=FailedAgent,
            BrowserSession=FakeBrowserSession,
            ChatOpenAI=FakeChatOpenAI,
        )
        with (
            mock.patch.dict(sys.modules, {"browser_use": failed_module}),
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-placeholder"}, clear=False),
            mock.patch.object(
                browser_use_adapter,
                "browser_use_external_cdp_compatibility",
                fake_compatibility,
            ),
            mock.patch.object(
                browser_use_adapter,
                "_verify_step_hook_contract",
                return_value={"verified": True, "source_hash_count": 2},
            ),
        ):
            failed = asyncio.run(
                browser_use_adapter._run(
                    attempt, "instruction", "http://127.0.0.1:9222"
                )
            )

        self.assertEqual(
            failed.error,
            "Browser-Use ended with failed framework steps before completion",
        )
        self.assertNotIn("provider/status", failed.error)


if __name__ == "__main__":
    unittest.main()
