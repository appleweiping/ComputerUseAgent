"""Fail-closed extraction, evaluation, and clean-kernel notebook driver.

Every stage reads the private FP-Agent data root (``--data-root`` or
``$CUA_PRIVATE_ROOT/fp_agent``) and the upstream checkout (``--upstream-repo``
or ``$CUA_FP_AGENT_UPSTREAM_REPO``). Aggregate outputs go to ``--output-dir``
(default ``<data-root>/outputs``); the archived evidence under
``research/evidence/fp_agent`` is never overwritten.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from research.fp_agent.paths import (
    CHECKOUT_ROOT,
    PrivatePathError,
    default_output_dir,
    refuse_evidence_output,
    require_data_root,
    require_upstream_repo,
)
from research.fp_agent.retrospective_features import features_path, raw_dataset_path


def checked(command: list[str]) -> None:
    # ``python -m research.*`` resolves the research package from the checkout.
    subprocess.run(command, cwd=CHECKOUT_ROOT, check=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--upstream-repo", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--rebuild-features", action="store_true")
    parser.add_argument("--download-raw", action="store_true")
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--permutations", type=int, default=1000)
    parser.add_argument("--permutation-workers", type=int, default=4)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        data_root = require_data_root(args.data_root)
        upstream_repo = require_upstream_repo(args.upstream_repo)
        output_dir = refuse_evidence_output(args.output_dir or default_output_dir(data_root))
    except PrivatePathError as error:
        parser.error(str(error))
    python = sys.executable
    location = ["--data-root", str(data_root)]
    if args.download_raw:
        checked(
            [
                python,
                "-m",
                "research.fp_agent.official_range_download",
                *location,
                "--upstream-repo",
                str(upstream_repo),
            ]
        )
    if args.rebuild_features or not features_path(data_root).exists():
        checked(
            [
                python,
                "-m",
                "research.fp_agent.retrospective_features",
                *location,
                "--raw",
                str(raw_dataset_path(data_root)),
                "--output",
                str(features_path(data_root)),
            ]
        )
    checked(
        [
            python,
            "-m",
            "research.fp_agent.retrospective_evaluation",
            *location,
            "--upstream-repo",
            str(upstream_repo),
            "--output-dir",
            str(output_dir),
            "--bootstrap",
            str(args.bootstrap),
            "--permutations",
            str(args.permutations),
            "--permutation-workers",
            str(args.permutation_workers),
        ]
    )
    checked(
        [
            python,
            "-m",
            "research.fp_agent.execute_notebook",
            "--result-dir",
            str(output_dir),
            "--output-dir",
            str(output_dir / "notebook"),
        ]
    )


if __name__ == "__main__":
    main()
