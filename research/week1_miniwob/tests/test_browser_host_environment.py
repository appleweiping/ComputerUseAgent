import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HOST = ROOT / "research" / "week1_miniwob" / "adapters" / "node" / "browser_host.mjs"
MANIFEST = ROOT / "research" / "week1_miniwob" / "benchmark_manifest.json"


class BrowserEnvironmentTests(unittest.TestCase):
    def test_browser_host_enforces_and_reports_manifest_locale_and_timezone(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["browser"]["locale"], "en-US")
        self.assertEqual(manifest["browser"]["timezone"], "UTC")
        source = HOST.read_text(encoding="utf-8")
        for contract in (
            'const PINNED_LOCALE = "en-US"',
            'const PINNED_TIMEZONE = "UTC"',
            'Emulation.setLocaleOverride',
            'Emulation.setTimezoneOverride',
            '`--lang=${PINNED_LOCALE}`',
            'observed_locale: observedEnvironment.locale',
            'observed_timezone: observedEnvironment.timezone',
            'verified: true',
        ):
            self.assertIn(contract, source)

    def test_browser_host_attests_capture_stability_switches_at_runtime(self) -> None:
        source = HOST.read_text(encoding="utf-8")
        for contract in (
            '"--disable-background-timer-throttling"',
            '"--disable-backgrounding-occluded-windows"',
            '"--disable-renderer-backgrounding"',
            'const CAPTURE_STABILITY_DISABLED_FEATURE = "CalculateNativeWinOcclusion"',
            'session.send("Browser.getBrowserCommandLine")',
            "disabledFeatureArguments.length !== 1",
            "capture_stability_verified: true",
            "native_window_occlusion_disabled: true",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)


if __name__ == "__main__":
    unittest.main()
