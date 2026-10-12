"""Shared deterministic and aggregate-only helpers for auxiliary analyses."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the single canonical JSON encoding used by every ledger/hash."""

    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def atomic_write_json(path: Path, value: Any) -> None:
    """Atomically write stable, pretty JSON without exposing partial results."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def numeric_profile(values: Iterable[Any]) -> dict[str, Any]:
    """Return bounded aggregate distribution evidence; never return row values."""

    import numpy as np  # the research extra; imported lazily so the helpers stay light

    materialized = [number for value in values if (number := finite_float(value)) is not None]
    array = np.asarray(materialized, dtype=float)
    if not materialized:
        return {
            "non_missing_count": 0,
            "mean": None,
            "std": None,
            "min": None,
            "max": None,
            "quantiles": {f"p{int(q * 100):02d}": None for q in QUANTILES},
        }
    return {
        "non_missing_count": int(array.size),
        "mean": float(array.mean()),
        "std": float(array.std(ddof=0)),
        "min": float(array.min()),
        "max": float(array.max()),
        "quantiles": {
            f"p{int(quantile * 100):02d}": float(np.quantile(array, quantile))
            for quantile in QUANTILES
        },
    }


def profile_sequence(values: Sequence[Any]) -> dict[str, Any]:
    profile = numeric_profile(values)
    profile["row_count"] = len(values)
    profile["missing_count"] = len(values) - int(profile["non_missing_count"])
    return profile


def distribution_comparison(
    population: Sequence[Mapping[str, Any]],
    sample: Sequence[Mapping[str, Any]],
    metrics: Sequence[str],
) -> dict[str, Any]:
    """Compare sample and eligible population using transparent aggregates."""

    output: dict[str, Any] = {}
    for metric in metrics:
        population_profile = profile_sequence([row.get(metric) for row in population])
        sample_profile = profile_sequence([row.get(metric) for row in sample])
        population_quantiles = population_profile["quantiles"]
        sample_quantiles = sample_profile["quantiles"]
        quantile_deltas: dict[str, float | None] = {}
        for name, population_value in population_quantiles.items():
            sample_value = sample_quantiles[name]
            quantile_deltas[name] = (
                None
                if population_value is None or sample_value is None
                else float(sample_value - population_value)
            )
        population_std = finite_float(population_profile["std"])
        population_mean = finite_float(population_profile["mean"])
        sample_mean = finite_float(sample_profile["mean"])
        standardized_mean_delta = (
            None
            if population_std in (None, 0.0)
            or population_mean is None
            or sample_mean is None
            else float((sample_mean - population_mean) / population_std)
        )
        output[metric] = {
            "population": population_profile,
            "sample": sample_profile,
            "sample_minus_population_quantiles": quantile_deltas,
            "standardized_mean_delta": standardized_mean_delta,
        }
    return output


def categorical_counts(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts = Counter(str(row.get(key, "<missing>")) for row in rows)
    return dict(sorted(counts.items()))


def identity_digest(identity: str) -> str:
    """Hide a provider row reference while retaining deterministic auditability."""

    return sha256_bytes(identity.encode("utf-8"))


def ledger_for_sample(
    *,
    dataset: str,
    seed: int,
    population_count: int,
    selected_identities: Sequence[str],
    strata_population: Mapping[str, int],
    strata_sample: Mapping[str, int],
    population_fingerprint: str,
) -> dict[str, Any]:
    selected_digests = sorted(identity_digest(identity) for identity in selected_identities)
    return {
        "dataset": dataset,
        "seed": seed,
        "algorithm": "sha256-priority-bottom-k-v1",
        "population_count": population_count,
        "sample_count": len(selected_identities),
        "strata_population_counts": dict(sorted(strata_population.items())),
        "strata_sample_counts": dict(sorted(strata_sample.items())),
        "population_fingerprint_sha256": population_fingerprint,
        "selected_identity_digests_sha256": canonical_sha256(selected_digests),
        "row_level_identities_exported": False,
    }


def population_fingerprint(
    rows: Sequence[Mapping[str, Any]],
    identity: Callable[[Mapping[str, Any]], str],
    payload: Callable[[Mapping[str, Any]], Any],
) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=identity):
        digest.update(canonical_json_bytes([identity(row), payload(row)]))
        digest.update(b"\n")
    return digest.hexdigest()
