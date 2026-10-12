"""Research reproduction dispatcher: one command per research area.

Each stage is a single command (``python -m research.<area>...``, ``node`` or
``npm``) run from the repository root.  ``--dry-run`` prints the plan for one
stage, or for every stage with ``--stage all``; it checks that each entry point
exists, runs nothing and writes nothing.  Without ``--dry-run`` the selected
stage's command is executed.  Arguments after ``--`` are appended to it.

The archived harnesses cannot run a study from this repository: their stages
execute only plan, check or audit modes that write nothing.  Product commands
(``validate``, ``extract``, ``analyze``, ``doctor``) belong to the
``computer-use-agent`` CLI, not to this dispatcher.

    python -B -m research.reproduce --dry-run
    python -B -m research.reproduce --stage codex-case
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_SCHEMA = "cua-research-reproduce-plan-v1"
PYTHON = "{python}"


@dataclass(frozen=True)
class Stage:
    """One research-area command and what a live run of it does."""

    summary: str
    argv: tuple[str, ...]
    entry_point: str
    entry_kind: str  # "module", "file" or "npm-script"
    live_writes: str = "nothing"

    @property
    def executable(self) -> str:
        return self.argv[0]


STAGES: dict[str, Stage] = {
    "week1": Stage(
        "Week 1 MiniWoB harness (frozen against recorder 0.4.0, cannot be launched): "
        "print the 27-cell plan and preflight",
        (PYTHON, "-B", "-m", "research.week1_miniwob.runner", "--dry-run"),
        "research.week1_miniwob.runner",
        "module",
    ),
    "scienceboard": Stage(
        "ScienceBoard harness (archived, never run on a VM): print the 27-cell plan",
        (PYTHON, "-B", "-m", "research.scienceboard.orchestrate", "--dry-run"),
        "research.scienceboard.orchestrate",
        "module",
    ),
    "codex-case": Stage(
        "Codex Desktop case study: re-validate the nine blocked bundles and their summary",
        (PYTHON, "-B", "-m", "research.codex_case.summarize", "--check"),
        "research.codex_case.summarize",
        "module",
    ),
    "skyvern-audit": Stage(
        "Skyvern pilot: offline, read-only re-audit of the archived sanitized runs",
        (
            PYTHON,
            "-B",
            "-m",
            "research.pilots.skyvern.runner",
            "--audit-existing",
            "research/evidence/pilots/skyvern/sanitized_runs.json",
            "--repetitions",
            "3",
            "--engine",
            "skyvern-2.0",
            "--max-steps",
            "7",
            "--cost-cap-usd",
            "2.5",
        ),
        "research.pilots.skyvern.runner",
        "module",
    ),
    "simulator-validators": Stage(
        "Scientific simulator control: offline validator and runner-policy tests",
        (
            "node",
            "--test",
            "research/scientific_simulators/validator.test.mjs",
            "research/scientific_simulators/runner_policy.test.mjs",
        ),
        "research/scientific_simulators/validator.test.mjs",
        "file",
    ),
    "simulator-plan": Stage(
        "Scientific simulator control: validate the task manifests and print the run plan",
        ("node", "research/scientific_simulators/run_extension_e2e.mjs", "--dry-run"),
        "research/scientific_simulators/run_extension_e2e.mjs",
        "file",
    ),
    "retrospective-config": Stage(
        "FP-Agent retrospective: validate the retrospective study configuration",
        (
            PYTHON,
            "-B",
            "-m",
            "research.fp_agent.validate_config",
            "--config",
            "research/configs/retrospective-study.json",
        ),
        "research.fp_agent.validate_config",
        "module",
    ),
    "literature": Stage(
        "Literature corpus: check the generated artifacts against their sources",
        (PYTHON, "-B", "-m", "research.literature.scripts.build_literature_artifacts", "--check"),
        "research.literature.scripts.build_literature_artifacts",
        "module",
    ),
    "citation-graph": Stage(
        "Citation graph: check the generated citation artifacts (large artifacts when present)",
        (PYTHON, "-B", "-m", "research.literature.scripts.build_citation_artifacts", "--check"),
        "research.literature.scripts.build_citation_artifacts",
        "module",
    ),
    "recorder-e2e": Stage(
        "Recorder extension E2E suite in an isolated Chrome profile",
        ("npm", "--prefix", "extension", "run", "e2e"),
        "extension/package.json#scripts.e2e",
        "npm-script",
        "an E2E bundle under CUA_E2E_RESULT_DIR (default: the OS temp directory)",
    ),
}


def entry_point_found(stage: Stage) -> bool:
    """Return whether the stage's entry point exists in this checkout (nothing is imported)."""

    if stage.entry_kind == "module":
        try:
            return importlib.util.find_spec(stage.entry_point) is not None
        except (ImportError, ValueError):
            return False
    if stage.entry_kind == "file":
        return (REPO_ROOT / stage.entry_point).is_file()
    package_path, _, script = stage.entry_point.partition("#scripts.")
    try:
        package = json.loads((REPO_ROOT / package_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(package.get("scripts"), dict) and script in package["scripts"]


def stage_command(stage: Stage, extra: Sequence[str] = ()) -> list[str]:
    """Return the display command (``python`` stands for the current interpreter)."""

    return ["python" if part == PYTHON else part for part in stage.argv] + list(extra)


def resolve_executable(stage: Stage) -> str | None:
    if stage.executable == PYTHON:
        return sys.executable
    return shutil.which(stage.executable)


def plan(names: Sequence[str], extra: Sequence[str] = ()) -> dict[str, Any]:
    rows = []
    for name in names:
        stage = STAGES[name]
        rows.append(
            {
                "stage": name,
                "summary": stage.summary,
                "command": stage_command(stage, extra),
                "entry_point": stage.entry_point,
                "entry_point_found": entry_point_found(stage),
                "executable_available": resolve_executable(stage) is not None,
                "live_run_writes": stage.live_writes,
            }
        )
    return {
        "schema_version": PLAN_SCHEMA,
        "cwd": "repository root",
        "stages": rows,
        "executed": False,
        "writes_performed": False,
    }


def run(name: str, extra: Sequence[str] = ()) -> int:
    stage = STAGES[name]
    executable = resolve_executable(stage)
    if executable is None:
        print(f"{name}: required executable is not available: {stage.executable}", file=sys.stderr)
        return 2
    if not entry_point_found(stage):
        print(f"{name}: entry point not found: {stage.entry_point}", file=sys.stderr)
        return 2
    command = [executable, *stage.argv[1:], *extra]
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--stage",
        choices=[*STAGES, "all"],
        default="all",
        help="research stage to plan or run (default: all, which requires --dry-run)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan; run nothing, write nothing"
    )
    parser.add_argument(
        "extra", nargs=argparse.REMAINDER, help="arguments after -- are appended to the stage"
    )
    args = parser.parse_args(argv)
    if args.extra and args.extra[0] == "--":
        args.extra = args.extra[1:]
    if args.stage == "all" and not args.dry_run:
        parser.error("running every stage is not supported; choose --stage or add --dry-run")
    if args.stage == "all" and args.extra:
        parser.error("extra stage arguments require a single --stage")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    names = list(STAGES) if args.stage == "all" else [args.stage]
    if args.dry_run:
        payload = plan(names, args.extra)
        print(json.dumps(payload, indent=2))
        return 0 if all(row["entry_point_found"] for row in payload["stages"]) else 2
    return run(args.stage, args.extra)


if __name__ == "__main__":
    raise SystemExit(main())
