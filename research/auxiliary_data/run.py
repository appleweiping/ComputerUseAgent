"""Fail-closed CLI for the three independent auxiliary calibration tracks."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Callable

from .common import atomic_write_json, canonical_sha256
from .paths import refuse_evidence_output
from .remote import PublicAccessError
from .sources import (
    CAPTCHA_PARQUET,
    CAPTCHA_SOURCE,
    MOLMOWEB_HUMAN,
    MOLMOWEB_SYNTHETIC,
    discover_huggingface_source,
)
from .tracks import run_aalto_136m, run_captchasolve30k, run_molmoweb


UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
TRACK_FILENAMES = {
    "captcha": "captchasolve30k_metrics.json",
    "aalto": "aalto_136m_metrics.json",
    "molmoweb": "molmoweb_metrics.json",
}


def _blocked(track: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "track": track,
        "status": "blocked",
        "blocked_reason": reason,
        "row_level_output": False,
        "raw_public_output": False,
    }


def _safe_run(track: str, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return operation()
    except (PublicAccessError, OSError, RuntimeError, ValueError, AssertionError) as exc:
        # PublicAccessError deliberately strips signed URLs and request headers.
        return _blocked(track, f"{type(exc).__name__}: {exc}")


def run_all(
    *,
    output_dir: Path,
    retrieved_at_utc: str,
    max_workers: int,
    selected_tracks: set[str],
) -> dict[str, Any]:
    if not UTC_PATTERN.fullmatch(retrieved_at_utc):
        raise ValueError("retrieved_at_utc must be an exact whole-second UTC timestamp ending in Z")
    output_dir = refuse_evidence_output(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    reused_results: dict[str, dict[str, Any]] = {}
    for track, filename in TRACK_FILENAMES.items():
        if track in selected_tracks:
            continue
        path = output_dir / filename
        if not path.exists():
            continue
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(candidate, dict) or "status" not in candidate:
            continue
        candidate_retrieval = candidate.get("retrieved_at_utc")
        if candidate_retrieval not in (None, retrieved_at_utc):
            continue
        candidate["retrieved_at_utc"] = retrieved_at_utc
        reused_results[track] = candidate

    source_manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "retrieved_at_utc": retrieved_at_utc,
        "access_policy": {
            "mode": "public, read-only, no credentials",
            "authorization_header_used": False,
            "signed_urls_persisted_or_logged": False,
            "row_level_cache": "none; projected rows are processed in memory",
            "raw_or_sampled_rows_tracked": False,
        },
        "sources": {},
    }

    discovered: dict[str, dict[str, Any]] = {}
    for spec in (CAPTCHA_SOURCE, CAPTCHA_PARQUET, MOLMOWEB_HUMAN, MOLMOWEB_SYNTHETIC):
        try:
            source = discover_huggingface_source(spec, retrieved_at_utc=retrieved_at_utc)
            discovered[spec.key] = source
            source_manifest["sources"][spec.key] = source
        except (PublicAccessError, OSError, RuntimeError, ValueError) as exc:
            source_manifest["sources"][spec.key] = {
                "source_key": spec.key,
                "provider_page": spec.provider_page,
                "revision": spec.revision,
                "retrieved_at_utc": retrieved_at_utc,
                "status": "blocked",
                "blocked_reason": f"{type(exc).__name__}: {exc}",
            }

    results: dict[str, dict[str, Any]] = dict(reused_results)
    if "captcha" in selected_tracks:
        if CAPTCHA_SOURCE.key not in discovered or CAPTCHA_PARQUET.key not in discovered:
            results["captcha"] = _blocked(
                "captchasolve30k_mouse_distribution_calibration",
                "pinned public source or Parquet projection metadata unavailable",
            )
        else:
            results["captcha"] = _safe_run(
                "captchasolve30k_mouse_distribution_calibration",
                lambda: run_captchasolve30k(
                    discovered[CAPTCHA_PARQUET.key], max_workers=min(max_workers, 5)
                ),
            )

    if "aalto" in selected_tracks:
        aalto_source: dict[str, Any] | None = None

        def aalto_operation() -> dict[str, Any]:
            nonlocal aalto_source
            result, aalto_source = run_aalto_136m(retrieved_at_utc=retrieved_at_utc)
            return result

        results["aalto"] = _safe_run(
            "aalto_136m_keyboard_distribution_calibration", aalto_operation
        )
        if aalto_source is not None:
            source_manifest["sources"]["aalto_136m_keystrokes"] = aalto_source
        else:
            source_manifest["sources"]["aalto_136m_keystrokes"] = {
                "source_key": "aalto_136m_keystrokes",
                "provider_page": "https://userinterfaces.aalto.fi/136Mkeystrokes/",
                "retrieved_at_utc": retrieved_at_utc,
                "status": "blocked",
                "blocked_reason": results["aalto"].get("blocked_reason"),
            }

    if "molmoweb" in selected_tracks:
        if MOLMOWEB_HUMAN.key not in discovered or MOLMOWEB_SYNTHETIC.key not in discovered:
            results["molmoweb"] = _blocked(
                "molmoweb_domain_held_out_action_sequence",
                "one or both pinned public MolmoWeb sources are unavailable",
            )
        else:
            results["molmoweb"] = _safe_run(
                "molmoweb_domain_held_out_action_sequence",
                lambda: run_molmoweb(
                    discovered[MOLMOWEB_HUMAN.key],
                    discovered[MOLMOWEB_SYNTHETIC.key],
                    max_workers=max_workers,
                ),
            )

    for track, result in results.items():
        result["retrieved_at_utc"] = retrieved_at_utc
        atomic_write_json(output_dir / TRACK_FILENAMES[track], result)

    ledgers: dict[str, Any] = {}
    if "sampling_ledger" in results.get("captcha", {}):
        ledgers["captchasolve30k"] = results["captcha"]["sampling_ledger"]
    if "sampling_ledger" in results.get("aalto", {}):
        ledgers["aalto_136m"] = results["aalto"]["sampling_ledger"]
    if "sampling_ledgers" in results.get("molmoweb", {}):
        ledgers["molmoweb"] = results["molmoweb"]["sampling_ledgers"]
    atomic_write_json(
        output_dir / "sampling_ledgers.json",
        {
            "schema_version": "1.0",
            "retrieved_at_utc": retrieved_at_utc,
            "ledgers": ledgers,
            "row_level_identities_exported": False,
        },
    )

    source_manifest["manifest_sha256"] = canonical_sha256(source_manifest)
    atomic_write_json(output_dir / "source_access_manifest.json", source_manifest)
    summary = {
        "schema_version": "1.0",
        "retrieved_at_utc": retrieved_at_utc,
        "tracks": {
            key: {
                "status": result["status"],
                "output": TRACK_FILENAMES[key],
                "blocked_reason": result.get("blocked_reason"),
            }
            for key, result in sorted(results.items())
        },
        "completed_tracks": sorted(
            key for key, result in results.items() if result["status"].startswith("complete")
        ),
        "blocked_tracks": sorted(
            key for key, result in results.items() if result["status"] == "blocked"
        ),
        "hard_boundaries": {
            "auxiliary_rows_concatenated_with_fp_agent": False,
            "headline_cross_dataset_classifier": False,
            "molmoweb_fed_to_23_feature_timing_model": False,
            "raw_or_sampled_rows_public": False,
        },
    }
    atomic_write_json(output_dir / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="aggregate-only output directory; the archived research/evidence/auxiliary_data "
        "is refused, so a rerun is compared with it instead of overwriting it",
    )
    parser.add_argument(
        "--retrieved-at-utc",
        required=True,
        help="exact retrieval timestamp, e.g. 2026-08-11T13:55:00Z",
    )
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument(
        "--tracks",
        nargs="+",
        choices=sorted(TRACK_FILENAMES),
        default=sorted(TRACK_FILENAMES),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_workers < 1 or args.max_workers > 16:
        raise SystemExit("--max-workers must be between 1 and 16")
    summary = run_all(
        output_dir=args.output_dir,
        retrieved_at_utc=args.retrieved_at_utc,
        max_workers=args.max_workers,
        selected_tracks=set(args.tracks),
    )
    print(
        f"auxiliary tracks complete={summary['completed_tracks']} "
        f"blocked={summary['blocked_tracks']}"
    )
    return 2 if summary["blocked_tracks"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
