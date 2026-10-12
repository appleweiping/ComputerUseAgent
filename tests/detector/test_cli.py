from __future__ import annotations

import json
import subprocess
import sys

from computer_use_agent.detector.contract import DATASET_SCHEMA


def test_pipeline_module_cli_builds_and_verifies_bundle(tmp_path, synthetic_records):
    dataset = tmp_path / "dataset.json"
    output = tmp_path / "analysis"
    dataset.write_text(
        json.dumps({"schema_version": DATASET_SCHEMA, "records": synthetic_records}),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "computer_use_agent.detector.pipeline",
            "--dataset",
            str(dataset),
            "--output-dir",
            str(output),
            "--profile",
            "smoke",
            "--epochs",
            "2",
            "--bootstrap",
            "2",
            "--permutations",
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    assert (output / "detector-result-bundle.zip").is_file()
    assert (output / "MANIFEST.sha256").is_file()
    assert (output / "trajectory_projection.json").is_file()
    summary = json.loads(completed.stdout)
    assert "trajectory_projection.json" in summary["hashes"]
    assert "Synthetic pipeline check only" in completed.stdout


def test_pipeline_module_cli_reports_errors_without_traceback(tmp_path):
    from computer_use_agent.detector.pipeline import main

    output = tmp_path / "exists"
    output.mkdir()
    dataset = tmp_path / "dataset.json"
    dataset.write_text("{}", encoding="utf-8")
    assert main(["--dataset", str(dataset), "--output-dir", str(output)]) == 1
    assert main(["--dataset", str(dataset), "--output-dir", str(tmp_path / "new")]) == 1
