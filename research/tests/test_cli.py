"""The research dispatcher plans every stage, runs one stage, and never writes in a dry run."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from research import reproduce

REPO_ROOT = Path(__file__).resolve().parents[2]


def _dry_run(*argv: str) -> tuple[int, dict]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), mock.patch.object(reproduce.subprocess, "run") as run:
        code = reproduce.main(["--dry-run", *argv])
    run.assert_not_called()
    return code, json.loads(output.getvalue())


def test_dry_run_plans_every_stage_and_runs_nothing() -> None:
    code, payload = _dry_run()
    assert code == 0
    assert payload["schema_version"] == "cua-research-reproduce-plan-v1"
    assert payload["executed"] is False
    assert payload["writes_performed"] is False
    assert [row["stage"] for row in payload["stages"]] == list(reproduce.STAGES)
    for row in payload["stages"]:
        assert row["entry_point_found"], row["stage"]


@pytest.mark.parametrize("stage", list(reproduce.STAGES))
def test_each_stage_dry_run_is_a_single_shell_free_command(stage: str) -> None:
    code, payload = _dry_run("--stage", stage)
    assert code == 0
    (row,) = payload["stages"]
    assert row["stage"] == stage
    command = row["command"]
    assert command[0] in {"python", "node", "npm"}
    assert not any(token in command for token in ("powershell", "cmd", "/c", "-Command", "sh", "-c"))
    if command[0] == "python":
        assert command[1:3] == ["-B", "-m"]
        assert command[3].startswith("research.")


def test_archived_harness_stages_only_use_non_writing_modes() -> None:
    expected_flags = {
        "week1": "--dry-run",
        "scienceboard": "--dry-run",
        "codex-case": "--check",
        "skyvern-audit": "--audit-existing",
        "simulator-plan": "--dry-run",
        "literature": "--check",
        "citation-graph": "--check",
    }
    for stage, flag in expected_flags.items():
        assert flag in reproduce.STAGES[stage].argv, stage
        assert reproduce.STAGES[stage].live_writes == "nothing", stage
    assert "--write" not in reproduce.STAGES["skyvern-audit"].argv


def test_live_stage_runs_its_command_from_the_repository_root_with_extra_args() -> None:
    completed = subprocess.CompletedProcess([], 0)
    with mock.patch.object(reproduce.subprocess, "run", return_value=completed) as run:
        assert reproduce.main(["--stage", "codex-case", "--", "--results", "elsewhere"]) == 0
    command = run.call_args.args[0]
    assert command == [
        sys.executable,
        "-B",
        "-m",
        "research.codex_case.summarize",
        "--check",
        "--results",
        "elsewhere",
    ]
    assert run.call_args.kwargs["cwd"] == reproduce.REPO_ROOT
    assert run.call_args.kwargs.get("shell", False) is False


def test_live_stage_reports_a_missing_executable_without_running() -> None:
    with mock.patch.object(reproduce.shutil, "which", return_value=None), mock.patch.object(
        reproduce.subprocess, "run"
    ) as run, contextlib.redirect_stderr(io.StringIO()) as error:
        assert reproduce.main(["--stage", "simulator-validators"]) == 2
    run.assert_not_called()
    assert "node" in error.getvalue()


def test_missing_entry_point_fails_the_dry_run() -> None:
    broken = reproduce.Stage("broken", ("{python}", "-B", "-m", "research.no_such_area"), "research.no_such_area", "module")
    with mock.patch.dict(reproduce.STAGES, {"week1": broken}):
        code, payload = _dry_run("--stage", "week1")
    assert code == 2
    assert payload["stages"][0]["entry_point_found"] is False


@pytest.mark.parametrize(
    "argv",
    [
        [],  # running every stage live is not supported
        ["--stage", "all"],
        ["--dry-run", "--", "--extra"],  # extra args need a single stage
        ["--stage", "deliverables"],  # dropped stages are not accepted
        ["--stage", "release-check"],
    ],
)
def test_argument_scope_is_fail_closed(argv: list[str]) -> None:
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as raised:
        reproduce.parse_args(argv)
    assert raised.value.code == 2


def test_module_entry_point_dry_run_from_a_subprocess() -> None:
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "research.reproduce", "--dry-run", "--stage", "week1"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["stages"][0]["stage"] == "week1"


def test_dropped_entry_points_are_gone() -> None:
    assert not (REPO_ROOT / "research" / "doctor.py").exists()
    assert "release-check" not in reproduce.STAGES
    assert "deliverables" not in reproduce.STAGES
