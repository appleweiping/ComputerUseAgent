from __future__ import annotations

from pathlib import Path

import pytest

from research.fp_agent import (
    evaluate_fp_agent_classifiers,
    execute_notebook,
    leave_one_family_out,
    paths,
)


def test_legacy_evidence_paths_translate_to_the_new_layout() -> None:
    assert paths.resolve_recorded_path("results/fp_agent/figures/a.png") == (
        paths.CHECKOUT_ROOT / "research" / "evidence" / "fp_agent" / "figures" / "a.png"
    )
    assert paths.resolve_recorded_path("experiments/fp_agent/notebooks/retrospective.py") == (
        paths.CHECKOUT_ROOT / "research" / "fp_agent" / "notebooks" / "retrospective.py"
    )
    assert paths.resolve_recorded_path("research/fp_agent/README.md") == (
        paths.CHECKOUT_ROOT / "research" / "fp_agent" / "README.md"
    )


def test_data_root_comes_from_the_flag_or_the_private_root(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(paths.PRIVATE_ROOT_ENV, raising=False)
    with pytest.raises(paths.PrivatePathError, match="--data-root"):
        paths.require_data_root(None)
    assert paths.require_data_root(tmp_path) == tmp_path.resolve()
    monkeypatch.setenv(paths.PRIVATE_ROOT_ENV, str(tmp_path))
    assert paths.require_data_root(None) == (tmp_path / "fp_agent").resolve()


def test_data_root_inside_the_checkout_is_refused() -> None:
    with pytest.raises(paths.PrivatePathError, match="outside the repository"):
        paths.require_data_root(paths.CHECKOUT_ROOT / "private_data" / "fp_agent")


def test_upstream_repo_comes_from_the_flag_or_the_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(paths.UPSTREAM_REPO_ENV, raising=False)
    with pytest.raises(paths.PrivatePathError, match="--upstream-repo"):
        paths.require_upstream_repo(None)
    monkeypatch.setenv(paths.UPSTREAM_REPO_ENV, str(tmp_path))
    assert paths.require_upstream_repo(None) == tmp_path.resolve()


def test_reruns_never_write_into_archived_evidence(tmp_path) -> None:
    with pytest.raises(paths.PrivatePathError, match="archival"):
        paths.refuse_evidence_output(paths.EVIDENCE_DIR / "fp_agent_loafo.json")
    assert paths.refuse_evidence_output(tmp_path / "out.json") == (tmp_path / "out.json").resolve()


def test_cli_without_a_data_root_exits_with_a_usage_error(monkeypatch, capsys) -> None:
    monkeypatch.delenv(paths.PRIVATE_ROOT_ENV, raising=False)
    for main in (evaluate_fp_agent_classifiers.main, leave_one_family_out.main):
        with pytest.raises(SystemExit) as raised:
            main(["--upstream-repo", "unused"])
        assert raised.value.code == 2
        assert "--data-root" in capsys.readouterr().err


def test_cli_refuses_an_evidence_output(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        leave_one_family_out.main(
            [
                "--data-root",
                str(tmp_path),
                "--upstream-repo",
                str(tmp_path),
                "--output-json",
                str(paths.EVIDENCE_DIR / "fp_agent_loafo.json"),
            ]
        )
    assert raised.value.code == 2
    assert "archival" in capsys.readouterr().err


def test_new_outputs_name_only_the_public_osf_project() -> None:
    assert paths.OSF_PROJECT_URL == "https://osf.io/j6b5p/"
    # The access-key query parameter must not appear in any analysis source; the
    # needle is assembled so that this file does not match a repository-wide grep.
    needle = "_".join(("view", "only"))
    for directory in (Path(paths.PACKAGE_DIR), paths.RESEARCH_ROOT / "auxiliary_data"):
        for source in directory.rglob("*.py"):
            assert needle not in source.read_text(encoding="utf-8"), source


def test_notebook_execution_never_writes_into_evidence() -> None:
    with pytest.raises(paths.PrivatePathError, match="archival"):
        execute_notebook.build_and_execute(output_dir=paths.EVIDENCE_DIR / "build")
