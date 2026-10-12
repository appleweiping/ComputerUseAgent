"""Week 1 protocol and adapter regression tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from research.week1_miniwob.provider_probe import (
    EXPECTED_RESPONSE_MODEL,
    OFFICIAL_PRICING,
    SCHEMA_VERSION,
    estimate_cost_usd,
    iso_z,
)


def provider_entitlement_fixture(
    reference: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    """Return deterministic-shape provider provenance for contract tests."""

    started = (reference or datetime.now(timezone.utc)).astimezone(timezone.utc)
    checked = started - timedelta(minutes=1)
    usage = {
        "input_tokens": 20,
        "cached_input_tokens": 5,
        "output_tokens": 2,
        "total_tokens": 22,
    }
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "provider": "openai",
            "endpoint": "https://api.openai.com/v1/responses",
            "payload_sha256": "e" * 64,
            "requested_model": "gpt-5.5",
            "response_model": EXPECTED_RESPONSE_MODEL,
            "checked_at": iso_z(checked),
            "expires_at": iso_z(checked + timedelta(hours=24)),
            "latency_ms": 100,
            "usage": usage,
            "estimated_cost_usd": estimate_cost_usd(usage),
            "official_pricing": dict(OFFICIAL_PRICING),
        },
        iso_z(started),
    )
