from __future__ import annotations

import json

import pytest

from research.auxiliary_data import execute_notebook
from research.auxiliary_data.paths import EVIDENCE_DIR, PACKAGE_DIR, resolve_recorded_path


def test_notebook_check_validates_bindings_without_an_executed_notebook() -> None:
    manifest = execute_notebook.check()
    assert manifest["schema_version"] == execute_notebook.SCHEMA_VERSION
    assert manifest["clean_kernel"] is True
    assert manifest["code_cell_count"] == manifest["executed_code_cell_count"] > 0
    assert manifest["error_output_count"] == 0
    assert manifest["executed_notebook_tracked"] is False
    assert manifest["figures_reproduced_byte_identical"] is True
    assert manifest["boundary_checks"]["row_level_data_loaded"] is False
    assert len(manifest["inputs"]) == 6
    assert len(manifest["outputs"]) == 2
    assert manifest["source"]["path"] == "research/auxiliary_data/notebooks/auxiliary_calibration.py"
    assert not list((PACKAGE_DIR / "notebooks").glob("*.executed.ipynb"))


def test_notebook_manifest_contains_fixed_sampling_seeds() -> None:
    manifest = json.loads(execute_notebook.MANIFEST.read_text(encoding="utf-8"))
    assert manifest["seeds"] == {
        "aalto_seed": 2026081102,
        "captcha_seed": 2026081101,
        "molmoweb_fold_seed": 2026081105,
        "molmoweb_human_seed": 2026081103,
        "molmoweb_synthetic_seed": 2026081104,
    }


def test_archived_execution_record_still_matches_the_evidence() -> None:
    assert execute_notebook.archived_execution_errors() == []
    archived = json.loads(execute_notebook.ARCHIVED_MANIFEST.read_text(encoding="utf-8"))
    recorded = [row["path"] for row in archived["inputs"] + archived["outputs"]]
    assert all(path.startswith("results/auxiliary_data/") for path in recorded)
    assert all(resolve_recorded_path(path).is_file() for path in recorded)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m["source"].update(sha256="0" * 64), "source binding changed"),
        (lambda m: m["inputs"].pop(), "input artifact bindings changed"),
        (lambda m: m["outputs"][0].update(bytes=1), "output artifact bindings changed"),
        (lambda m: m.update(executed_notebook_tracked=True), "untracked build output"),
        (lambda m: m["boundary_checks"].update(row_level_data_loaded=True), "boundary changed"),
        (lambda m: m["seeds"].update(captcha_seed=1), "sampling seeds differ"),
    ],
)
def test_notebook_check_rejects_a_changed_binding(tmp_path, mutate, message) -> None:
    manifest = json.loads(execute_notebook.MANIFEST.read_text(encoding="utf-8"))
    mutate(manifest)
    candidate = tmp_path / "auxiliary_calibration.execution.json"
    candidate.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match=message):
        execute_notebook.check(candidate)


def test_execution_refuses_to_write_into_evidence() -> None:
    with pytest.raises(ValueError, match="archival"):
        execute_notebook.build_and_execute(output_dir=EVIDENCE_DIR / "build")
