from __future__ import annotations

import pytest

from research.fp_agent import official_range_download as downloader
from research.fp_agent.official_range_download import (
    DownloadLayout,
    chunk_plan,
    parse_content_range,
    share_link_query,
)
from research.fp_agent.paths import USER_AGENT


def test_chunk_plan_is_contiguous_and_exact() -> None:
    chunks = chunk_plan(total=25, chunk_bytes=8)
    assert [(chunk.start, chunk.end, chunk.size) for chunk in chunks] == [
        (0, 7, 8),
        (8, 15, 8),
        (16, 23, 8),
        (24, 24, 1),
    ]


def test_content_range_parser_fails_closed() -> None:
    assert parse_content_range("bytes 10-19/100") == (10, 19, 100)
    with pytest.raises(ValueError):
        parse_content_range("bytes */100")


def test_every_downloaded_file_stays_inside_the_data_root(tmp_path) -> None:
    layout = DownloadLayout(tmp_path)
    paths = [layout.final_path, layout.linear_partial, layout.parts_dir, layout.ledger_path]
    paths.append(chunk_plan(total=10, chunk_bytes=4)[1].path(layout.parts_dir))
    assert all(path.is_relative_to(tmp_path) for path in paths)
    assert layout.final_path.name == "raw_dataset.json"


def test_share_link_query_is_read_from_the_upstream_readme(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        "Data: [OSF](https://osf.io/j6b5p/overview?share=0123abcd) and https://osf.io/j6b5p/\n",
        encoding="utf-8",
    )
    assert share_link_query(readme) == "share=0123abcd"


def test_share_link_query_accepts_a_public_link_and_rejects_a_missing_one(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("Project page: https://osf.io/j6b5p/\n", encoding="utf-8")
    assert share_link_query(readme) == ""
    readme.write_text("Another node: https://osf.io/j6b5px/?share=1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="links no OSF share"):
        share_link_query(readme)


def test_share_link_query_rejects_ambiguous_links(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        "https://osf.io/j6b5p/?share=aaaa https://osf.io/j6b5p/?share=bbbb\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        share_link_query(readme)


def test_requests_use_the_neutral_user_agent() -> None:
    assert USER_AGENT.startswith("ComputerUseAgent-research/")
    assert "https://github.com/appleweiping/ComputerUseAgent" in USER_AGENT


def test_redirects_are_limited_to_osf_storage_hosts() -> None:
    assert frozenset({"osf.io", "files.osf.io", "storage.googleapis.com"}) == (
        downloader.ALLOWED_REDIRECT_HOSTS
    )
