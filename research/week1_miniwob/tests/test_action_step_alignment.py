from __future__ import annotations

import asyncio
import inspect
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock

from research.week1_miniwob.adapters import browser_use_adapter, common
from research.week1_miniwob import runner


class ActionStepAlignmentTests(unittest.TestCase):
    class FakeHost:
        def __init__(self) -> None:
            self.steps: list[str | None] = []

        def set_recorder_step(self, step_id: str | None) -> dict[str, str | None]:
            self.steps.append(step_id)
            return {"step_id": step_id}

    def test_authenticated_sidecar_accepts_only_next_opaque_step(self) -> None:
        host = self.FakeHost()
        with common.StepAlignmentSidecar(host, "browser-use", 2) as sidecar:
            descriptor = sidecar.descriptor()
            token = descriptor["bearer_token"]
            common.post_step_alignment(descriptor, "browser-use", 1)

            body = json.dumps(
                {"step_id": "step-002", "selector": "#secret"}
            ).encode("ascii")
            request = Request(
                descriptor["endpoint"],
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=2)  # noqa: S310 - test targets validated loopback sidecar
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()

            unauthorized = Request(
                descriptor["endpoint"],
                data=b'{"step_id":"step-002"}',
                method="POST",
                headers={"Authorization": "Bearer wrong-token"},
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(unauthorized, timeout=2)  # noqa: S310 - test targets loopback
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()

            common.post_step_alignment(descriptor, "browser-use", 2)
            evidence_during = sidecar.public_evidence()
            self.assertNotIn(token, json.dumps(evidence_during))
            self.assertEqual(evidence_during["accepted_requests"], 2)
            self.assertEqual(evidence_during["rejected_requests"], 2)

        evidence = sidecar.public_evidence()
        self.assertTrue(evidence["clear_acknowledged"])
        self.assertEqual(host.steps, ["step-001", "step-002", None])
        self.assertEqual(sidecar.token, "")

    def test_descriptor_rejects_non_loopback_extra_fields_and_action_payloads(self) -> None:
        base = {
            "endpoint": "http://127.0.0.1:39123/v1/step",
            "bearer_token": "a" * 48,
            "framework": "browser-use",
            "max_steps": 30,
        }
        self.assertEqual(
            common.validate_step_alignment_descriptor(base, "browser-use"),
            base,
        )
        for changed in (
            {**base, "endpoint": "http://example.com/v1/step"},
            {**base, "selector": "#private"},
            {**base, "bearer_token": "short"},
        ):
            with self.assertRaises(common.AdapterError):
                common.validate_step_alignment_descriptor(changed, "browser-use")

    def test_retired_stagehand_has_no_step_alignment_hook(self) -> None:
        self.assertNotIn("stagehand", common.STEP_ALIGNMENT_HOOKS)
        self.assertNotIn("stagehand", common.ACTIVE_FRAMEWORKS)
        descriptor = {
            "endpoint": "http://127.0.0.1:39123/v1/step",
            "bearer_token": "a" * 48,
            "framework": "stagehand",
            "max_steps": 30,
        }
        with self.assertRaises(common.AdapterError):
            common.validate_step_alignment_descriptor(descriptor, "stagehand")

    def test_browser_use_hook_emits_sequence_only_without_agent_content(self) -> None:
        descriptor = {
            "endpoint": "http://127.0.0.1:39123/v1/step",
            "bearer_token": "a" * 48,
            "framework": "browser-use",
            "max_steps": 30,
        }
        attempt = {"_runtime": {"step_alignment": descriptor}}
        hook = browser_use_adapter._BrowserUseStepHook(attempt)

        async def exercise() -> None:
            await hook(object())
            await hook(object())

        with mock.patch.object(browser_use_adapter, "post_step_alignment") as posted:
            asyncio.run(exercise())
        self.assertEqual(hook.calls, 2)
        self.assertEqual(
            [call.args[2] for call in posted.call_args_list],
            [1, 2],
        )
        serialized = json.dumps([call.args for call in posted.call_args_list], default=str)
        self.assertNotIn("selector", serialized)
        self.assertNotIn("instruction", serialized)

    def test_skyvern_is_explicitly_excluded_from_step_level_analysis(self) -> None:
        self.assertNotIn("skyvern", common.STEP_ALIGNMENT_HOOKS)
        source = (
            common.REPO_ROOT / "research" / "week1_miniwob" / "adapters" / "skyvern_local_bridge.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"step-alignment-capability"', source)
        self.assertIn('"step_alignment_support_status": "not_supported"', source)

    def test_runtime_sources_call_real_pre_action_hooks(self) -> None:
        browser_source = (
            common.REPO_ROOT / "research" / "week1_miniwob" / "adapters" / "browser_use_adapter.py"
        ).read_text(encoding="utf-8")
        host_source = common.NODE_HOST.read_text(encoding="utf-8")
        common_source = inspect.getsource(common.post_step_alignment)
        self.assertIn("agent.run(max_steps=max_steps, on_step_start=on_step_start)", browser_source)
        self.assertIn("setActiveStep(nextStepId)", host_source)
        self.assertIn("enqueueLifecycle(() => enqueueWrite", host_source)
        self.assertIn("extension.workers()", host_source)
        self.assertIn("build_opener(ProxyHandler({}))", common_source)

    def test_secret_free_alignment_counters_survive_publication_boundary(self) -> None:
        evidence = {
            "supported": True,
            "support_status": "supported",
            "hook_type": "browser_use_on_step_start",
            "transport_type": "authenticated_loopback_http",
            "accepted_requests": 3,
            "rejected_requests": 0,
            "framework_hook_calls": 3,
            "framework_host_counts_match": True,
            "aligned_natural_event_count": 17,
            "alignment_coverage_percent": 89.47,
            "analysis_eligible": True,
        }
        public = runner._public_framework_evidence(
            {"action_step_alignment": evidence, "bearer_token": "do-not-publish"}
        )
        self.assertEqual(public["action_step_alignment"], evidence)
        self.assertNotIn("do-not-publish", json.dumps(public))


if __name__ == "__main__":
    unittest.main()
