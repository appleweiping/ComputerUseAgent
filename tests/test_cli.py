"""The ``computer-use-agent`` console script."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from computer_use_agent import __version__, cli
from computer_use_agent.detector.contract import RECORD_SCHEMA_V1
from computer_use_agent.schemas import SCHEMA_FILES, schema_text
from tests.fixtures.synthetic import (
    write_synthetic_dataset,
    write_synthetic_x11_trace,
    write_trace_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"
FAST = ["--epochs", "2", "--bootstrap", "1", "--permutations", "1"]


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_version_and_module_entry_point():
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "computer_use_agent", "--version"],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert completed.stdout.strip() == f"computer-use-agent {__version__}"


def test_a_subcommand_is_required(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main([])
    assert raised.value.code == 2
    assert "COMMAND" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("schema", "path", "records"),
    [
        ("cui-trace-v2", EXAMPLES / "traces" / "minimal-session-v2.json", 1),
        ("cui-trace-v1", EXAMPLES / "traces" / "minimal-session.json", 1),
        ("cui-detector-dataset-v2", EXAMPLES / "detector" / "synthetic-dataset.json", 18),
    ],
)
def test_validate_reports_object_and_record_counts(capsys, schema, path, records):
    code, out, _ = run(capsys, "validate", "--schema", schema, "--input", str(path))
    assert code == 0
    summary = json.loads(out)
    assert summary == {
        "schema_version": schema,
        "object_count": 1,
        "record_count": records,
        "validation_status": "pass",
        "writes_performed": False,
    }


def test_validate_x11_trace_and_failures(capsys, tmp_path):
    trace = write_synthetic_x11_trace(tmp_path / "x11.jsonl", events=4)
    code, out, _ = run(capsys, "validate", "--schema", "x11-trace-v2", "--input", str(trace))
    assert code == 0 and json.loads(out)["record_count"] == 4
    example = EXAMPLES / "traces" / "minimal-session.json"
    code, _, err = run(capsys, "validate", "--schema", "cui-trace-v2", "--input", str(example))
    assert code == 1 and "validation failed" in err
    code, _, err = run(capsys, "validate", "--schema", "x11-trace-v2", "--input", str(example))
    assert code == 1 and ".jsonl" in err
    missing = tmp_path / "missing.json"
    code, _, err = run(capsys, "validate", "--schema", "cui-codex-case-v1", "--input", str(missing))
    assert code == 1 and "cannot read" in err


def test_legacy_v1_dataset_is_read_only(capsys, tmp_path):
    payload = json.loads((EXAMPLES / "detector" / "synthetic-dataset.json").read_text("utf-8"))
    for record in payload["records"]:
        record["schema_version"] = RECORD_SCHEMA_V1
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps({"schema_version": "cui-detector-dataset-v1", "records": payload["records"]}),
        encoding="utf-8",
    )
    arguments = ["validate", "--schema", "cui-detector-dataset-v1", "--input", str(legacy)]
    code, out, _ = run(capsys, *arguments)
    assert code == 0 and json.loads(out)["record_count"] == 18
    code, _, err = run(capsys, *arguments, "--for-write")
    assert code == 1 and "read-only" in err


def test_extract_analyze_and_verify_bundle(capsys, tmp_path):
    manifest, _ = write_trace_manifest(tmp_path / "private")
    dataset = tmp_path / "dataset.json"
    code, out, _ = run(capsys, "extract", "--manifest", str(manifest), "--output", str(dataset))
    assert code == 0 and json.loads(out)["record_count"] == 18
    assert json.loads(dataset.read_text("utf-8"))["audit"]["task_count"] == 3

    synthetic = write_synthetic_dataset(tmp_path / "synthetic.json")
    output = tmp_path / "run"
    code, out, err = run(
        capsys,
        "analyze",
        "--dataset",
        str(synthetic),
        "--output-dir",
        str(output),
        "--profile",
        "smoke",
        *FAST,
    )
    assert code == 0, err
    assert "profile smoke" in err
    summary = json.loads(out)
    assert summary["claim_status"].startswith("Synthetic pipeline check only")
    configuration = json.loads((output / "result.json").read_text("utf-8"))["configuration"]
    assert configuration["epochs"] == 2 and configuration["bootstrap_repetitions"] == 1

    code, out, _ = run(capsys, "verify-bundle", str(output))
    assert code == 0
    verified = json.loads(out)
    assert verified["verified"] is True
    assert verified["files"]["result.json"] == summary["hashes"]["result.json"]

    code, _, err = run(
        capsys, "analyze", "--dataset", str(synthetic), "--output-dir", str(output), *FAST
    )
    assert code == 1 and "Refusing to overwrite" in err

    broken = tmp_path / "broken"
    shutil.copytree(output, broken)
    (broken / "detector-result-bundle.zip").write_bytes(b"garbage")
    code, _, err = run(capsys, "verify-bundle", str(broken))
    assert code == 1 and "Bundle ZIP is invalid" in err and "Traceback" not in err

    (output / "model_card.md").write_text("tampered\n", encoding="utf-8")
    code, _, err = run(capsys, "verify-bundle", str(output))
    assert code == 1 and "digest mismatch" in err


def test_analyze_rejects_non_positive_overrides(capsys, tmp_path):
    synthetic = write_synthetic_dataset(tmp_path / "synthetic.json")
    code, _, err = run(
        capsys,
        "analyze",
        "--dataset",
        str(synthetic),
        "--output-dir",
        str(tmp_path / "run"),
        "--epochs",
        "0",
    )
    assert code == 1 and "must be positive" in err


def test_schema_list_and_show(capsys):
    code, out, _ = run(capsys, "schema", "list")
    assert code == 0
    assert all(schema_id in out for schema_id in SCHEMA_FILES)
    code, out, _ = run(capsys, "schema", "list", "--json")
    rows = json.loads(out)
    assert [row["schema"] for row in rows] == sorted(SCHEMA_FILES)
    code, out, _ = run(capsys, "schema", "show", "cui-trace-v2")
    assert code == 0 and out == schema_text("cui-trace-v2")
    code, _, err = run(capsys, "schema", "show", "nope")
    assert code == 1 and "unknown schema" in err


def test_view_wires_the_viewer_parser(monkeypatch, capsys):
    calls: list[argparse.Namespace] = []

    def fake_serve(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr("computer_use_agent.viewer.serve.serve", fake_serve)
    trace = EXAMPLES / "traces" / "minimal-session-v2.json"
    code, _, _ = run(capsys, "view", "--trace", f"demo={trace}", "--port", "0")
    assert code == 0
    assert calls[0].trace == [("demo", trace)]
    assert calls[0].port == 0


# ------------------------------------------------------------------ doctor --


def test_doctor_passes_with_warnings_for_optional_tools(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cli, "_chrome_candidates", list)
    code, out, _ = run(capsys, "doctor")
    assert code == 0
    assert "[warn] node" in out and "[warn] chrome" in out
    assert "ok with 2 warning(s)" in out
    code, out, _ = run(capsys, "doctor", "--json")
    report = json.loads(out)
    assert report["ok"] is True
    statuses = {check["name"]: check["status"] for check in report["checks"]}
    assert statuses == {
        "python": "ok",
        "jsonschema": "ok",
        "package-data": "ok",
        "node": "warn",
        "chrome": "warn",
    }


def test_doctor_fails_only_for_required_checks(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "DOCTOR_CHECKS",
        (lambda: cli.Check("python", "fail", "too old", True),),
    )
    code, out, _ = run(capsys, "doctor")
    assert code == 1 and "FAILED" in out


@pytest.mark.parametrize(
    ("output", "status"),
    [
        ("v22.12.0", "ok"),
        ("v24.1.0", "ok"),
        ("v22.11.9", "warn"),
        ("v20.0.0", "warn"),
        ("", "warn"),
    ],
)
def test_node_version_gate(monkeypatch, output, status):
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(cli, "_run_version", lambda _executable: output)
    check = cli._check_node()
    assert check.status == status and check.required is False


@pytest.mark.parametrize(
    ("output", "needs_newer_for_lint"),
    [("v22.12.3", True), ("v22.13.0", False), ("v24.1.0", False)],
)
def test_node_below_the_eslint_floor_is_ok_with_a_note(monkeypatch, output, needs_newer_for_lint):
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(cli, "_run_version", lambda _executable: output)
    check = cli._check_node()
    assert check.status == "ok"
    assert ("ESLint 10 (npm run lint) needs Node >= 22.13" in check.detail) is needs_newer_for_lint


def test_node_that_cannot_run_is_a_warning(monkeypatch):
    def broken(_executable: str) -> str:
        raise OSError("cannot execute")

    monkeypatch.setattr(cli.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(cli, "_run_version", broken)
    assert cli._check_node().status == "warn"


def test_chrome_is_found_from_the_e2e_variable(monkeypatch, tmp_path):
    chrome = tmp_path / "chrome-for-testing"
    chrome.write_bytes(b"")
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    monkeypatch.setenv("CUA_E2E_CHROME", str(chrome))
    check = cli._check_chrome()
    assert check.status == "ok" and str(chrome) in check.detail
    assert chrome in cli._chrome_candidates()


def test_python_and_jsonschema_checks(monkeypatch):
    assert cli._check_python().status == "ok"
    monkeypatch.setattr(cli.sys, "version_info", (3, 11, 9, "final", 0))
    assert cli._check_python().status == "fail"
    assert cli._check_jsonschema().status == "ok"

    def missing(_name: str) -> str:
        raise cli.metadata.PackageNotFoundError("jsonschema")

    monkeypatch.setattr(cli.metadata, "version", missing)
    assert cli._check_jsonschema().status == "fail"
    assert cli._parse_version("node v22.21.1") == (22, 21, 1)
    assert cli._parse_version("no digits") is None
