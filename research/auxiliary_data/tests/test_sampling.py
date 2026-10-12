from __future__ import annotations

import pytest

from research.auxiliary_data.common import ledger_for_sample
from research.auxiliary_data.sampling import (
    allocate_largest_remainder,
    hash_priority_sample,
    stratified_hash_priority_sample,
)


def test_largest_remainder_is_exact_and_deterministic() -> None:
    counts = {"b": 3, "a": 6, "c": 1}
    assert allocate_largest_remainder(counts, 5) == {"a": 3, "b": 2, "c": 0}
    assert sum(allocate_largest_remainder(counts, 7).values()) == 7


def test_hash_priority_sample_is_exact_stable_and_unique() -> None:
    rows = [{"id": f"row-{index}"} for index in range(100)]
    first = hash_priority_sample(rows, 17, seed=42, identity=lambda row: row["id"])
    second = hash_priority_sample(list(reversed(rows)), 17, seed=42, identity=lambda row: row["id"])
    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert len(first) == len({row["id"] for row in first}) == 17


def test_hash_priority_rejects_duplicate_sampling_identity() -> None:
    with pytest.raises(ValueError, match="unique"):
        hash_priority_sample([{"id": "x"}, {"id": "x"}], 1, seed=1, identity=lambda row: row["id"])


def test_stratified_sample_honors_allocation() -> None:
    rows = [
        {"id": f"a-{index}", "stratum": "a"} for index in range(10)
    ] + [{"id": f"b-{index}", "stratum": "b"} for index in range(20)]
    sample = stratified_hash_priority_sample(
        rows,
        allocation={"a": 3, "b": 7},
        seed=11,
        identity=lambda row: row["id"],
        stratum=lambda row: row["stratum"],
    )
    assert len(sample) == 10
    assert sum(row["stratum"] == "a" for row in sample) == 3
    assert sum(row["stratum"] == "b" for row in sample) == 7


def test_ledger_exports_only_identity_hash() -> None:
    ledger = ledger_for_sample(
        dataset="fixture",
        seed=1,
        population_count=3,
        selected_identities=["sensitive-id-1", "sensitive-id-2"],
        strata_population={"all": 3},
        strata_sample={"all": 2},
        population_fingerprint="0" * 64,
    )
    assert ledger["sample_count"] == 2
    assert ledger["row_level_identities_exported"] is False
    assert "sensitive-id" not in str(ledger)
