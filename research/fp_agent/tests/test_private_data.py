"""Checks that need the restricted FP-Agent files under the private data root.

They are marked ``requires_private_data`` (CI deselects them) and skip cleanly
when ``$CUA_PRIVATE_ROOT/fp_agent`` or the named file is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research.fp_agent.paths import PRIVATE_ROOT_ENV, default_data_root, default_upstream_repo
from research.fp_agent.retrospective_features import validate_private_artifact

pytestmark = pytest.mark.requires_private_data


def _private_file(name: str) -> Path:
    data_root = default_data_root()
    if data_root is None:
        pytest.skip(f"{PRIVATE_ROOT_ENV} is not set")
    path = data_root / name
    if not path.is_file():
        pytest.skip(f"{name} is absent from the private data root")
    return path


@pytest.mark.parametrize("name", ["feature_vectors.json", "train_test_split.npz"])
def test_released_artifacts_match_the_tracked_manifest(name: str) -> None:
    record = validate_private_artifact(_private_file(name), name)
    assert record["name"] == name


@pytest.mark.slow
def test_raw_corpus_matches_the_tracked_manifest() -> None:
    record = validate_private_artifact(_private_file("raw_dataset.json"), "raw_dataset.json")
    assert record["bytes"] == 4_256_528_751


def test_loafo_preflight_is_ready() -> None:
    pytest.importorskip("numpy", reason="needs the research extra")
    from research.fp_agent import leave_one_family_out as loafo

    data_path = _private_file("feature_vectors.json")
    repo = default_upstream_repo()
    if repo is None or not (repo / ".git").exists():
        pytest.skip("CUA_FP_AGENT_UPSTREAM_REPO does not point at an FP-Agent git checkout")
    report = loafo.preflight(
        data_path=data_path, repo=repo.resolve(), seed=20260808, test_group_fraction=0.2
    )
    assert report["status"] == "ready"
    assert report["human_split_summary"]["group_overlap"] == 0
