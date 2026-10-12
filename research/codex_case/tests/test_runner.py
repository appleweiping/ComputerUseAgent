from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from research.codex_case import runner, summarize
from research.codex_case.runner import (
    DEFAULT_CONFIG,
    DEFAULT_RESULTS,
    build_attempt,
    case_id,
    load_config,
    parse_args,
    select_cell,
)
from research.codex_case.summarize import build_summary, resolve_recorded_path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_matrix_is_exactly_nine_unique_cells() -> None:
    config = load_config()
    cells = [
        (task["task_id"], item["repetition"], item["seed"])
        for task in config["tasks"]
        for item in task["repetitions"]
    ]
    assert len(cells) == 9
    assert len(set(cells)) == 9


def test_attempt_is_exploratory_and_bounded() -> None:
    config = load_config()
    task, seed = select_cell(config, "enter-text", 2)
    attempt = build_attempt(config, task, 2, seed)
    assert attempt["attempt_id"] == case_id("enter-text", 2)
    assert attempt["framework"]["id"] == "codex-desktop"
    assert attempt["limits"] == {"max_actions": 30, "timeout_seconds": 480}
    assert config["controls"]["formal_claim_permitted"] is False
    assert config["controls"]["scripted_control_allowed"] is False
    assert config["controls"]["manual_rescue_allowed"] is False


def test_blocked_mode_is_explicit_and_uses_frozen_failure_code() -> None:
    args = parse_args(
        [
            "--task",
            "choose-list",
            "--repetition",
            "3",
            "--results",
            "new-codex-results",
            "--private-root",
            "private-cache",
            "--miniwob-root",
            "miniwob-source",
            "--record-blocked",
        ]
    )
    assert args.record_blocked is True
    assert args.blocker_code == "codex_interaction_surfaces_unavailable"


