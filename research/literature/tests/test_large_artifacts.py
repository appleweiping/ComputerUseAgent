"""Tests for the large-artifact record, fetch_large, the layout helpers and --check modes.

None of these tests needs the network or the large artifacts themselves: downloads go
through a fake opener and the fetch tests use small stand-in files.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import pytest

from research.literature import fetch_large
from research.literature.scripts import build_citation_artifacts as citation
from research.literature.scripts.corpus_common import (
    BASE_PATH,
    GENERATED_DIR,
    LARGE_DIR,
    LITERATURE_DIR,
    PRIVATE_ROOT_ENV,
    REPO_ROOT,
    LargeArtifactError,
    large_artifacts_present,
    load_large_artifacts,
    private_data_root,
    record_private_path,
    recorded_path,
    resolve_private_path,
    resolve_recorded_path,
    sha256_file,
)

TEN_MIB = 10 * 1024 * 1024


def make_spec(name: str, data: bytes, url: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "url": url or f"https://raw.githubusercontent.com/owner/repo/{'0' * 40}/{name}",
    }


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, url: str, *, content_length: int | None = None) -> None:
        super().__init__(data)
        self._url = url
        length = len(data) if content_length is None else content_length
        self.headers = {"Content-Length": str(length)}

    def geturl(self) -> str:
        return self._url


# --------------------------------------------------------------- the record --


def test_large_artifacts_record_matches_the_citation_builder() -> None:
    record = json.loads((GENERATED_DIR / "LARGE_ARTIFACTS.json").read_text(encoding="utf-8"))
    specs = load_large_artifacts()
    by_output = citation.large_artifact_specs()

    assert set(by_output) == set(citation.LARGE_OUTPUT_NAMES) == {"graph", "assertions_csv"}
    for name, spec in by_output.items():
        assert citation.OUTPUTS[name].parent == LARGE_DIR
        assert citation.OUTPUTS[name].name == spec["name"]
    assert set(specs) == {"citation_graph.json", "citation_assertions.csv"}
    assert {name: (spec["bytes"], spec["sha256"]) for name, spec in specs.items()} == {
        "citation_graph.json": (
            72766360,
            "ed3f7050b257562f8602379fcbbb22d20b29b2e7d4fcf6227291955450754642",
        ),
        "citation_assertions.csv": (
            46375030,
            "1bb6e05747ef5051810896765b2c1013d70be8a29988afc5bdb039099d0018b2",
        ),
    }

    commit = record["source"]["commit"]
    assert len(commit) == 40 and all(char in "0123456789abcdef" for char in commit)
    for spec in specs.values():
        # Too large to commit is the only reason these files live outside git.
        assert spec["bytes"] >= TEN_MIB
        url = urlparse(spec["url"])
        assert url.scheme == "https"
        assert url.hostname == "raw.githubusercontent.com"
        # Immutable: pinned to the commit, never to a branch name.
        assert f"/{commit}/" in url.path
        assert url.path.endswith("/" + spec["sourcePath"])
        assert recorded_path(LARGE_DIR / spec["name"]) == spec["sourcePath"]


def test_large_directory_is_ignored_and_no_tracked_literature_file_is_large() -> None:
    ignore_lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "research/literature/generated/large/" in ignore_lines
    oversized = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in LITERATURE_DIR.rglob("*")
        if path.is_file()
        and LARGE_DIR not in path.parents
        and "__pycache__" not in path.parts
        and path.stat().st_size >= TEN_MIB
    ]
    assert oversized == []


# ------------------------------------------------------------- layout helpers --


def test_recorded_paths_reproduce_the_source_layout_and_resolve_back() -> None:
    assert recorded_path(BASE_PATH) == "literature/literature_survey_upsert.json"
    assert recorded_path(LARGE_DIR / "citation_graph.json") == "literature/citation_graph.json"
    script = LITERATURE_DIR / "scripts" / "build_citation_artifacts.py"
    assert recorded_path(script) == "literature/scripts/build_citation_artifacts.py"
    assert resolve_recorded_path("literature/scripts/build_citation_artifacts.py") == script

    truth = json.loads((GENERATED_DIR / "literature_truth_68.json").read_text(encoding="utf-8"))
    inputs = truth["provenance"]["inputs"]
    assert len(inputs) == 7
    for item in inputs:
        # Every recorded input still exists, with the bytes the truth file was built from.
        assert sha256_file(resolve_recorded_path(item["path"])) == item["sha256"]

    for bad in ("results/x.json", "/literature/x.json", "literature/../x.json", "literature"):
        with pytest.raises(ValueError):
            resolve_recorded_path(bad)
    with pytest.raises(FileNotFoundError):
        resolve_recorded_path("literature/no_such_file.json")


def test_private_paths_resolve_against_the_configured_private_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PRIVATE_ROOT_ENV, raising=False)
    assert private_data_root() == REPO_ROOT / "private_data"

    monkeypatch.setenv(PRIVATE_ROOT_ENV, str(tmp_path))
    resolved = resolve_private_path("private_data/literature/citation_raw/run-1/a.json")
    assert resolved == tmp_path.resolve() / "literature" / "citation_raw" / "run-1" / "a.json"
    assert record_private_path(resolved) == "private_data/literature/citation_raw/run-1/a.json"
    for bad in ("../private_data/x", "/private_data/x", "results/x", "private_data/../x"):
        with pytest.raises(ValueError):
            resolve_private_path(bad)

    monkeypatch.setenv(PRIVATE_ROOT_ENV, str(REPO_ROOT / "private_data"))
    with pytest.raises(ValueError, match="outside the repository"):
        private_data_root()


# ---------------------------------------------------------------- fetch_large --


def test_fetch_from_dir_copies_verifies_and_is_idempotent(tmp_path: Path) -> None:
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    payloads = {"a.json": b'{"a": 1}\n', "b.csv": b"x,y\n1,2\n"}
    for name, data in payloads.items():
        (source / name).write_bytes(data)
    specs = {name: make_spec(name, data) for name, data in payloads.items()}

    first = fetch_large.fetch(specs, dest, from_dir=source)
    assert {name: result["status"] for name, result in first.items()} == {
        "a.json": "copied",
        "b.csv": "copied",
    }
    for name, data in payloads.items():
        assert (dest / name).read_bytes() == data
    stamps = {name: (dest / name).stat().st_mtime_ns for name in payloads}

    second = fetch_large.fetch(specs, dest, from_dir=source)
    assert {result["status"] for result in second.values()} == {"present"}
    assert {name: (dest / name).stat().st_mtime_ns for name in payloads} == stamps
    assert sorted(path.name for path in dest.iterdir()) == ["a.json", "b.csv"]


def test_fetch_rejects_wrong_bytes_and_leaves_nothing_behind(tmp_path: Path) -> None:
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    (source / "a.json").write_bytes(b"tampered\n")
    specs = {"a.json": make_spec("a.json", b"expected\n")}

    with pytest.raises(LargeArtifactError):
        fetch_large.fetch(specs, dest, from_dir=source)
    assert not dest.exists() or list(dest.iterdir()) == []

    with pytest.raises(fetch_large.FetchError):
        fetch_large.fetch(specs, dest, from_dir=tmp_path / "missing")
    with pytest.raises(fetch_large.FetchError):
        fetch_large.fetch(specs, dest, from_dir=source, names=["unknown.json"])


def test_fetch_replaces_a_corrupt_file_and_verify_only_never_writes(tmp_path: Path) -> None:
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    data = b"good bytes\n"
    (source / "a.json").write_bytes(data)
    specs = {"a.json": make_spec("a.json", data)}

    with pytest.raises(LargeArtifactError, match="missing"):
        fetch_large.fetch(specs, dest, verify_only=True)
    (dest / "a.json").write_bytes(b"bad bytes!!\n")
    with pytest.raises(LargeArtifactError):
        fetch_large.fetch(specs, dest, verify_only=True)
    assert (dest / "a.json").read_bytes() == b"bad bytes!!\n"

    (dest / ".a.json.123.part").write_bytes(b"left by an interrupted run")
    result = fetch_large.fetch(specs, dest, from_dir=source)
    assert result["a.json"]["status"] == "replaced-by-copy"
    assert (dest / "a.json").read_bytes() == data
    assert sorted(path.name for path in dest.iterdir()) == ["a.json"]
    assert fetch_large.fetch(specs, dest, verify_only=True)["a.json"]["status"] == "present"


def test_download_streams_verifies_and_refuses_untrusted_urls(tmp_path: Path) -> None:
    data = b"0123456789" * 1000
    spec = make_spec("big.json", data)
    calls: list[str] = []

    def opener(url: str, timeout: float) -> FakeResponse:
        calls.append(url)
        return FakeResponse(data, url)

    result = fetch_large.fetch({"big.json": spec}, tmp_path, opener=opener)
    assert result["big.json"]["status"] == "downloaded"
    assert (tmp_path / "big.json").read_bytes() == data
    assert calls == [spec["url"]]
    assert (
        fetch_large.fetch({"big.json": spec}, tmp_path, opener=opener)["big.json"]["status"]
        == "present"
    )
    assert len(calls) == 1

    def corrupt(url: str, timeout: float) -> FakeResponse:
        return FakeResponse(data[:-1] + b"X", url)

    other = make_spec("other.json", data)
    with pytest.raises(LargeArtifactError):
        fetch_large.fetch({"other.json": other}, tmp_path, opener=corrupt)
    assert not (tmp_path / "other.json").exists()

    def wrong_length(url: str, timeout: float) -> FakeResponse:
        return FakeResponse(data, url, content_length=len(data) + 1)

    with pytest.raises(LargeArtifactError, match="server reports"):
        fetch_large.fetch({"other.json": other}, tmp_path, opener=wrong_length)

    def redirect_elsewhere(url: str, timeout: float) -> FakeResponse:
        return FakeResponse(data, "https://evil.example/other.json")

    with pytest.raises(fetch_large.FetchError, match="non-allowlisted"):
        fetch_large.fetch({"other.json": other}, tmp_path, opener=redirect_elsewhere)

    for url in ("http://raw.githubusercontent.com/o/r/c/x", "https://evil.example/x"):
        with pytest.raises(fetch_large.FetchError):
            fetch_large.fetch(
                {"other.json": make_spec("other.json", data, url)}, tmp_path, opener=opener
            )
    assert sorted(path.name for path in tmp_path.iterdir()) == ["big.json"]


def test_cli_copies_from_a_directory_and_reports_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    data = b"payload\n"
    (source / "a.json").write_bytes(data)
    manifest = tmp_path / "LARGE_ARTIFACTS.json"
    manifest.write_text(
        json.dumps(
            {"schemaVersion": "cua-large-artifacts-v1", "artifacts": [make_spec("a.json", data)]}
        ),
        encoding="utf-8",
    )
    argv = ["--manifest", str(manifest), "--dest", str(dest)]

    assert fetch_large.main([*argv, "--from-dir", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["artifacts"]["a.json"]["status"] == "copied"
    assert fetch_large.main([*argv, "--verify-only"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"

    (dest / "a.json").write_bytes(b"changed\n")
    assert fetch_large.main([*argv, "--verify-only"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "fail"


# ------------------------------------------------------- citation --check modes --


def test_large_artifact_verification_rejects_wrong_files(tmp_path: Path) -> None:
    assert citation.verify_large_artifacts(tmp_path) == {
        "citation_assertions.csv": "absent",
        "citation_graph.json": "absent",
    }
    with pytest.raises(LargeArtifactError, match="fetch_large"):
        citation.verify_large_artifacts(tmp_path, require=True)
    (tmp_path / "citation_graph.json").write_bytes(b"{}\n")
    with pytest.raises(LargeArtifactError, match="bytes"):
        citation.verify_large_artifacts(tmp_path)


def test_small_file_check_needs_no_large_artifacts() -> None:
    outputs = citation.verify_tracked_bundle_without_large_artifacts()
    assert set(outputs) == {"nodes_csv", "edges_csv", "markdown", "qa"}
    for name in outputs:
        assert citation.OUTPUTS[name].parent == GENERATED_DIR


def test_citation_check_cli_passes_in_the_current_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    if citation.private_raw_availability() != "absent":
        pytest.skip("private raw window present; the byte-identical rebuild is tested elsewhere")
    assert citation.main(["--check"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "pass"
    if large_artifacts_present():
        assert summary["mode"] == "tracked-bundle-with-private-raw-absent"
        assert set(summary["largeArtifacts"].values()) == {"verified"}
    else:
        assert summary["mode"] == "tracked-small-files-only-large-artifacts-absent"
        assert set(summary["largeArtifacts"].values()) == {"absent"}
        assert citation.main(["--check", "--require-large"]) == 2
        assert json.loads(capsys.readouterr().out)["status"] == "fail"
    for recorded in summary["outputs"]:
        assert not PurePosixPath(recorded).is_absolute()
