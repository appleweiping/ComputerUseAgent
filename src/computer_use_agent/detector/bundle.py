"""Deterministic, self-checking detector result bundle writer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
import zlib
from pathlib import Path
from typing import Any

from .contract import canonical_json
from .reporting import (
    TRAJECTORY_ARTIFACT,
    build_trajectory_projection,
    trajectory_artifact_binding,
    validate_reporting_contract,
    validate_trajectory_projection,
)

BUNDLE_FILES = (
    "data_card.md",
    "model_card.md",
    "result.json",
    TRAJECTORY_ARTIFACT,
)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _provenance_lines(provenance: object) -> list[str]:
    if not isinstance(provenance, dict):
        return []
    lines = [f"- Source binding: `{provenance.get('schema_version')}`"]
    if "source_stage" in provenance:
        lines.append(f"- Source stage: `{provenance['source_stage']}`")
    if "study_ledger_integrity_sha256" in provenance:
        lines.append(
            f"- Source ledger integrity SHA-256: `{provenance['study_ledger_integrity_sha256']}`"
        )
    if "provenance_sha256" in provenance:
        lines.append(f"- Source provenance SHA-256: `{provenance['provenance_sha256']}`")
    lines.append(f"- Raw sequences embedded: `{provenance.get('raw_sequences_embedded', False)}`")
    return lines


def _data_card(result: dict[str, Any]) -> str:
    audit = result["dataset"]
    counts = result["class_and_task_counts"]
    evidence = ", ".join(audit["evidence_classes"])
    scopes = ", ".join(audit["participant_scopes"])
    provenance = result.get("source_provenance")
    trajectory = result["trajectory_artifact"]
    lines = [
        "# Detector analysis data card",
        "",
        "## Intended use",
        "",
        "This bundle evaluates whether the reviewed behavioral feature set can separate "
        "human from reasoning-agent sessions under task-held-out evaluation. It is not "
        "a population sample or a deployment dataset.",
        "",
        "## Composition",
        "",
        f"- Records: {audit['record_count']}",
        f"- Tasks: {audit['task_count']}",
        f"- Evidence class: {evidence}",
        f"- Participant scope: {scopes}",
        f"- Dataset SHA-256: `{audit['dataset_sha256']}`",
        f"- Class counts: `{counts['by_class']}`",
        f"- Task/class counts: `{counts['by_task_and_class']}`",
        *_provenance_lines(provenance),
        "",
        "Only de-identified, deterministic feature vectors enter the analysis. Raw "
        "keystroke text, action text, selectors, URLs, session UUIDs, absolute time, "
        "software versions, validator outcomes, actor/framework identifiers, and trust "
        "flags are excluded from the model matrix.",
        "",
        "## Splitting and leakage controls",
        "",
        "The primary split leaves one complete task out. Standardization is fitted on "
        "the outer training fold only. Probability calibration uses nested task-held-out "
        "training scores. The bundle also contains a task-identification negative "
        "control and within-task label permutation test.",
        "",
        "## Task-internal descriptive evidence",
        "",
        "`result.json` reports n, mean, median, and human-minus-agent Hedges-g "
        "for every allowed feature within each task alias. These are run-level "
        "descriptions, not population estimates.",
        "",
        f"`{trajectory['artifact_name']}` is bound by SHA-256 "
        f"`{trajectory['sha256']}`. It contains only task-by-class aggregate "
        "feature profiles; source task names, record identifiers, paths, raw "
        "feature rows, and event sequences are absent.",
        "",
        "## Known limitations",
        "",
        *[f"- {item}" for item in result["limitations"]],
        "",
    ]
    return "\n".join(lines)


def _model_card(result: dict[str, Any]) -> str:
    metrics = result["evaluation"]["primary_loto"]["aggregate"]
    config = result["configuration"]
    top = result["explanation"]["coefficient_summary"][:8]
    lines = [
        "# Exploratory detector model card",
        "",
        "## Status",
        "",
        f"**{result['claim_status']}**",
        "",
        "No deployable model is released by this bundle. Every reported probability is "
        "an out-of-task-fold prediction used to probe the analysis pipeline.",
        "",
        "## Model and evaluation",
        "",
        f"- Algorithm: {config['algorithm']}",
        f"- Calibration: {config['calibration']}",
        f"- Threshold: {metrics['threshold']}",
        f"- Balanced accuracy: {metrics['balanced_accuracy']}",
        f"- ROC AUC: {metrics['roc_auc']}",
        f"- Brier score: {metrics['brier_score']}",
        f"- Expected calibration error: {metrics['expected_calibration_error']}",
        "",
        "The task-cluster bootstrap intervals, calibration curve, per-task folds, label "
        "permutation test, task-identification control, and modality/feature ablations "
        "are in `result.json`.",
        "Task-internal n/mean/median and Hedges-g values are descriptive effect "
        "evidence and are not model coefficients. The deterministic trajectory "
        "artifact is an aggregate feature-profile projection, not a pointer or "
        "event timeline.",
        "",
        "## Feature explanation",
        "",
        "The values below are mean standardized coefficients across outer folds after "
        "calibration. They are stability diagnostics, not causal explanations.",
        "",
        "| Feature | Mean effect | Mean absolute effect |",
        "|---|---:|---:|",
        *[
            f"| {row['feature']} | {row['mean_standardized_effect']} | "
            f"{row['mean_absolute_standardized_effect']} |"
            for row in top
        ],
        "",
        "## Prohibited uses",
        "",
        "Do not use this exploratory output for identity claims, monitoring people, "
        "disciplinary decisions, undisclosed recording, or production blocking. Do not "
        "interpret a single-participant or synthetic result as population validity.",
        "",
    ]
    return "\n".join(lines)


def _write_deterministic_zip(directory: Path, output: Path) -> None:
    members = (*BUNDLE_FILES, "MANIFEST.sha256")
    # Stored members avoid zlib-version-dependent bytes. The bundle is small,
    # and cross-platform reproducibility is more valuable than compression.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in members:
            info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, (directory / name).read_bytes())


def write_bundle(result: dict[str, Any], output_dir: Path) -> dict[str, str]:
    """Atomically publish a deterministic result directory and ZIP.

    The destination must not already exist. This prevents a partial rerun from
    silently mixing artifacts from different datasets or configurations.
    """

    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite result bundle: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        validate_reporting_contract(result)
        projection = build_trajectory_projection(result["descriptive"])
        expected_binding = trajectory_artifact_binding(projection)
        if result["trajectory_artifact"] != expected_binding:
            raise ValueError("Detector result does not bind its trajectory projection")
        (temporary / "result.json").write_bytes(canonical_json(result))
        (temporary / TRAJECTORY_ARTIFACT).write_bytes(canonical_json(projection))
        (temporary / "data_card.md").write_text(_data_card(result), encoding="utf-8", newline="\n")
        (temporary / "model_card.md").write_text(
            _model_card(result), encoding="utf-8", newline="\n"
        )
        hashes = {name: _sha256(temporary / name) for name in BUNDLE_FILES}
        manifest = "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
        (temporary / "MANIFEST.sha256").write_text(manifest, encoding="ascii", newline="\n")
        _write_deterministic_zip(temporary, temporary / "detector-result-bundle.zip")
        os.replace(temporary, output_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        **hashes,
        "MANIFEST.sha256": _sha256(output_dir / "MANIFEST.sha256"),
        "detector-result-bundle.zip": _sha256(output_dir / "detector-result-bundle.zip"),
    }


def verify_bundle(output_dir: Path) -> dict[str, str]:
    output_dir = Path(output_dir)
    expected_lines = (output_dir / "MANIFEST.sha256").read_text(encoding="ascii").splitlines()
    expected: dict[str, str] = {}
    for line in expected_lines:
        digest, name = line.split("  ", 1)
        if name not in BUNDLE_FILES or name in expected:
            raise ValueError("Bundle manifest contains an unexpected/duplicate member")
        expected[name] = digest
    if set(expected) != set(BUNDLE_FILES):
        raise ValueError("Bundle manifest is incomplete")
    for name, digest in expected.items():
        if _sha256(output_dir / name) != digest:
            raise ValueError(f"Bundle digest mismatch: {name}")
    try:
        result = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
        projection = json.loads((output_dir / TRAJECTORY_ARTIFACT).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise ValueError(f"Bundle reporting artifact is invalid JSON: {error}") from error
    if not isinstance(result, dict) or not isinstance(projection, dict):
        raise ValueError("Bundle reporting artifact root is not an object")
    if (output_dir / "result.json").read_bytes() != canonical_json(result) or (
        output_dir / TRAJECTORY_ARTIFACT
    ).read_bytes() != canonical_json(projection):
        raise ValueError("Bundle JSON artifacts are not canonical")
    validate_reporting_contract(result)
    validate_trajectory_projection(projection, descriptive=result["descriptive"])
    if result["trajectory_artifact"] != trajectory_artifact_binding(projection):
        raise ValueError("Bundle trajectory projection differs from its result binding")
    try:
        with zipfile.ZipFile(output_dir / "detector-result-bundle.zip") as archive:
            names = archive.namelist()
            if names != [*BUNDLE_FILES, "MANIFEST.sha256"]:
                raise ValueError("Bundle ZIP names/order are not canonical")
            if len(names) != len(set(names)) or any(".." in Path(name).parts for name in names):
                raise ValueError("Bundle ZIP contains duplicate or unsafe paths")
            for name in names:
                if archive.read(name) != (output_dir / name).read_bytes():
                    raise ValueError(f"Bundle ZIP readback mismatch: {name}")
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
        EOFError,
        NotImplementedError,
        RuntimeError,
    ) as error:
        # A corrupt archive or member stream, an unsupported compression method
        # or an encrypted member is a verification failure, not a crash (none of
        # these is a ValueError). The ValueErrors raised above pass through.
        raise ValueError(f"Bundle ZIP is invalid: {error}") from error
    return expected