def test_runner_refuses_to_write_into_archived_evidence(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(runner, "record_blocked_case", lambda *_args: called.append("ran"))
    for results in (DEFAULT_RESULTS, DEFAULT_RESULTS / "attempts", REPO_ROOT / "research" / "evidence"):
        code = runner.main(
            [
                "--task",
                "click-checkboxes",
                "--repetition",
                "1",
                "--results",
                str(results),
                "--private-root",
                "private-cache",
                "--miniwob-root",
                "miniwob-source",
                "--record-blocked",
            ]
        )
        assert code == 2
        assert "results_in_archived_evidence" in capsys.readouterr().err
    assert called == []


def test_results_directory_is_required() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--task", "enter-text", "--repetition", "1", "--private-root", "p", "--miniwob-root", "m"])


def test_checked_in_matrix_has_all_nine_honest_bundles() -> None:
    summary = build_summary(
        REPO_ROOT / "research" / "configs" / "codex-case.json",
        REPO_ROOT / "research" / "evidence" / "codex_case_blocked",
    )
    assert summary["planned_attempts"] == 9
    assert summary["bundled_attempts"] == 9
    assert summary["outcomes"] == {"blocked": 9}
    assert summary["eligible_recorder_traces"] == 0
    assert summary["official_rewards_available"] == 0
    assert summary["scripted_control_attempts"] == 0


def test_check_mode_matches_the_archived_summary_without_writing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary_path = DEFAULT_RESULTS / "codex_case_summary.json"
    before = summary_path.read_bytes()
    assert summarize.main(["--check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "pass", "attempts": 9}
    assert summary_path.read_bytes() == before


def test_writing_a_summary_into_archived_evidence_is_refused() -> None:
    with pytest.raises(SystemExit):
        summarize.main(["--output", str(DEFAULT_RESULTS / "codex_case_summary.json")])
    with pytest.raises(SystemExit):
        summarize.main([])


def test_recorded_legacy_paths_resolve_inside_the_results_root(tmp_path: Path) -> None:
    recorded = "results/codex_case/attempts/codex-miniwob-enter-text-r2-20260811/initial.png"
    assert resolve_recorded_path(recorded, tmp_path) == (
        tmp_path / "attempts" / "codex-miniwob-enter-text-r2-20260811" / "initial.png"
    )
    current = "research/evidence/codex_case_blocked/surface_preflight.json"
    assert resolve_recorded_path(current, tmp_path) == REPO_ROOT / current
    for bad in ("", "/abs/file.png", "C:/x.png", "results/codex_case/../../secret"):
        with pytest.raises(ValueError):
            resolve_recorded_path(bad, tmp_path)


def test_every_archived_screenshot_path_is_legacy_and_resolvable() -> None:
    for case_path in sorted((DEFAULT_RESULTS / "attempts").glob("*/case.json")):
        case = json.loads(case_path.read_text(encoding="utf-8"))
        for screenshot in case["screenshots"]:
            assert screenshot["repository_path"].startswith("results/codex_case/")
            assert resolve_recorded_path(screenshot["repository_path"], DEFAULT_RESULTS).is_file()


def test_protocol_config_is_still_bound_by_the_archived_source_manifest() -> None:
    manifest = json.loads((DEFAULT_RESULTS / "source_manifest.json").read_text(encoding="utf-8"))
    recorded = {row["source_id"]: row for row in manifest["sources"]}["codex-case-config"]
    assert recorded["locator"]["value"] == "configs/codex-case.json"
    assert recorded["sha256"] == hashlib.sha256(DEFAULT_CONFIG.read_bytes()).hexdigest()


def _blocked_argv(results: Path, *extra: str) -> list[str]:
    return [
        "--task",
        "click-checkboxes",
        "--repetition",
        "1",
        "--results",
        str(results),
        "--private-root",
        "private-cache",
        "--miniwob-root",
        "miniwob-source",
        "--record-blocked",
        *extra,
    ]


def _write_recorder_manifest(path: Path, version: str) -> Path:
    path.write_text(json.dumps({"manifest_version": 3, "version": version}), encoding="utf-8")
    return path


def test_runner_refuses_the_live_recorder(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = load_config()["recorder"]["version"]
    live = json.loads(runner.RECORDER_MANIFEST.read_text(encoding="utf-8"))["version"]
    assert pinned == "0.4.0"
    assert live != pinned, "the Codex harness is frozen against recorder 0.4.0"
    called: list[str] = []
    monkeypatch.setattr(runner, "record_blocked_case", lambda *_args: called.append("blocked"))
    monkeypatch.setattr(runner, "run_case", lambda *_args: called.append("run"))
    results = REPO_ROOT / "build" / "codex-case-frozen-runner-test"
    blocked = _blocked_argv(results)
    for argv in (blocked, [arg for arg in blocked if arg != "--record-blocked"]):
        assert runner.main(argv) == 2
        error = json.loads(capsys.readouterr().err)
        assert error["error_code"] == "recorder_version_mismatch"
        assert f"recorder {live}" in error["detail"]
        assert f"require recorder {pinned}" in error["detail"]
    assert called == []
    assert not results.exists()


def test_frozen_recorder_check_accepts_only_the_pinned_version(tmp_path: Path) -> None:
    config = load_config()
    manifest = tmp_path / "manifest.json"
    pinned = _write_recorder_manifest(manifest, "0.4.0")
    assert runner.require_frozen_recorder(config, pinned) == "0.4.0"
    for version in ("0.5.0", "0.4.1", ""):
        with pytest.raises(runner.RecorderVersionError, match="require recorder 0.4.0"):
            runner.require_frozen_recorder(config, _write_recorder_manifest(manifest, version))
    with pytest.raises(runner.RecorderVersionError, match="cannot read"):
        runner.require_frozen_recorder(config, tmp_path / "missing.json")
    manifest.write_text("[]", encoding="utf-8")
    with pytest.raises(runner.RecorderVersionError):
        runner.require_frozen_recorder(config, manifest)


def test_pinned_recorder_passes_the_gate(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        runner, "RECORDER_MANIFEST", _write_recorder_manifest(tmp_path / "manifest.json", "0.4.0")
    )
    stub_case = {
        "case_id": case_id("click-checkboxes", 1),
        "outcome": {"status": "blocked"},
        "validator": {"reward": None},
        "recorder": {"event_count": 0},
    }
    monkeypatch.setattr(runner, "record_blocked_case", lambda *_args: stub_case)
    assert runner.main(_blocked_argv(REPO_ROOT / "build" / "codex-case-gate-test")) == 0
    assert json.loads(capsys.readouterr().out)["case_id"] == stub_case["case_id"]


def test_invalid_config_is_reported_before_any_case_runs(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called: list[str] = []
    monkeypatch.setattr(runner, "record_blocked_case", lambda *_args: called.append("blocked"))
    bad = tmp_path / "codex-case.json"
    bad.write_text("{", encoding="utf-8")
    argv = _blocked_argv(REPO_ROOT / "build" / "codex-case-config-test", "--config", str(bad))
    assert runner.main(argv) == 2
    assert json.loads(capsys.readouterr().err)["error_code"] == "invalid_config"
    assert called == []


def test_archived_source_manifest_is_reused_and_never_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, sha = runner.archived_source_manifest()
    archived = DEFAULT_RESULTS / "source_manifest.json"
    assert sha == hashlib.sha256(archived.read_bytes()).hexdigest()
    reference = runner._source_manifest_ref(manifest, sha)
    assert reference == {
        "schema_version": "cui-source-manifest-v1",
        "manifest_id": "codex-miniwob-source-manifest-20260811",
        "repository_path": "research/evidence/codex_case_blocked/source_manifest.json",
        "sha256": sha,
    }
    assert resolve_recorded_path(reference["repository_path"], tmp_path) == archived

    missing = tmp_path / "source_manifest.json"
    monkeypatch.setattr(runner, "SOURCE_MANIFEST_PATH", missing)
    with pytest.raises(runner.CaseError, match="never writes a new one"):
        runner.archived_source_manifest()
    assert not missing.exists()
