"""Build, execute, and validate the aggregate-only auxiliary notebook.

The executed ``.ipynb`` and the regenerated figures are untracked build outputs
(written to ``--output-dir``, a temporary directory by default). The tracked
execution manifest ``notebooks/auxiliary_calibration.execution.json`` binds the
notebook source, the six archived aggregate inputs, the two archived figures,
the notebook lock and the archived 2026-08-11 execution record by SHA-256.
``--check`` re-verifies those bindings and needs no executed notebook.

Executing against the archived inputs (the default ``--result-dir``) is a
reproduction check: it fails unless both regenerated figures are byte-identical
to the archived ones, and only then rewrites the tracked manifest. Executing
against any other directory writes the manifest next to the outputs.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import tempfile
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import (
    CHECKOUT_ROOT,
    EVIDENCE_DIR,
    PACKAGE_DIR,
    display_path,
    is_evidence_path,
    refuse_evidence_output,
    resolve_recorded_path,
)

SCHEMA_VERSION = "cui-notebook-execution-v2"
SOURCE = PACKAGE_DIR / "notebooks" / "auxiliary_calibration.py"
MANIFEST = PACKAGE_DIR / "notebooks" / "auxiliary_calibration.execution.json"
ARCHIVED_MANIFEST = EVIDENCE_DIR / "notebook_execution.json"
LOCK = PACKAGE_DIR / "notebook-requirements.lock"
EXECUTED_NOTEBOOK = "auxiliary_calibration.executed.ipynb"
INPUT_NAMES = (
    "captchasolve30k_metrics.json",
    "aalto_136m_metrics.json",
    "molmoweb_metrics.json",
    "summary.json",
    "source_access_manifest.json",
    "sampling_ledgers.json",
)
FIGURE_NAMES = ("auxiliary_calibration.svg", "auxiliary_calibration.png")
INPUTS = tuple(EVIDENCE_DIR / name for name in INPUT_NAMES)
FIGURES = tuple(EVIDENCE_DIR / name for name in FIGURE_NAMES)
PACKAGES = ("ipykernel", "jupytext", "matplotlib", "nbclient", "nbformat")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding(path: Path) -> dict[str, Any]:
    return {"path": display_path(path), "bytes": path.stat().st_size, "sha256": sha256(path)}


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{display_path(path)} is not a JSON object")
    return payload


def _environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "operating_system": platform.system(),
        "operating_system_release": platform.release(),
        "machine": platform.machine(),
        "isolated_python": sys.prefix != sys.base_prefix,
        "packages": {name: importlib.metadata.version(name) for name in PACKAGES},
        "lock": _binding(LOCK),
    }


def _assert_source_boundaries(result_dir: Path) -> dict[str, Any]:
    summary = _read_json(result_dir / "summary.json")
    molmo = _read_json(result_dir / "molmoweb_metrics.json")
    checks = {
        "row_level_data_loaded": False,
        "auxiliary_rows_concatenated_with_fp_agent": summary["hard_boundaries"][
            "auxiliary_rows_concatenated_with_fp_agent"
        ],
        "headline_cross_dataset_classifier": summary["hard_boundaries"][
            "headline_cross_dataset_classifier"
        ],
        "molmoweb_fed_to_23_feature_timing_model": summary["hard_boundaries"][
            "molmoweb_fed_to_23_feature_timing_model"
        ],
        "molmoweb_timestamp_values_retained": bool(
            molmo["timestamp_audit"]["human"]["timestamp_values_retained"]
            or molmo["timestamp_audit"]["synthetic"]["timestamp_values_retained"]
        ),
    }
    if any(checks.values()):
        raise RuntimeError(f"auxiliary notebook boundary failed: {checks}")
    return checks


def _seeds(result_dir: Path) -> dict[str, int]:
    captcha = _read_json(result_dir / "captchasolve30k_metrics.json")
    aalto = _read_json(result_dir / "aalto_136m_metrics.json")
    molmo = _read_json(result_dir / "molmoweb_metrics.json")
    return {
        "captcha_seed": captcha["seed"],
        "aalto_seed": aalto["seed"],
        "molmoweb_human_seed": molmo["human_seed"],
        "molmoweb_synthetic_seed": molmo["synthetic_seed"],
        "molmoweb_fold_seed": molmo["domain_held_out_analysis"]["fold_seed"],
    }


@contextlib.contextmanager
def _kernel_environment(values: Mapping[str, str]) -> Iterator[None]:
    """Expose locations to the kernel, which inherits this process's environment."""

    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def archived_execution_errors() -> list[str]:
    """Cross-check the archived execution record against the current evidence bytes.

    The 2026-08-11 record binds the inputs and figures under their original
    ``results/`` paths; they are translated, never rewritten.
    """

    errors: list[str] = []
    archived = _read_json(ARCHIVED_MANIFEST)
    if archived.get("status") != "complete" or archived.get("error_output_count") != 0:
        errors.append("archived execution record is not complete and error-free")
    for section in ("inputs", "outputs"):
        for row in archived.get(section, []):
            path = resolve_recorded_path(str(row.get("path")))
            if not path.is_file():
                errors.append(f"archived {section} file is absent: {row.get('path')}")
            elif sha256(path) != row.get("sha256") or path.stat().st_size != row.get("bytes"):
                errors.append(f"archived {section} binding no longer matches: {row.get('path')}")
    lock = archived.get("environment", {}).get("lock", {})
    if lock.get("sha256") != sha256(LOCK) or lock.get("bytes") != LOCK.stat().st_size:
        errors.append("archived execution record binds a different notebook lock")
    recorded = {Path(str(row.get("path"))).name for row in archived.get("inputs", [])}
    if recorded != set(INPUT_NAMES):
        errors.append("archived execution record binds a different input set")
    recorded = {Path(str(row.get("path"))).name for row in archived.get("outputs", [])}
    if recorded != set(FIGURE_NAMES):
        errors.append("archived execution record binds a different figure set")
    return errors


