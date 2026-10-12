"""Cross-version deterministic fixed-size and stratified sampling."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from typing import Callable, Mapping, Sequence, TypeVar


T = TypeVar("T")
HASH_PRIORITY_ALGORITHM = "sha256-priority-bottom-k-v1"


def _priority(seed: int, identity: str) -> bytes:
    return hashlib.sha256(f"{seed}\0{identity}".encode("utf-8")).digest()


def hash_priority_sample(
    rows: Sequence[T],
    sample_count: int,
    *,
    seed: int,
    identity: Callable[[T], str],
) -> list[T]:
    """Select the bottom-k SHA-256 priorities, a deterministic uniform sample."""

    if sample_count < 0:
        raise ValueError("sample_count must be non-negative")
    if sample_count > len(rows):
        raise ValueError(f"cannot sample {sample_count} rows from population {len(rows)}")
    identities = [identity(row) for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("sampling identities must be unique")
    ranked = sorted(
        zip(rows, identities, strict=True),
        key=lambda pair: (_priority(seed, pair[1]), pair[1]),
    )
    return [row for row, _ in ranked[:sample_count]]


def allocate_largest_remainder(
    population_counts: Mapping[str, int], sample_count: int
) -> dict[str, int]:
    """Allocate an exact proportional sample with deterministic tie-breaking."""

    if sample_count < 0:
        raise ValueError("sample_count must be non-negative")
    if any(count < 0 for count in population_counts.values()):
        raise ValueError("population counts must be non-negative")
    total = sum(population_counts.values())
    if sample_count > total:
        raise ValueError(f"cannot allocate {sample_count} rows from population {total}")
    if total == 0:
        if sample_count:
            raise ValueError("cannot allocate from an empty population")
        return {stratum: 0 for stratum in sorted(population_counts)}

    exact = {
        stratum: sample_count * count / total for stratum, count in population_counts.items()
    }
    allocated = {stratum: math.floor(value) for stratum, value in exact.items()}
    remaining = sample_count - sum(allocated.values())
    order = sorted(
        population_counts,
        key=lambda stratum: (-(exact[stratum] - allocated[stratum]), stratum),
    )
    for stratum in order[:remaining]:
        allocated[stratum] += 1
    if sum(allocated.values()) != sample_count:
        raise AssertionError("largest-remainder allocation did not preserve sample size")
    if any(allocated[stratum] > population_counts[stratum] for stratum in allocated):
        raise AssertionError("largest-remainder allocation exceeded a stratum")
    return dict(sorted(allocated.items()))


def stratified_hash_priority_sample(
    rows: Sequence[T],
    *,
    allocation: Mapping[str, int],
    seed: int,
    identity: Callable[[T], str],
    stratum: Callable[[T], str],
) -> list[T]:
    grouped: dict[str, list[T]] = defaultdict(list)
    for row in rows:
        grouped[stratum(row)].append(row)
    unexpected = set(grouped) - set(allocation)
    if unexpected:
        raise ValueError(f"allocation is missing strata: {sorted(unexpected)}")
    selected: list[T] = []
    for name in sorted(allocation):
        selected.extend(
            hash_priority_sample(
                grouped.get(name, []),
                allocation[name],
                seed=seed,
                identity=identity,
            )
        )
    return sorted(selected, key=identity)
