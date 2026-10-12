"""Every research npm lockfile resolves from the official npm registry.

``npm ci`` downloads exactly the ``resolved`` URL recorded in the lockfile, so a
lockfile regenerated behind a registry mirror would send every documented
install to that mirror (and record the build host's registry). The integrity
hashes do not depend on the host, so the official URL is always recoverable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH = REPO_ROOT / "research"
OFFICIAL_REGISTRY = "https://registry.npmjs.org/"
LOCKFILES = sorted(
    path for path in RESEARCH.rglob("package-lock.json") if "node_modules" not in path.parts
)


def test_research_lockfiles_are_discovered() -> None:
    relative = {path.relative_to(REPO_ROOT).as_posix() for path in LOCKFILES}
    assert "research/scientific_simulators/package-lock.json" in relative
    assert "research/week1_miniwob/adapters/node/package-lock.json" in relative


@pytest.mark.parametrize(
    "lockfile", LOCKFILES, ids=[path.relative_to(REPO_ROOT).as_posix() for path in LOCKFILES]
)
def test_lockfile_resolves_only_from_the_official_registry(lockfile: Path) -> None:
    lock = json.loads(lockfile.read_text(encoding="utf-8"))
    package = json.loads((lockfile.parent / "package.json").read_text(encoding="utf-8"))
    assert lock["lockfileVersion"] == 3
    assert lock["name"] == package["name"]
    assert lock["packages"][""]["name"] == package["name"]
    entries = {key: value for key, value in lock["packages"].items() if key}
    assert entries, lockfile
    for key, entry in entries.items():
        assert key.startswith("node_modules/"), key
        if entry.get("link") is True:
            continue
        resolved = entry.get("resolved", "")
        assert resolved.startswith(OFFICIAL_REGISTRY), f"{key}: {resolved or 'no resolved URL'}"
        assert str(entry.get("integrity", "")).startswith("sha512-"), key