def build_and_execute(
    *,
    result_dir: Path = EVIDENCE_DIR,
    output_dir: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    result_dir = result_dir.resolve()
    archival = result_dir == EVIDENCE_DIR.resolve()
    boundary_checks = _assert_source_boundaries(result_dir)
    output_dir = refuse_evidence_output(
        output_dir or Path(tempfile.mkdtemp(prefix="cua-auxiliary-notebook-"))
    )
    figure_dir = output_dir / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    if manifest_path is None:
        manifest_path = MANIFEST if archival else output_dir / MANIFEST.name
    elif is_evidence_path(manifest_path):
        raise RuntimeError("the execution manifest must not be written into research/evidence")

    # Execution-only dependencies are imported lazily, after every path guard,
    # so that --check and the tests run without the notebook stack.
    import jupytext
    import nbformat
    from nbclient import NotebookClient

    notebook = jupytext.read(SOURCE)
    notebook.metadata.kernelspec = {
        "display_name": "Locked auxiliary calibration Python",
        "language": "python",
        "name": "python3",
    }
    notebook.metadata.language_info = {"name": "python", "version": platform.python_version()}
    client = NotebookClient(
        notebook,
        timeout=300,
        kernel_name="python3",
        allow_errors=False,
        resources={"metadata": {"path": str(CHECKOUT_ROOT)}},
    )
    locations = {"CUA_AUX_RESULT_DIR": str(result_dir), "CUA_AUX_FIGURE_DIR": str(figure_dir)}
    with _kernel_environment(locations):
        executed = client.execute(cwd=str(CHECKOUT_ROOT))
    notebook_path = output_dir / EXECUTED_NOTEBOOK
    nbformat.write(executed, notebook_path)
    code_cells = [cell for cell in executed.cells if cell.cell_type == "code"]
    error_count = sum(
        output.get("output_type") == "error"
        for cell in code_cells
        for output in cell.get("outputs", [])
    )
    if not code_cells or any(cell.execution_count is None for cell in code_cells) or error_count:
        raise RuntimeError("auxiliary notebook did not execute every code cell without errors")

    generated = tuple(figure_dir / name for name in FIGURE_NAMES)
    if archival:
        differing = [
            path.name
            for path, archived in zip(generated, FIGURES, strict=True)
            if not path.is_file() or sha256(path) != sha256(archived)
        ]
        if differing:
            raise RuntimeError(
                "regenerated figures differ from the archived figures "
                f"(archived evidence left untouched): {differing}"
            )
        outputs = [_binding(path) for path in FIGURES]
    else:
        outputs = [_binding(path) for path in generated]

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "clean_kernel": True,
        "allow_errors": False,
        "executed_notebook_tracked": False,
        "executed_notebook_sha256": sha256(notebook_path),
        "source": _binding(SOURCE),
        "inputs": [_binding(result_dir / name) for name in INPUT_NAMES],
        "outputs": outputs,
        "figures_reproduced_byte_identical": True if archival else None,
        "archived_execution": _binding(ARCHIVED_MANIFEST) if archival else None,
        "code_cell_count": len(code_cells),
        "executed_code_cell_count": len(code_cells),
        "error_output_count": error_count,
        "seeds": _seeds(result_dir),
        "boundary_checks": boundary_checks,
        "environment": _environment(),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def check(manifest_path: Path = MANIFEST) -> dict[str, Any]:
    """Verify the tracked bindings; the executed notebook is not required."""

    manifest = _read_json(manifest_path)
    errors: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("execution schema changed")
    if manifest.get("status") != "complete" or manifest.get("clean_kernel") is not True:
        errors.append("notebook execution is not complete/clean-kernel")
    if manifest.get("allow_errors") is not False or manifest.get("error_output_count") != 0:
        errors.append("notebook execution retained an error")
    cells = manifest.get("code_cell_count")
    if not isinstance(cells, int) or cells < 1 or cells != manifest.get("executed_code_cell_count"):
        errors.append("executed code-cell count differs from inventory")
    if manifest.get("executed_notebook_tracked") is not False:
        errors.append("the executed notebook must stay an untracked build output")
    if manifest.get("source") != _binding(SOURCE):
        errors.append("source binding changed")
    if manifest.get("inputs") != [_binding(path) for path in INPUTS]:
        errors.append("input artifact bindings changed")
    if manifest.get("outputs") != [_binding(path) for path in FIGURES]:
        errors.append("output artifact bindings changed")
    if manifest.get("archived_execution") != _binding(ARCHIVED_MANIFEST):
        errors.append("archived execution record binding changed")
    if manifest.get("environment", {}).get("lock") != _binding(LOCK):
        errors.append("environment lock binding changed")
    if any(manifest.get("boundary_checks", {}).values()):
        errors.append("auxiliary separation boundary changed")
    if manifest.get("seeds") != _seeds(EVIDENCE_DIR):
        errors.append("sampling seeds differ from the archived metrics")
    if manifest.get("seeds") != _read_json(ARCHIVED_MANIFEST).get("seeds"):
        errors.append("sampling seeds differ from the archived execution record")
    errors.extend(archived_execution_errors())
    if errors:
        raise RuntimeError("; ".join(errors))
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the tracked manifest only")
    parser.add_argument(
        "--result-dir",
        type=Path,
        default=EVIDENCE_DIR,
        help="directory holding the six aggregate inputs (default: the archived evidence)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="build directory for the executed notebook and figures (default: a new temp dir)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="execution manifest to write or check (default: the tracked manifest when "
        "executing against the archived inputs, else <output-dir>/" + MANIFEST.name + ")",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        manifest = check(args.manifest or MANIFEST)
    else:
        manifest = build_and_execute(
            result_dir=args.result_dir,
            output_dir=args.output_dir,
            manifest_path=args.manifest,
        )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
