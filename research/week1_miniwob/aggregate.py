"""Rebuild a Week 1 matrix summary from retained attempt artifacts.

Run from the repository root: ``python -m research.week1_miniwob.aggregate --input-dir DIR``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .protocol import aggregate_records, build_matrix, load_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("benchmark_manifest.json"))
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest.resolve())
    planned = build_matrix(manifest)
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(args.input_dir.glob("*.json"))]
    summary = aggregate_records(records, planned)
    text = json.dumps(summary, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if summary["matrix_artifacts_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
