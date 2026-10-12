"""Minimal, auditable GPT-5.5 entitlement probe for the formal MiniWoB study.

The probe deliberately does not open a browser or expose any tools.  Its
private certificate proves only that the exact OpenAI model endpoint accepted
one small Responses API request.  It is a prerequisite, never task evidence.

The certificate is private.  Its default location is
``$CUA_PRIVATE_ROOT/week1_miniwob/provider_entitlement.json``; ``--output`` or
``CUI_WEEK1_PROVIDER_CERT`` select another file.  Every certificate path must
resolve outside the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, cast


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "cui-week1-provider-entitlement-v1"
MODEL = "gpt-5.5"
EXPECTED_RESPONSE_MODEL = "gpt-5.5-2026-04-23"
API_ROOT = "https://api.openai.com/v1"
RESPONSES_URL = f"{API_ROOT}/responses"
CERTIFICATE_ENV = "CUI_WEEK1_PROVIDER_CERT"
PRIVATE_ROOT_ENV = "CUA_PRIVATE_ROOT"
CERTIFICATE_RELATIVE_PATH = Path("week1_miniwob") / "provider_entitlement.json"
MAX_CERTIFICATE_AGE = timedelta(hours=24)
PROMPT = "Reply with exactly the two uppercase letters OK and no punctuation."
PROMPT_SHA256 = hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
HEX_SHA256 = re.compile(r"[a-f0-9]{64}")

# Official GPT-5.5 standard-processing prices observed on 2026-08-09.  The
# source URL is persisted with each certificate; no mutable third-party price
# feed participates in the research cost gate.
INPUT_PER_MILLION_USD = 5.0
CACHED_INPUT_PER_MILLION_USD = 0.5
OUTPUT_PER_MILLION_USD = 30.0
OFFICIAL_PRICING: dict[str, str | float] = {
    "input_per_million_usd": INPUT_PER_MILLION_USD,
    "cached_input_per_million_usd": CACHED_INPUT_PER_MILLION_USD,
    "output_per_million_usd": OUTPUT_PER_MILLION_USD,
    "source_url": "https://developers.openai.com/api/docs/models/gpt-5.5",
    "observed_on": "2026-08-09",
}

TOP_LEVEL_FIELDS = {
    "schema_version",
    "provider",
    "endpoint",
    "requested_model",
    "request_contract",
    "checked_at",
    "expires_at",
    "status",
    "http_status",
    "response_model",
    "response_id_sha256",
    "response_contract_ok",
    "latency_ms",
    "usage",
    "estimated_cost_usd",
    "official_pricing",
    "error_code",
    "payload_sha256",
}
REQUEST_FIELDS = {
    "store",
    "max_output_tokens",
    "reasoning_effort",
    "tools_enabled",
    "prompt_sha256",
}
USAGE_FIELDS = {"input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"}
PUBLIC_SUMMARY_FIELDS = {
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
}


class ProviderProbeError(ValueError):
    """Raised when the provider certificate or probe contract is invalid."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProviderProbeError(f"{field} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ProviderProbeError(f"{field} must be a valid UTC timestamp") from error
    return parsed


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def payload_sha256(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "payload_sha256"}
    return hashlib.sha256(canonical_bytes(unsigned)).hexdigest()


def estimate_cost_usd(usage: Mapping[str, int]) -> float:
    input_tokens = usage["input_tokens"]
    cached_tokens = usage["cached_input_tokens"]
    output_tokens = usage["output_tokens"]
    if cached_tokens > input_tokens:
        raise ProviderProbeError("cached input tokens cannot exceed total input tokens")
    cost = (
        (input_tokens - cached_tokens) * INPUT_PER_MILLION_USD
        + cached_tokens * CACHED_INPUT_PER_MILLION_USD
        + output_tokens * OUTPUT_PER_MILLION_USD
    ) / 1_000_000
    return round(cost, 9)


