from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from research.week1_miniwob.adapters import common


REPO_ROOT = Path(__file__).resolve().parents[3]


class EpisodeLifecycleGuardTests(unittest.TestCase):
    def test_browser_host_has_sticky_page_document_and_environment_guards(self) -> None:
        source = (
            REPO_ROOT / "research" / "week1_miniwob" / "adapters" / "node" / "browser_host.mjs"
        ).read_text(encoding="utf-8")
        required_tokens = (
            "establishSingleTaskPage",
            'browser.on("targetcreated"',
            'browser.on("targetdestroyed"',
            'browser.on("targetchanged"',
            'taskEnvironmentSession.on("Page.frameNavigated"',
            'taskEnvironmentSession.on("Page.frameStartedLoading"',
            'taskEnvironmentSession.on("Runtime.executionContextsCleared"',
            "loader_id",
            "document_token",
            "time_origin_ms",
            "episode_started_at_ms",
            "device_pixel_ratio",
            '"--force-device-scale-factor=1"',
            '"--high-dpi-support=1"',
        )
        for token in required_tokens:
            with self.subTest(token=token):
                self.assertIn(token, source)

    def test_formal_order_asserts_identity_immediately_before_official_reward(self) -> None:
        source = inspect.getsource(common.run_formal_adapter)
        pre_guard = source.index("host.assert_episode_guard()")
        validator = source.index("reward, terminated, task_info = episode.validate()")
        post_guard = source.index("host.assert_episode_guard()", pre_guard + 1)
        disarm = source.index("host.disarm_episode_guard(require_stable=True)")
        screenshot = source.index("episode.save_final_screenshot")
        recorder_stop = source.index("recorder_stop = host.stop_recorder()")
        self.assertLess(pre_guard, validator)
        self.assertLess(validator, post_guard)
        self.assertLess(post_guard, disarm)
        self.assertLess(disarm, screenshot)
        self.assertLess(screenshot, recorder_stop)
        between = source[pre_guard:validator]
        self.assertNotIn("driver(", between)
        self.assertNotIn("save_final_screenshot", between)
        after_validator = source[validator:disarm]
        self.assertIn('promote_failure("episode_guard_post_validator_failed"', after_validator)

    def test_strict_disarm_rejects_violation_after_final_validator_attestation(self) -> None:
        host = common.BrowserHost.__new__(common.BrowserHost)
        host.episode_guard_attestations = [
            {"attestation_sequence": 1},
            {"attestation_sequence": 2},
        ]
        host.rpc = lambda *_args, **_kwargs: {
            "disarmed": True,
            "violation_count": 1,
            "final_attestation_sequence": 2,
        }
        with self.assertRaises(common.AdapterError) as caught:
            host.disarm_episode_guard(require_stable=True)
        self.assertEqual(caught.exception.code, "episode_guard_disarm_unstable")

    def test_browser_host_rpc_attestations_are_monotonic_and_bound_to_disarm(self) -> None:
        source = (
            REPO_ROOT / "research" / "week1_miniwob" / "adapters" / "node" / "browser_host.mjs"
        ).read_text(encoding="utf-8")
        self.assertIn("episodeGuard.assertionCount += 1", source)
        self.assertIn("attestation_sequence: episodeGuard.assertionCount", source)
        self.assertIn("final_attestation_sequence: finalAttestationSequence", source)

    def test_browser_host_re_pins_environment_before_recorder_start(self) -> None:
        episode_source = inspect.getsource(common.OfficialMiniwobEpisode.__enter__)
        self.assertIn("self.host.pin_post_setup_environment()", episode_source)
        formal_source = inspect.getsource(common.run_formal_adapter)
        setup = formal_source.index("with OfficialMiniwobEpisode")
        recorder = formal_source.index("recorder_start = host.start_recorder")
        self.assertLess(setup, recorder)


if __name__ == "__main__":
    unittest.main()
