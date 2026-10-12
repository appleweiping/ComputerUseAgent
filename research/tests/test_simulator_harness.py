"""The simulator E2E harness can never write into the archived evidence tree.

These tests run ``run_extension_e2e.mjs`` only in ``--dry-run`` mode or with an
output directory that it must refuse, so no browser is launched and Puppeteer
need not be installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "research" / "scientific_simulators" / "run_extension_e2e.mjs"
ARCHIVED_CONTROL = REPO_ROOT / "research" / "evidence" / "scripted_simulator_control_v0.2.1"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


def _harness(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    clean_env = {
        key: value for key, value in os.environ.items() if not key.startswith("CUA_SIM_E2E_")
    }
    clean_env.update(env or {})
    return subprocess.run(
        [NODE, str(HARNESS), *argv],
        cwd=REPO_ROOT,
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_dry_run_prints_a_plan_outside_the_repository_and_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "out"
    completed = _harness("--dry-run", "--output-dir", str(target), "--reps", "2")
    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["writes_performed"] is False
    assert plan["planned_run_count"] == 6
    assert len(plan["task_ids"]) == 3
    assert plan["extension"]["path"] == "extension/app"
    assert not target.exists()


def test_default_output_is_a_fresh_temp_directory(tmp_path: Path) -> None:
    completed = _harness("--dry-run", env={"TMP": str(tmp_path), "TEMP": str(tmp_path), "TMPDIR": str(tmp_path)})
    assert completed.returncode == 0, completed.stderr
    output = Path(json.loads(completed.stdout)["output_dir"])
    assert tmp_path in output.parents
    assert not output.exists()


@pytest.mark.parametrize(
    "target",
    [
        ARCHIVED_CONTROL,
        ARCHIVED_CONTROL / "runs",
        REPO_ROOT / "research" / "evidence" / "new-simulator-run",
    ],
)
def test_output_inside_research_evidence_is_refused(target: Path) -> None:
    before = _tree_digest(ARCHIVED_CONTROL)
    for mode in (["--dry-run"], []):
        completed = _harness(*mode, "--output-dir", str(target))
        assert completed.returncode == 2
        assert "research/evidence" in completed.stderr
    assert _tree_digest(ARCHIVED_CONTROL) == before
    assert not (REPO_ROOT / "research" / "evidence" / "new-simulator-run").exists()


def test_output_inside_research_evidence_is_refused_through_the_environment() -> None:
    completed = _harness("--dry-run", env={"CUA_SIM_E2E_RESULT_DIR": str(ARCHIVED_CONTROL)})
    assert completed.returncode == 2
    assert "research/evidence" in completed.stderr


def test_non_empty_output_directory_is_refused_and_left_intact(tmp_path: Path) -> None:
    keep = tmp_path / "keep.txt"
    keep.write_text("do not delete", encoding="utf-8")
    completed = _harness("--output-dir", str(tmp_path))
    assert completed.returncode == 2
    assert "new or empty" in completed.stderr
    assert keep.read_text(encoding="utf-8") == "do not delete"


def test_invalid_arguments_are_rejected() -> None:
    for argv in (["--bogus"], ["--reps", "0"], ["--reps", "11"], ["--output-dir"]):
        completed = _harness(*argv)
        assert completed.returncode == 2, argv


def test_harness_has_its_own_pinned_package() -> None:
    package_dir = HARNESS.parent
    package = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
    assert package["private"] is True
    assert package["type"] == "module"
    assert package["devDependencies"] == {"puppeteer": "25.4.0"}
    lock = json.loads((package_dir / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"]["node_modules/puppeteer"]["version"] == "25.4.0"
    assert lock["packages"][""]["devDependencies"] == package["devDependencies"]


def test_task_manifests_use_neutral_descriptive_fields() -> None:
    for path in sorted((HARNESS.parent / "tasks").glob("*.json")):
        task = json.loads(path.read_text(encoding="utf-8"))
        assert task["status"] == "historical-scripted-control-provisional-task-selection"
        assert task["research_boundary"] == (
            "Scripted-automation engineering control. No human trace is collected; the "
            "simulator and task selection was provisional and was never confirmed for a "
            "formal study."
        )
    harness_source = HARNESS.read_text(encoding="utf-8")
    for removed in ("_selection_status", "umn-", "project_week", "research/evidence/scripted"):
        assert removed not in harness_source, removed