def _usage_from_response(response: Mapping[str, Any]) -> dict[str, int]:
    raw = response.get("usage")
    if not isinstance(raw, Mapping):
        raise ProviderProbeError("provider response omitted token usage")
    details = raw.get("input_tokens_details")
    cached = details.get("cached_tokens", 0) if isinstance(details, Mapping) else 0
    raw_values = (
        raw.get("input_tokens"),
        cached,
        raw.get("output_tokens"),
        raw.get("total_tokens"),
    )
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in raw_values):
        raise ProviderProbeError("provider token usage must contain non-negative integers")
    input_tokens, cached_tokens, output_tokens, total_tokens = raw_values
    assert all(isinstance(value, int) and not isinstance(value, bool) for value in raw_values)
    usage: dict[str, int] = {
        "input_tokens": cast(int, input_tokens),
        "cached_input_tokens": cast(int, cached_tokens),
        "output_tokens": cast(int, output_tokens),
        "total_tokens": cast(int, total_tokens),
    }
    if usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]:
        raise ProviderProbeError("provider total token count is inconsistent")
    return usage


def _output_text(response: Mapping[str, Any]) -> str:
    parts: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "output_text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts).strip()


def _request_contract() -> dict[str, Any]:
    return {
        "store": False,
        "max_output_tokens": 16,
        "reasoning_effort": "none",
        "tools_enabled": False,
        "prompt_sha256": PROMPT_SHA256,
    }


def _base_certificate(*, checked: datetime, latency_ms: int) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "openai",
        "endpoint": RESPONSES_URL,
        "requested_model": MODEL,
        "request_contract": _request_contract(),
        "checked_at": iso_z(checked),
        "expires_at": iso_z(checked + MAX_CERTIFICATE_AGE),
        "status": "failed",
        "http_status": None,
        "response_model": None,
        "response_id_sha256": None,
        "response_contract_ok": False,
        "latency_ms": latency_ms,
        "usage": None,
        "estimated_cost_usd": None,
        "official_pricing": dict(OFFICIAL_PRICING),
        "error_code": "probe_not_completed",
        "payload_sha256": "",
    }


def validate_certificate(
    value: Any,
    *,
    require_pass: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != TOP_LEVEL_FIELDS:
        raise ProviderProbeError("provider certificate does not match the closed schema")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ProviderProbeError("provider certificate schema version is invalid")
    if value.get("provider") != "openai" or value.get("endpoint") != RESPONSES_URL:
        raise ProviderProbeError("provider certificate is not bound to the official Responses endpoint")
    if value.get("requested_model") != MODEL:
        raise ProviderProbeError("provider certificate requested the wrong model")
    request = value.get("request_contract")
    if not isinstance(request, dict) or set(request) != REQUEST_FIELDS or request != _request_contract():
        raise ProviderProbeError("provider probe request contract is invalid")
    pricing = value.get("official_pricing")
    if pricing != OFFICIAL_PRICING:
        raise ProviderProbeError("provider certificate pricing is not the pinned official table")
    expected_hash = payload_sha256(value)
    if value.get("payload_sha256") != expected_hash:
        raise ProviderProbeError("provider certificate integrity hash does not match")
    checked = parse_time(value.get("checked_at"), "checked_at")
    expires = parse_time(value.get("expires_at"), "expires_at")
    if expires - checked != MAX_CERTIFICATE_AGE:
        raise ProviderProbeError("provider certificate lifetime is not exactly 24 hours")
    current = now or utc_now()
    if checked > current + timedelta(minutes=5):
        raise ProviderProbeError("provider certificate timestamp is in the future")
    if require_pass and current > expires:
        raise ProviderProbeError("provider entitlement certificate has expired")
    if type(value.get("latency_ms")) is not int or value["latency_ms"] < 0:
        raise ProviderProbeError("provider latency must be a non-negative integer")
    status = value.get("status")
    if status not in {"passed", "failed"}:
        raise ProviderProbeError("provider certificate status is invalid")
    if require_pass and status != "passed":
        raise ProviderProbeError("gpt-5.5 entitlement probe did not pass")
    if status == "passed":
        if value.get("http_status") != 200 or value.get("response_contract_ok") is not True:
            raise ProviderProbeError("passing entitlement certificate lacks a valid response")
        response_model = value.get("response_model")
        if response_model != EXPECTED_RESPONSE_MODEL:
            raise ProviderProbeError("provider resolved an unexpected model")
        if not isinstance(value.get("response_id_sha256"), str) or HEX_SHA256.fullmatch(
            value["response_id_sha256"]
        ) is None:
            raise ProviderProbeError("provider response ID digest is invalid")
        usage = value.get("usage")
        if not isinstance(usage, dict) or set(usage) != USAGE_FIELDS:
            raise ProviderProbeError("provider usage does not match the closed schema")
        if any(type(item) is not int or item < 0 for item in usage.values()):
            raise ProviderProbeError("provider usage contains invalid token counts")
        if usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]:
            raise ProviderProbeError("provider total token count is inconsistent")
        expected_cost = estimate_cost_usd(usage)
        if value.get("estimated_cost_usd") != expected_cost:
            raise ProviderProbeError("provider probe cost does not match official prices")
        if value.get("error_code") is not None:
            raise ProviderProbeError("passing entitlement certificate cannot contain an error")
    else:
        error_code = value.get("error_code")
        if not isinstance(error_code, str) or re.fullmatch(r"[a-z0-9_]{1,64}", error_code) is None:
            raise ProviderProbeError("failed entitlement certificate needs a stable error code")
    return value


