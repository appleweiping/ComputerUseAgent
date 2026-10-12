"""Packaging contract for the extension ZIP (tools/package_extension.py)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from tools import package_extension

SOURCE = package_extension.SOURCE_DIRECTORY
REPOSITORY_ROOT = package_extension.REPOSITORY_ROOT
# Uppercase sorts first: LICENSE and NOTICE lead the archive.
EXPECTED_ARCHIVE_FILES = [
    "LICENSE",
    "NOTICE",
    "dashboard/dashboard.css",
    "dashboard/dashboard.html",
    "dashboard/dashboard.js",
    "icons/128.png",
    "icons/16.png",
    "icons/32.png",
    "icons/48.png",
    "manifest.json",
    "popup/popup.css",
    "popup/popup.html",
    "popup/popup.js",
    "privacy/disclosure.css",
    "privacy/disclosure.html",
    "src/background.js",
    "src/content.js",
    "src/core.js",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_app(tmp_path: Path) -> Path:
    copy = tmp_path / "app"
    shutil.copytree(SOURCE, copy)
    return copy


def test_two_builds_are_byte_identical(tmp_path: Path) -> None:
    first = package_extension.build_archive(SOURCE, tmp_path / "one")
    second = package_extension.build_archive(SOURCE, tmp_path / "two")
    assert first.name == second.name
    assert first.read_bytes() == second.read_bytes()
    assert _sha256(first) == _sha256(second)


def test_archive_contains_exactly_the_runtime_allowlist(tmp_path: Path) -> None:
    archive = package_extension.build_archive(SOURCE, tmp_path)
    version = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))["version"]
    assert archive.name == f"computer-use-agent-recorder-{version}.zip"
    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()
        assert names == EXPECTED_ARCHIVE_FILES
        assert {name.split("/")[0] for name in names} == {
            "LICENSE",
            "NOTICE",
            "manifest.json",
            "src",
            "popup",
            "dashboard",
            "privacy",
            "icons",
        }
        for info in opened.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == zipfile.ZIP_STORED
            assert info.external_attr >> 16 == 0o100644
            assert not info.is_dir()
        packaged_manifest = json.loads(opened.read("manifest.json"))
        for legal in ("LICENSE", "NOTICE"):
            expected = (REPOSITORY_ROOT / legal).read_bytes().replace(b"\r\n", b"\n")
            assert opened.read(legal) == expected
    assert packaged_manifest["version"] == version
    for relative in packaged_manifest["icons"].values():
        assert relative in names
    assert packaged_manifest["background"]["service_worker"] in names
    assert packaged_manifest["action"]["default_popup"] in names
    assert packaged_manifest["options_page"] in names


def test_unexpected_files_fail_closed(tmp_path: Path) -> None:
    for stray in (
        "LICENSE",
        "notes.md",
        "src/core.js.map",
        "popup/.DS_Store",
        "test/extra.test.js",
        "icons/logo.svg",
    ):
        app = _copy_app(tmp_path / stray.replace("/", "_"))
        target = app / stray
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("stray", encoding="utf-8")
        with pytest.raises(package_extension.PackagingError, match="outside the runtime allowlist"):
            package_extension.build_archive(app, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.zip"))


def test_missing_manifest_or_bad_version_fails(tmp_path: Path) -> None:
    app = _copy_app(tmp_path / "missing")
    (app / "manifest.json").unlink()
    with pytest.raises(package_extension.PackagingError, match="manifest.json is missing"):
        package_extension.build_archive(app, tmp_path / "out")

    app = _copy_app(tmp_path / "badversion")
    manifest = json.loads((app / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = "0.5"
    (app / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(package_extension.PackagingError, match=r"MAJOR\.MINOR\.PATCH"):
        package_extension.build_archive(app, tmp_path / "out")

    with pytest.raises(package_extension.PackagingError, match="source directory is missing"):
        package_extension.runtime_files(tmp_path / "does-not-exist")


def _with_line_endings(data: bytes, newline: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", newline)


def test_line_endings_are_normalized_for_cross_platform_identity(tmp_path: Path) -> None:
    builds = []
    for label, newline in (("lf", b"\n"), ("crlf", b"\r\n")):
        app = _copy_app(tmp_path / label)
        for path in app.rglob("*"):
            if path.suffix in {".js", ".html", ".css", ".json"}:
                path.write_bytes(_with_line_endings(path.read_bytes(), newline))
        legal_root = tmp_path / f"{label}-legal"
        legal_root.mkdir()
        for legal in package_extension.LEGAL_FILES:
            data = (REPOSITORY_ROOT / legal).read_bytes()
            (legal_root / legal).write_bytes(_with_line_endings(data, newline))
        builds.append(
            package_extension.build_archive(app, tmp_path / f"{label}-out", legal_root=legal_root)
        )
    assert builds[0].read_bytes() == builds[1].read_bytes()
    with zipfile.ZipFile(builds[1]) as opened:
        assert b"\r\n" not in opened.read("LICENSE")


def test_missing_or_non_regular_legal_files_fail_closed(tmp_path: Path) -> None:
    legal_root = tmp_path / "legal"
    legal_root.mkdir()
    shutil.copyfile(REPOSITORY_ROOT / "LICENSE", legal_root / "LICENSE")
    with pytest.raises(package_extension.PackagingError, match="NOTICE is missing"):
        package_extension.build_archive(SOURCE, tmp_path / "out", legal_root=legal_root)
    (legal_root / "NOTICE").mkdir()
    with pytest.raises(package_extension.PackagingError, match="NOTICE must be a regular file"):
        package_extension.build_archive(SOURCE, tmp_path / "out", legal_root=legal_root)
    assert not list((tmp_path / "out").glob("*.zip"))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlink support")
def test_links_are_rejected(tmp_path: Path) -> None:
    app = _copy_app(tmp_path / "linked")
    link = app / "src" / "linked.js"
    try:
        link.symlink_to(app / "src" / "core.js")
    except OSError as error:  # Windows without developer mode or privileges
        pytest.skip(f"cannot create a symlink here: {error}")
    with pytest.raises(package_extension.PackagingError, match="link/reparse"):
        package_extension.build_archive(app, tmp_path / "out")

    legal_root = tmp_path / "legal"
    legal_root.mkdir()
    shutil.copyfile(REPOSITORY_ROOT / "NOTICE", legal_root / "NOTICE")
    (legal_root / "LICENSE").symlink_to(REPOSITORY_ROOT / "LICENSE")
    with pytest.raises(package_extension.PackagingError, match="LICENSE must be a regular file"):
        package_extension.build_archive(SOURCE, tmp_path / "out", legal_root=legal_root)


def test_main_reports_the_archive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert package_extension.main(["--output-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    archive = next(tmp_path.glob("computer-use-agent-recorder-*.zip"))
    assert f"files={len(EXPECTED_ARCHIVE_FILES)}" in output
    assert f"sha256={_sha256(archive)}" in output


def test_main_reports_packaging_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(package_extension, "SOURCE_DIRECTORY", tmp_path / "absent")
    assert package_extension.main(["--output-dir", str(tmp_path / "out")]) == 1
    assert "source directory is missing" in capsys.readouterr().err
