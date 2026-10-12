"""tools/ci/check_versions.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.ci.check_versions import VersionCheckError, check, collect_versions, main, normalize_tag

REPOSITORY = Path(__file__).resolve().parents[2]


def _tree(root: Path, version: str = "1.4.2", **overrides: str) -> Path:
    versions = {
        "pyproject": version,
        "manifest": version,
        "package": version,
        "lock": version,
        "citation": version,
        "init": version,
        **overrides,
    }
    (root / "extension" / "app").mkdir(parents=True)
    (root / "src" / "computer_use_agent").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nversion = "{versions["pyproject"]}"\n', encoding="utf-8"
    )
    (root / "extension" / "app" / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "version": versions["manifest"]}), encoding="utf-8"
    )
    (root / "extension" / "package.json").write_text(
        json.dumps({"name": "demo", "version": versions["package"]}), encoding="utf-8"
    )
    (root / "extension" / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": versions["lock"],
                "lockfileVersion": 3,
                "packages": {"": {"name": "demo", "version": versions["lock"]}},
            }
        ),
        encoding="utf-8",
    )
    (root / "CITATION.cff").write_text(
        f'cff-version: 1.2.0\ntitle: "Demo"\nversion: "{versions["citation"]}"\n',
        encoding="utf-8",
    )
    (root / "src" / "computer_use_agent" / "__init__.py").write_text(
        f'try:\n    pass\nexcept Exception:\n    __version__ = "{versions["init"]}"\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - 2026-10-11\n\n- Demo.\n",
        encoding="utf-8",
    )
    return root


def test_consistent_tree_passes(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    assert check(root) == []
    assert check(root, tag="v1.4.2") == []
    assert check(root, tag="refs/tags/v1.4.2") == []
    assert {source.version for source in collect_versions(root)} == {"1.4.2"}


@pytest.mark.parametrize("source", ["manifest", "package", "lock", "citation", "init"])
def test_any_drifted_source_fails(tmp_path: Path, source: str) -> None:
    root = _tree(tmp_path, **{source: "1.4.1"})
    problems = check(root)
    assert len(problems) >= 1
    assert all("'1.4.1' != '1.4.2'" in problem for problem in problems)


def test_tag_mismatch_and_missing_changelog_section_fail(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    assert any("tag 'v1.4.3'" in problem for problem in check(root, tag="v1.4.3"))
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")
    assert any("CHANGELOG.md has no '## [1.4.2]'" in p for p in check(root, tag="v1.4.2"))


def test_prerelease_versions_are_rejected(tmp_path: Path) -> None:
    root = _tree(tmp_path, version="1.4.2-rc1")
    assert any("MAJOR.MINOR.PATCH" in problem for problem in check(root))


def test_missing_file_is_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "CITATION.cff").unlink()
    assert check(root) == ["CITATION.cff version: file not found"]


def test_normalize_tag() -> None:
    assert normalize_tag("v0.5.0") == "0.5.0"
    assert normalize_tag("refs/tags/v0.5.0") == "0.5.0"
    with pytest.raises(VersionCheckError):
        normalize_tag("0.5.0")


def test_cli_exit_codes(tmp_path: Path, capsys) -> None:
    root = _tree(tmp_path)
    assert main(["--root", str(root)]) == 0
    assert "PASS" in capsys.readouterr().out
    assert main(["--root", str(root), "--tag", "v9.9.9"]) == 1


def test_repository_canonical_version_is_well_formed() -> None:
    sources = collect_versions(REPOSITORY)
    assert sources[0].label.startswith("pyproject.toml")
    assert sources[0].version is not None
    assert not any("MAJOR.MINOR.PATCH" in problem for problem in check(REPOSITORY))