def _private_path(path: Path) -> Path:
    """Resolve a certificate path and require it to lie outside the repository."""

    resolved = path.expanduser().resolve()
    repository = REPO_ROOT.resolve()
    if resolved == repository or repository in resolved.parents:
        raise ProviderProbeError("provider certificate must be stored outside the repository")
    return resolved


def default_certificate_path() -> Path:
    explicit = os.environ.get(CERTIFICATE_ENV)
    if explicit:
        return _private_path(Path(explicit))
    private_root = os.environ.get(PRIVATE_ROOT_ENV)
    if not private_root:
        raise ProviderProbeError(f"set {PRIVATE_ROOT_ENV} or {CERTIFICATE_ENV}, or pass --output")
    return _private_path(Path(private_root) / CERTIFICATE_RELATIVE_PATH)


def load_private_certificate(path: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    certificate_path = _private_path(path or default_certificate_path())
    try:
        value = json.loads(certificate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProviderProbeError("private provider entitlement certificate is unavailable") from error
    validated = validate_certificate(value, require_pass=True, now=now)
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": validated["provider"],
        "endpoint": validated["endpoint"],
        "payload_sha256": validated["payload_sha256"],
        "requested_model": validated["requested_model"],
        "response_model": validated["response_model"],
        "checked_at": validated["checked_at"],
        "expires_at": validated["expires_at"],
        "latency_ms": validated["latency_ms"],
        "usage": validated["usage"],
        "estimated_cost_usd": validated["estimated_cost_usd"],
        "official_pricing": validated["official_pricing"],
    }


def validate_public_summary(
    value: Any,
    *,
    at: datetime | str | None = None,
) -> dict[str, Any]:
    """Validate the content-free certificate descriptor stored with attempts.

    ``at`` binds a formal attempt to the certificate validity window without
    making historical evidence depend on the wall clock at publication time.
    Callers validating a live private certificate use ``validate_certificate``
    instead, which additionally enforces expiry against the current time.
    """

    if not isinstance(value, dict) or set(value) != PUBLIC_SUMMARY_FIELDS:
        raise ProviderProbeError("provider entitlement summary does not match the closed schema")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("provider") != "openai"
        or value.get("endpoint") != RESPONSES_URL
        or value.get("requested_model") != MODEL
    ):
        raise ProviderProbeError("provider entitlement summary has the wrong identity")
    if not isinstance(value.get("payload_sha256"), str) or HEX_SHA256.fullmatch(
        value["payload_sha256"]
    ) is None:
        raise ProviderProbeError("provider entitlement summary hash is invalid")
    response_model = value.get("response_model")
    if response_model != EXPECTED_RESPONSE_MODEL:
        raise ProviderProbeError("provider entitlement summary resolved model is invalid")
    checked = parse_time(value.get("checked_at"), "checked_at")
    expires = parse_time(value.get("expires_at"), "expires_at")
    if expires - checked != MAX_CERTIFICATE_AGE:
        raise ProviderProbeError("provider entitlement summary lifetime is invalid")
    if at is not None:
        observed = parse_time(at, "attempt_started_at") if isinstance(at, str) else at
        if observed.tzinfo is None:
            raise ProviderProbeError("attempt_started_at must be timezone-aware")
        observed = observed.astimezone(timezone.utc)
        if observed < checked or observed > expires:
            raise ProviderProbeError(
                "formal attempt started outside the provider entitlement window"
            )
    if type(value.get("latency_ms")) is not int or value["latency_ms"] < 0:
        raise ProviderProbeError("provider entitlement summary latency is invalid")
    usage = value.get("usage")
    if not isinstance(usage, dict) or set(usage) != USAGE_FIELDS:
        raise ProviderProbeError("provider entitlement summary usage is invalid")
    if any(type(item) is not int or item < 0 for item in usage.values()):
        raise ProviderProbeError("provider entitlement summary token count is invalid")
    if usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]:
        raise ProviderProbeError("provider entitlement summary total tokens are inconsistent")
    if value.get("official_pricing") != OFFICIAL_PRICING:
        raise ProviderProbeError("provider entitlement summary pricing is invalid")
    if value.get("estimated_cost_usd") != estimate_cost_usd(usage):
        raise ProviderProbeError("provider entitlement summary cost is invalid")
    return value


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        raise ProviderProbeError(
            "provider certificate already exists; retain it and choose a new immutable path"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_probe(*, timeout_seconds: float = 30.0) -> dict[str, Any]:
    configured_base = os.environ.get("OPENAI_BASE_URL")
    if configured_base and configured_base.rstrip("/") != API_ROOT:
        raise ProviderProbeError("formal entitlement probe forbids a non-OpenAI base URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or any(character.isspace() for character in api_key):
        raise ProviderProbeError("OPENAI_API_KEY is missing or malformed")
    body = {
        "model": MODEL,
        "input": PROMPT,
        "max_output_tokens": 16,
        "reasoning": {"effort": "none"},
        "store": False,
        "tools": [],
    }
    request = urllib.request.Request(
        RESPONSES_URL,
        data=canonical_bytes(body),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    checked = utc_now()
    started = time.monotonic()
    certificate = _base_certificate(checked=checked, latency_ms=0)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            raw = response.read(2_000_000)
            http_status = int(getattr(response, "status", 200))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ProviderProbeError("provider response was not a JSON object")
        response_model = payload.get("model")
        response_id = payload.get("id")
        if response_model != EXPECTED_RESPONSE_MODEL:
            raise ProviderProbeError("provider resolved an unexpected model")
        if not isinstance(response_id, str) or not response_id:
            raise ProviderProbeError("provider response omitted its identifier")
        usage = _usage_from_response(payload)
        if _output_text(payload) != "OK":
            raise ProviderProbeError("provider response did not satisfy the probe contract")
        certificate.update(
            {
                "status": "passed",
                "http_status": http_status,
                "response_model": response_model,
                "response_id_sha256": hashlib.sha256(response_id.encode("utf-8")).hexdigest(),
                "response_contract_ok": http_status == 200,
                "usage": usage,
                "estimated_cost_usd": estimate_cost_usd(usage),
                "error_code": None,
            }
        )
    except urllib.error.HTTPError as error:
        certificate.update({"http_status": int(error.code), "error_code": f"http_{int(error.code)}"})
    except urllib.error.URLError:
        certificate["error_code"] = "network_error"
    except (json.JSONDecodeError, UnicodeDecodeError, ProviderProbeError):
        certificate["error_code"] = "response_contract_invalid"
    certificate["latency_ms"] = max(0, round((time.monotonic() - started) * 1000))
    certificate["payload_sha256"] = payload_sha256(certificate)
    validate_certificate(certificate, require_pass=False, now=checked)
    return certificate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="Private certificate path outside the repository "
        "(default: $CUA_PRIVATE_ROOT/week1_miniwob/provider_entitlement.json)",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--validate", action="store_true", help="Validate an existing certificate only")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = _private_path(args.output) if args.output else default_certificate_path()
        if args.validate:
            summary = load_private_certificate(output)
            print(json.dumps({"ready": True, "certificate": summary}, indent=2))
            return 0
        if not 1 <= args.timeout_seconds <= 60:
            raise ProviderProbeError("timeout must be between 1 and 60 seconds")
        certificate = run_probe(timeout_seconds=args.timeout_seconds)
        _atomic_write(output, certificate)
        passed = certificate["status"] == "passed"
        print(
            json.dumps(
                {
                    "ready": passed,
                    "schema_version": SCHEMA_VERSION,
                    "status": certificate["status"],
                    "http_status": certificate["http_status"],
                    "payload_sha256": certificate["payload_sha256"],
                    "secrets_printed": False,
                },
                indent=2,
            )
        )
        return 0 if passed else 2
    except ProviderProbeError as error:
        print(json.dumps({"ready": False, "error": str(error), "secrets_printed": False}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
