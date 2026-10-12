"""Build, execute, and validate the FP-Agent retrospective notebook.

The executed ``.ipynb`` and the regenerated figures are untracked build outputs
(written to ``--output-dir``, a temporary directory by default). The tracked
execution manifest ``notebooks/retrospective.execution.json`` binds the notebook
source, the archived result and its status, the archived figure manifest and
every archived figure by SHA-256. ``--check`` re-verifies those bindings and
needs no executed notebook.

Executing against the archived result (the default ``--result-dir``) is a
reproduction check: it fails unless every regenerated figure is byte-identical
to the archived one, and only then rewrites the tracked manifest. Executing
against any other result directory writes the manifest next to the outputs.
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

from research.fp_agent.paths import (
    CHECKOUT_ROOT,
    EVIDENCE_DIR,
    PACKAGE_DIR,
    display_path,
    is_evidence_path,
    refuse_evidence_output,
    resolve_recorded_path,
)

SCHEMA_VERSION = "cui-notebook-execution-v2"
SOURCE = PACKAGE_DIR / "notebooks" / "retrospective.py"
MANIFEST = PACKAGE_DIR / "notebooks" / "retrospective.execution.json"
LOCK = PACKAGE_DIR / "notebook-requirements.lock"
RESULT_FILENAME = "fp_agent_retrospective.json"
STATUS_FILENAME = "fp_agent_retrospective_status.json"
RESULT = EVIDENCE_DIR / RESULT_FILENAME
RESULT_STATUS = EVIDENCE_DIR / STATUS_FILENAME
FIGURES = EVIDENCE_DIR / "figures" / "figure_manifest.json"
EXECUTED_NOTEBOOK = "retrospective.executed.ipynb"
GENERATED_FIGURE_MANIFEST = "figure_manifest.json"
MINIMUM_FIGURES = 6
PACKAGES = (
    "ipykernel",
    "jupytext",
    "matplotlib",
    "nbclient",
    "nbformat",
    "numpy",
    "pandas",
    "scikit-learn",
    "seaborn",
    "xgboost",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding(path: Path) -> dict[str, Any]:
    return {"path": display_path(path), "bytes": path.stat().st_size, "sha256": sha256(path)}


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


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{display_path(path)} is not a JSON object")
    return payload


def archived_figures() -> list[dict[str, Any]]:
    """Expected bindings of the archived figures, translated to the current tree."""

    manifest = _read_json(FIGURES)
    rows = []
    for row in manifest.get("figures", []):
        rows.append(
            {
                "path": display_path(resolve_recorded_path(str(row["path"]))),
                "bytes": row["bytes"],
                "sha256": row["sha256"],
            }
        )
    return rows


def _verify_result_binding(result: Path, status_path: Path) -> None:
    status = _read_json(status_path)
    if status.get("status") != "complete" or status.get("output_sha256") != sha256(result):
        raise RuntimeError("Retrospective result/status binding is incomplete")


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


def _original_execution(path: Path) -> dict[str, Any] | None:
    """Carry forward the first recorded execution (the run that produced the evidence).

    A v2 manifest already holds it under ``original_execution``; an older (v1)
    manifest is itself that first execution and is summarized.
    """

    if not path.is_file():
        return None
    old = _read_json(path)
    if "original_execution" in old:
        carried = old["original_execution"]
        return carried if isinstance(carried, dict) else None
    source = old.get("source")
    if isinstance(source, dict):  # v2 manifest
        source_path, source_sha = source.get("path"), source.get("sha256")
    else:  # v1 manifest
        source_path, source_sha = source, old.get("source_sha256")
    environment = old.get("environment", {})
    return {
        "schema_version": old.get("schema_version"),
        "executed_at_utc": old.get("executed_at_utc"),
        "source": source_path,
        "source_sha256": source_sha,
        "executed_notebook_sha256": old.get("notebook_sha256"),
        "python": environment.get("python"),
        "packages": environment.get("packages"),
    }


def build_and_execute(
    *,
    result_dir: Path = EVIDENCE_DIR,
    output_dir: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    result_dir = result_dir.resolve()
    archival = result_dir == EVIDENCE_DIR.resolve()
    result = result_dir / RESULT_FILENAME
    status_path = result_dir / STATUS_FILENAME
    _verify_result_binding(result, status_path)
    output_dir = refuse_evidence_output(
        output_dir or Path(tempfile.mkdtemp(prefix="cua-fp-notebook-"))
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
        "display_name": "Locked FP retrospective Python",
        "language": "python",
        "name": "python3",
    }
    notebook.metadata.language_info = {"name": "python", "version": platform.python_version()}
    root = str(CHECKOUT_ROOT)
    client = NotebookClient(
        notebook,
        timeout=900,
        kernel_name="python3",
        allow_errors=False,
        resources={"metadata": {"path": root}},
    )
    locations = {"CUA_FP_RESULT_DIR": str(result_dir), "CUA_FP_FIGURE_DIR": str(figure_dir)}
    with _kernel_environment(locations):
        executed = client.execute(cwd=root)
    notebook_path = output_dir / EXECUTED_NOTEBOOK
    nbformat.write(executed, notebook_path)

    code_cells = [cell for cell in executed.cells if cell.cell_type == "code"]
    if not code_cells or any(cell.execution_count is None for cell in code_cells):
        raise RuntimeError("Executed notebook has an unexecuted code cell")
    error_count = sum(
        output.get("output_type") == "error"
        for cell in code_cells
        for output in cell.get("outputs", [])
    )
    if error_count:
        raise RuntimeError("Executed notebook retained an error output")

    generated_manifest = figure_dir / GENERATED_FIGURE_MANIFEST
    generated = _read_json(generated_manifest).get("figures", [])
    for row in generated:
        path = figure_dir / str(row["path"])
        if row.get("sha256") != sha256(path) or row.get("bytes") != path.stat().st_size:
            raise RuntimeError(f"Generated figure binding failed for {row.get('path')}")
    if len(generated) < MINIMUM_FIGURES:
        raise RuntimeError("fewer than six research figures were generated")

    if archival:
        expected = {Path(row["path"]).name: row["sha256"] for row in archived_figures()}
        observed = {str(row["path"]): row["sha256"] for row in generated}
        differing = sorted(
            name for name in expected.keys() | observed.keys() if expected.get(name) != observed.get(name)
        )
        if differing:
            raise RuntimeError(
                "regenerated figures differ from the archived figures "
                f"(archived evidence left untouched): {differing}"
            )
        figure_manifest = _binding(FIGURES)
        figures = archived_figures()
    else:
        figure_manifest = _binding(generated_manifest)
        figures = [_binding(figure_dir / str(row["path"])) for row in generated]

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "clean_kernel": True,
        "allow_errors": False,
        "executed_notebook_tracked": False,
        "executed_notebook_sha256": sha256(notebook_path),
        "source": _binding(SOURCE),
        "result": _binding(result),
        "result_status": _binding(status_path),
        "figure_manifest": figure_manifest,
        "figures": figures,
        "figures_reproduced_byte_identical": True if archival else None,
        "code_cell_count": len(code_cells),
        "executed_code_cell_count": len(code_cells),
        "error_output_count": error_count,
        "environment": _environment(),
        "original_execution": _original_execution(manifest_path),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def check(manifest_path: Path = MANIFEST) -> dict[str, Any]:
    """Verify the tracked bindings; the executed notebook is not required."""

    manifest = _read_json(manifest_path)
    errors: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("execution manifest schema changed")
    if manifest.get("status") != "complete" or manifest.get("clean_kernel") is not True:
        errors.append("notebook execution is not complete/clean-kernel")
    if manifest.get("allow_errors") is not False or manifest.get("error_output_count") != 0:
        errors.append("notebook execution retained an error")
    cells = manifest.get("code_cell_count")
    if not isinstance(cells, int) or cells < 1 or cells != manifest.get("executed_code_cell_count"):
        errors.append("notebook contains an unexecuted code cell")
    if manifest.get("executed_notebook_tracked") is not False:
        errors.append("the executed notebook must stay an untracked build output")
    for key, path in (
        ("source", SOURCE),
        ("result", RESULT),
        ("result_status", RESULT_STATUS),
        ("figure_manifest", FIGURES),
    ):
        if manifest.get(key) != _binding(path):
            errors.append(f"{key} binding changed")
    if manifest.get("environment", {}).get("lock") != _binding(LOCK):
        errors.append("environment lock binding changed")
    try:
        _verify_result_binding(RESULT, RESULT_STATUS)
    except RuntimeError as error:
        errors.append(str(error))
    if _read_json(FIGURES).get("source_result_sha256") != sha256(RESULT):
        errors.append("archived figure manifest is bound to a different result")
    expected_figures = archived_figures()
    if manifest.get("figures") != expected_figures:
        errors.append("figure inventory changed")
    if len(expected_figures) < MINIMUM_FIGURES:
        errors.append("fewer than six research figures are bound")
    for row in expected_figures:
        path = resolve_recorded_path(row["path"])
        if not path.is_file():
            errors.append(f"figure is absent: {row['path']}")
        elif sha256(path) != row["sha256"] or path.stat().st_size != row["bytes"]:
            errors.append(f"figure binding failed: {row['path']}")
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
        help="directory holding the result and status JSON (default: the archived evidence)",
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
        "executing against the archived result, else <output-dir>/" + MANIFEST.name + ")",
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
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
