"""Validate and summarize the immutable Codex MiniWoB case matrix.

Run from the repository root: ``python -m research.codex_case.summarize --check``.
``--check`` is read-only; writing a summary needs ``--output`` outside the
archived evidence tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from computer_use_agent.contracts.validation import validate_contract
from research.codex_case.runner import DEFAULT_CONFIG, DEFAULT_RESULTS, case_id, load_config

# The archived case bundles record ``repository_path`` values in the source
# repository's layout.  Their bytes are archival, so the paths are translated
# here instead of being rewritten.
LEGACY_RESULTS_PREFIX = "results/codex_case/"
EVIDENCE_ROOT = DEFAULT_RESULTS.parent
DEFAULT_SUMMARY = DEFAULT_RESULTS / "codex_case_summary.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_recorded_path(recorded: str, results_root: Path) -> Path:
    """Map a ``repository_path`` recorded in a case bundle onto ``results_root``.

    Legacy ``results/codex_case/...`` paths resolve inside ``results_root``.
    Paths already in the current layout resolve against the repository root.
    Absolute paths and ``..`` segments are rejected.
    """

    value = recorded.replace("\\", "/")
    parts = PurePosixPath(value).parts
    if not value or value.startswith("/") or (parts and ":" in parts[0]) or ".." in parts:
        raise ValueError(f"recorded path must be repository-relative: {recorded!r}")
    if value.startswith(LEGACY_RESULTS_PREFIX):
        return results_root / value[len(LEGACY_RESULTS_PREFIX) :]
    return DEFAULT_RESULTS.parents[2] / value


def build_summary(config_path: Path, results_root: Path) -> dict[str, Any]:
    config = load_config(config_path)
    expected = [
        (task, repetition)
        for task in config["tasks"]
        for repetition in task["repetitions"]
    ]
    attempts: list[dict[str, Any]] = []
    for task, repetition in expected:
        identifier = case_id(task["task_id"], repetition["repetition"])
        case_path = results_root / "attempts" / identifier / "case.json"
        if not case_path.is_file():
            raise ValueError(f"missing immutable case bundle: {identifier}")
        payload = json.loads(case_path.read_text(encoding="utf-8"))
        validate_contract(payload, schema_version="cui-codex-case-v1")
        if (
            payload["case_id"] != identifier
            or payload["task_id"] != task["task_id"]
            or payload["repetition"] != repetition["repetition"]
            or payload["seed"] != repetition["seed"]
        ):
            raise ValueError(f"case metadata differs from the frozen matrix: {identifier}")
        for screenshot in payload["screenshots"]:
            screenshot_path = resolve_recorded_path(screenshot["repository_path"], results_root)
            if not screenshot_path.is_file() or sha256_file(screenshot_path) != screenshot["sha256"]:
                raise ValueError(f"screenshot hash mismatch: {identifier}")
        attempts.append(payload)

    outcomes = Counter(item["outcome"]["status"] for item in attempts)
    task_outcomes: dict[str, dict[str, int]] = {}
    for task in config["tasks"]:
        task_rows = [item for item in attempts if item["task_id"] == task["task_id"]]
        task_outcomes[task["task_id"]] = dict(
            sorted(Counter(item["outcome"]["status"] for item in task_rows).items())
        )
    return {
        "schema_version": "cui-codex-case-summary-v1",
        "protocol_version": config["protocol_version"],
        "as_of_utc": max(item["completed_at_utc"] for item in attempts),
        "planned_attempts": len(expected),
        "bundled_attempts": len(attempts),
        "outcomes": dict(sorted(outcomes.items())),
        "task_outcomes": task_outcomes,
        "eligible_recorder_traces": sum(
            item["recorder"]["status"] == "captured" for item in attempts
        ),
        "official_rewards_available": sum(
            item["validator"]["reward"] is not None for item in attempts
        ),
        "screenshots": sum(len(item["screenshots"]) for item in attempts),
        "manual_rescue_attempts": sum(not item["no_manual_rescue"] for item in attempts),
        "scripted_control_attempts": sum(not item["no_scripted_control"] for item in attempts),
        "formal_claim_permitted": False,
        "claim_boundary": config["claim_boundary"],
        "attempts": [
            {
                "case_id": item["case_id"],
                "task_id": item["task_id"],
                "repetition": item["repetition"],
                "seed": item["seed"],
                "status": item["outcome"]["status"],
                "failure_code": item["outcome"]["failure_code"],
                "action_count": item["action_count"],
                "case_sha256": sha256_file(
                    results_root / "attempts" / item["case_id"] / "case.json"
                ),
            }
            for item in attempts
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument(
        "--output",
        type=Path,
        help=f"Summary to compare (--check, default {DEFAULT_SUMMARY.name}) or to write",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    summary = build_summary(args.config.resolve(), args.results.resolve())
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.check:
        expected = args.output or args.results / DEFAULT_SUMMARY.name
        if not expected.is_file() or expected.read_text(encoding="utf-8") != rendered:
            raise SystemExit("Codex case summary is missing or stale")
        print(json.dumps({"status": "pass", "attempts": summary["bundled_attempts"]}))
        return 0
    if args.output is None:
        parser.error("--output is required unless --check is used")
    evidence = EVIDENCE_ROOT.resolve()
    if evidence == args.output.resolve() or evidence in args.output.resolve().parents:
        parser.error("--output must not be inside research/evidence; archived evidence is read-only")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(json.dumps({"status": "written", "attempts": summary["bundled_attempts"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
