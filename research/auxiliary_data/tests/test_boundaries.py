from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("requests", reason="needs requests from the research extra")

import requests

from research.auxiliary_data import sources, tracks
from research.auxiliary_data.paths import EVIDENCE_DIR
from research.auxiliary_data.remote import (
    USER_AGENT,
    PublicAccessError,
    PublicRangeReader,
    _allowed_public_url,
    public_session,
)
from research.auxiliary_data.run import _blocked, build_parser, run_all


def test_public_session_never_sets_authorization() -> None:
    session = public_session()
    try:
        assert "Authorization" not in session.headers
        assert session.headers["User-Agent"] == USER_AGENT
    finally:
        session.close()


def test_user_agent_names_the_project_neutrally() -> None:
    assert USER_AGENT.startswith("ComputerUseAgent-research/")
    assert "https://github.com/appleweiping/ComputerUseAgent" in USER_AGENT


def test_range_reader_rejects_lookalike_or_non_https_hosts() -> None:
    with pytest.raises(PublicAccessError):
        PublicRangeReader("https://evilhuggingface.co/file", 10)
    with pytest.raises(PublicAccessError):
        PublicRangeReader("http://huggingface.co/file", 10)


@pytest.mark.parametrize(
    "url",
    [
        "https://huggingface.co/api/datasets/x",
        "https://cdn-lfs.huggingface.co/repos/x",
        "https://cdn-lfs-us-1.hf.co/repos/x",
        "https://cas-bridge.xethub.hf.co/xet-bridge-us/x",
        "https://userinterfaces.aalto.fi/136Mkeystrokes/data/Keystrokes.zip",
    ],
)
def test_allowlist_accepts_provider_hosts(url: str) -> None:
    assert _allowed_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://some-bucket.s3.amazonaws.com/x",
        "https://s3.amazonaws.com/x",
        "https://evil.example/huggingface.co",
        "https://huggingface.co.evil.example/x",
        "https://hf.co.evil.example/x",
        "https://evil-aalto.fi/x",
        "https://sub.userinterfaces.aalto.fi/x",
        "http://cdn-lfs.hf.co/x",
    ],
)
def test_allowlist_rejects_other_hosts(url: str) -> None:
    assert not _allowed_public_url(url)


def test_disallowed_redirect_is_refused_before_it_is_followed() -> None:
    session = public_session()
    response = requests.Response()
    response.status_code = 302
    response.url = "https://huggingface.co/datasets/x/resolve/main/a.parquet"
    try:
        response.headers["Location"] = "https://bucket.s3.amazonaws.com/a.parquet"
        with pytest.raises(PublicAccessError, match="non-allowlisted"):
            session.get_redirect_target(response)
        response.headers["Location"] = "https://cas-bridge.xethub.hf.co/a.parquet"
        assert session.get_redirect_target(response) == "https://cas-bridge.xethub.hf.co/a.parquet"
        response.headers["Location"] = "/datasets/x/resolve/abc/a.parquet"
        assert session.get_redirect_target(response) == "/datasets/x/resolve/abc/a.parquet"
    finally:
        session.close()


def test_source_restrictions_are_locked() -> None:
    captcha_terms = " ".join(sources.CAPTCHA_SOURCE.restrictions).lower()
    assert "re-identification" in captcha_terms
    assert "biometric" in captcha_terms
    assert "authentication" in captcha_terms
    assert sources.MOLMOWEB_HUMAN.license_id == "ODC-BY-1.0"


def test_track_module_has_no_fp_agent_or_timing_model_dependency() -> None:
    source = inspect.getsource(tracks)
    assert "research.fp_agent" not in source
    assert "retrospective_features" not in source
    assert "23-feature" not in source


def test_blocked_result_is_non_row_level() -> None:
    result = _blocked("fixture", "unavailable")
    assert result["status"] == "blocked"
    assert result["row_level_output"] is False
    assert result["raw_public_output"] is False


def test_run_requires_an_explicit_output_dir_and_refuses_evidence() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--retrieved-at-utc", "2026-08-11T14:00:00Z"])
    with pytest.raises(ValueError, match="archival"):
        run_all(
            output_dir=EVIDENCE_DIR,
            retrieved_at_utc="2026-08-11T14:00:00Z",
            max_workers=1,
            selected_tracks=set(),
        )


def test_notebook_section_order() -> None:
    notebook = Path(tracks.__file__).parent / "notebooks" / "auxiliary_calibration.py"
    text = notebook.read_text(encoding="utf-8")
    positions = [
        text.index("## tl;dr"),
        text.index("## Context & Methods"),
        text.index("## Data"),
        text.index("## Results"),
        text.index("## Takeaways"),
    ]
    assert positions == sorted(positions)
