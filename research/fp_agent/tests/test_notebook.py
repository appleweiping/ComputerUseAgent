"""The tracked notebook execution manifest is verified without an executed notebook."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from research.fp_agent import execute_notebook
from research.fp_agent.paths import PACKAGE_DIR, resolve_recorded_path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_notebook_check_validates_bindings_without_an_executed_notebook() -> None:
    manifest = execute_notebook.check()
    assert manifest["schema_version"] == execute_notebook.SCHEMA_VERSION
    assert manifest["status"] == "complete"
    assert manifest["clean_kernel"] is True
    assert manifest["allow_errors"] is False
    assert manifest["error_output_count"] == 0
    assert manifest["code_cell_count"] == manifest["executed_code_cell_count"] > 0
    assert manifest["executed_notebook_tracked"] is False
    assert manifest["figures_reproduced_byte_identical"] is True
    assert manifest["source"]["path"] == "research/fp_agent/notebooks/retrospective.py"
    assert manifest["result"]["path"] == "research/evidence/fp_agent/fp_agent_retrospective.json"
    assert len(manifest["figures"]) >= execute_notebook.MINIMUM_FIGURES
    for row in manifest["figures"]:
        path = resolve_recorded_path(row["path"])
        assert path.is_file()
        assert row["sha256"] == file_sha256(path)
        assert row["bytes"] == path.stat().st_size
    assert not list((PACKAGE_DIR / "notebooks").glob("*.executed.ipynb"))


@pytest.mark.parametrize(
    ("key", "field"),
    [("source", "sha256"), ("result", "sha256"), ("figure_manifest", "bytes")],
)
def test_notebook_check_rejects_a_changed_binding(tmp_path, key: str, field: str) -> None:
    manifest = json.loads(execute_notebook.MANIFEST.read_text(encoding="utf-8"))
    manifest[key][field] = "0" * 64 if field == "sha256" else 1
    candidate = tmp_path / "retrospective.execution.json"
    candidate.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match=f"{key} binding changed"):
        execute_notebook.check(candidate)


def test_notebook_check_rejects_a_missing_figure(tmp_path) -> None:
    manifest = json.loads(execute_notebook.MANIFEST.read_text(encoding="utf-8"))
    manifest["figures"] = manifest["figures"][:-1]
    candidate = tmp_path / "retrospective.execution.json"
    candidate.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="figure inventory changed"):
        execute_notebook.check(candidate)


def test_original_execution_is_recorded_and_carried_forward(tmp_path) -> None:
    manifest = json.loads(execute_notebook.MANIFEST.read_text(encoding="utf-8"))
    original = manifest["original_execution"]
    assert original["schema_version"] == "cui-notebook-execution-v1"
    assert original["executed_at_utc"].startswith("2026-08-11")
    assert original["source"] == "experiments/fp_agent/notebooks/retrospective.py"
    candidate = tmp_path / "manifest.json"
    candidate.write_text(json.dumps(manifest), encoding="utf-8")
    assert execute_notebook._original_execution(candidate) == original
    assert execute_notebook._original_execution(tmp_path / "absent.json") is None
