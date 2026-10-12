from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from research.week1_miniwob import provider_probe


class _Response:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class ProviderEntitlementProbeTests(unittest.TestCase):
    def response(self) -> dict[str, object]:
        return {
            "id": "resp_private_identifier",
            "model": "gpt-5.5-2026-04-23",
            "usage": {
                "input_tokens": 20,
                "input_tokens_details": {"cached_tokens": 5},
                "output_tokens": 2,
                "total_tokens": 22,
            },
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "OK"}],
                }
            ],
        }

    def test_probe_uses_exact_tool_free_responses_contract_and_redacts_identifiers(self) -> None:
        observed: dict[str, object] = {}

        def fake_urlopen(request: object, timeout: float) -> _Response:
            observed["url"] = request.full_url  # type: ignore[attr-defined]
            observed["body"] = json.loads(request.data)  # type: ignore[attr-defined]
            observed["authorization"] = request.headers.get("Authorization")  # type: ignore[attr-defined]
            observed["timeout"] = timeout
            return _Response(self.response())

        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "private-test-key", "OPENAI_BASE_URL": provider_probe.API_ROOT},
            clear=False,
        ), mock.patch.object(
            provider_probe.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            certificate = provider_probe.run_probe(timeout_seconds=7)

        self.assertEqual(observed["url"], provider_probe.RESPONSES_URL)
        self.assertEqual(
            observed["body"],
            {
                "model": "gpt-5.5",
                "input": provider_probe.PROMPT,
                "max_output_tokens": 16,
                "reasoning": {"effort": "none"},
                "store": False,
                "tools": [],
            },
        )
        self.assertEqual(observed["authorization"], "Bearer private-test-key")
        self.assertEqual(certificate["status"], "passed")
        self.assertNotIn("resp_private_identifier", json.dumps(certificate))
        self.assertNotIn("private-test-key", json.dumps(certificate))
        self.assertEqual(certificate["usage"]["cached_input_tokens"], 5)
        self.assertEqual(certificate["estimated_cost_usd"], 0.0001375)
        provider_probe.validate_certificate(certificate)

    def test_certificate_fails_closed_on_tamper_expiry_and_unknown_fields(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "private-test-key", "OPENAI_BASE_URL": provider_probe.API_ROOT},
            clear=False,
        ), mock.patch.object(
            provider_probe.urllib.request, "urlopen", return_value=_Response(self.response())
        ):
            certificate = provider_probe.run_probe()
        tampered = copy.deepcopy(certificate)
        tampered["usage"]["output_tokens"] = 100
        with self.assertRaisesRegex(provider_probe.ProviderProbeError, "integrity hash"):
            provider_probe.validate_certificate(tampered)
        unknown = copy.deepcopy(certificate)
        unknown["prompt"] = "leak"
        unknown["payload_sha256"] = provider_probe.payload_sha256(unknown)
        with self.assertRaisesRegex(provider_probe.ProviderProbeError, "closed schema"):
            provider_probe.validate_certificate(unknown)
        with self.assertRaisesRegex(provider_probe.ProviderProbeError, "expired"):
            provider_probe.validate_certificate(
                certificate,
                now=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
        summary = {
            key: certificate[key] for key in provider_probe.PUBLIC_SUMMARY_FIELDS
        }
        with self.assertRaisesRegex(provider_probe.ProviderProbeError, "outside"):
            provider_probe.validate_public_summary(
                summary,
                at="2030-01-01T00:00:00Z",
            )

    def test_non_openai_base_url_is_rejected_before_network_or_secret_output(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "private-test-key", "OPENAI_BASE_URL": "https://proxy.invalid/v1"},
            clear=False,
        ), mock.patch.object(provider_probe.urllib.request, "urlopen") as urlopen:
            with self.assertRaisesRegex(provider_probe.ProviderProbeError, "non-OpenAI"):
                provider_probe.run_probe()
        urlopen.assert_not_called()

    def test_default_certificate_lives_under_cua_private_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {"CUA_PRIVATE_ROOT": temporary}
            with mock.patch.dict(os.environ, environment, clear=False):
                os.environ.pop(provider_probe.CERTIFICATE_ENV, None)
                path = provider_probe.default_certificate_path()
            self.assertEqual(
                path,
                (Path(temporary) / "week1_miniwob" / "provider_entitlement.json").resolve(),
            )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(provider_probe.CERTIFICATE_ENV, None)
            os.environ.pop("CUA_PRIVATE_ROOT", None)
            with self.assertRaisesRegex(provider_probe.ProviderProbeError, "CUA_PRIVATE_ROOT"):
                provider_probe.default_certificate_path()

    def test_certificate_path_rule_is_repository_boundary_not_drive_or_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            # Any directory outside the checkout is acceptable: no drive letter
            # and no "private" directory-name convention is required.
            outside = Path(temporary) / "certs" / "probe.json"
            self.assertEqual(provider_probe._private_path(outside), outside.resolve())
        for inside in (
            provider_probe.REPO_ROOT / "probe.json",
            provider_probe.REPO_ROOT / "research" / "evidence" / "probe.json",
        ):
            with self.subTest(inside=inside), self.assertRaisesRegex(
                provider_probe.ProviderProbeError, "outside the repository"
            ):
                provider_probe._private_path(inside)

    def test_private_file_loader_verifies_path_hash_and_public_summary(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "private-test-key", "OPENAI_BASE_URL": provider_probe.API_ROOT},
            clear=False,
        ), mock.patch.object(
            provider_probe.urllib.request, "urlopen", return_value=_Response(self.response())
        ):
            certificate = provider_probe.run_probe()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "_private"
            path = root / "provider.json"
            root.mkdir()
            provider_probe._atomic_write(path, certificate)
            with self.assertRaisesRegex(provider_probe.ProviderProbeError, "already exists"):
                provider_probe._atomic_write(path, certificate)
            summary = provider_probe.load_private_certificate(path)
            provider_probe.validate_public_summary(summary)
        self.assertEqual(summary["requested_model"], "gpt-5.5")
        self.assertEqual(set(summary), {
            "schema_version",
            "provider",
            "endpoint",
            "payload_sha256",
            "requested_model",
            "response_model",
            "checked_at",
            "expires_at",
            "latency_ms",
            "usage",
            "estimated_cost_usd",
            "official_pricing",
        })


if __name__ == "__main__":
    unittest.main()
