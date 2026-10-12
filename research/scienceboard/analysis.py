"""Task-controlled exploratory analysis for matched ScienceBoard traces.

The implementation uses a small deterministic softmax regression written with
the Python standard library. It keeps labels/provenance outside the feature
matrix and evaluates actor probes strictly by leaving one task out. With the
planned 27 sessions, outputs are explicitly a single-participant pipeline
probe rather than a deployable detector estimate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from computer_use_agent.features import FEATURE_NAMES, extract_features, validate_feature_vector
from computer_use_agent.features.statistics import task_descriptive_summary
from computer_use_agent.trace.io import load_trace
from research.scienceboard.artifacts import (
    validate_session_evidence,
    verify_stored_artifact_hashes,
)
from research.scienceboard.preflight import DEFAULT_MANIFEST
from research.scienceboard.private_storage import PrivateStorageError, require_private_path


MODEL_CONFIG: dict[str, Any] = {
    "algorithm": "deterministic full-batch softmax regression",
    "epochs": 1200,
    "learning_rate": 0.08,
    "l2": 0.02,
    "standardization": "training-fold mean and population standard deviation",
}


def _matrix(records: Sequence[Mapping[str, Any]]) -> list[list[float]]:
    matrix: list[list[float]] = []
    for record in records:
        features = record["features"]
        validate_feature_vector(features)
        matrix.append([float(features[name]) for name in FEATURE_NAMES])
    return matrix


def standardize(
    train: Sequence[Sequence[float]],
    test: Sequence[Sequence[float]],
) -> tuple[list[list[float]], list[list[float]]]:
    if not train:
        raise ValueError("Training matrix is empty")
    width = len(train[0])
    if any(len(row) != width for row in [*train, *test]):
        raise ValueError("Inconsistent feature width")
    means = [statistics.fmean(row[index] for row in train) for index in range(width)]
    scales = []
    for index, mean in enumerate(means):
        variance = statistics.fmean((row[index] - mean) ** 2 for row in train)
        deviation = math.sqrt(variance)
        scales.append(deviation if deviation > 1e-12 else 1.0)

    def transform(rows: Sequence[Sequence[float]]) -> list[list[float]]:
        return [
            [(float(value) - means[index]) / scales[index] for index, value in enumerate(row)]
            for row in rows
        ]

    return transform(train), transform(test)


def _softmax(scores: Sequence[float]) -> list[float]:
    maximum = max(scores)
    exponentials = [math.exp(score - maximum) for score in scores]
    total = sum(exponentials)
    return [value / total for value in exponentials]


class SoftmaxClassifier:
    def __init__(self, *, epochs: int = 1200, learning_rate: float = 0.08, l2: float = 0.02) -> None:
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.l2 = l2
        self.classes: list[str] = []
        self.weights: list[list[float]] = []
        self.bias: list[float] = []

    def fit(self, matrix: Sequence[Sequence[float]], labels: Sequence[str]) -> "SoftmaxClassifier":
        if not matrix or len(matrix) != len(labels):
            raise ValueError("Training matrix/label length mismatch")
        self.classes = sorted(set(labels))
        if len(self.classes) < 2:
            raise ValueError("Classifier requires at least two classes")
        width = len(matrix[0])
        class_index = {label: index for index, label in enumerate(self.classes)}
        self.weights = [[0.0] * width for _ in self.classes]
        self.bias = [0.0] * len(self.classes)
        sample_count = len(matrix)

        for _ in range(self.epochs):
            weight_gradient = [[0.0] * width for _ in self.classes]
            bias_gradient = [0.0] * len(self.classes)
            for row, label in zip(matrix, labels, strict=True):
                probabilities = _softmax(
                    [
                        self.bias[class_id]
                        + sum(weight * value for weight, value in zip(self.weights[class_id], row, strict=True))
                        for class_id in range(len(self.classes))
                    ]
                )
                expected = class_index[label]
                for class_id, probability in enumerate(probabilities):
                    error = probability - float(class_id == expected)
                    bias_gradient[class_id] += error
                    for feature_id, value in enumerate(row):
                        weight_gradient[class_id][feature_id] += error * value
            for class_id in range(len(self.classes)):
                self.bias[class_id] -= self.learning_rate * bias_gradient[class_id] / sample_count
                for feature_id in range(width):
                    gradient = (
                        weight_gradient[class_id][feature_id] / sample_count
                        + self.l2 * self.weights[class_id][feature_id]
                    )
                    self.weights[class_id][feature_id] -= self.learning_rate * gradient
        return self

    def predict(self, matrix: Sequence[Sequence[float]]) -> list[str]:
        predictions = []
        for row in matrix:
            scores = [
                self.bias[class_id]
                + sum(weight * value for weight, value in zip(self.weights[class_id], row, strict=True))
                for class_id in range(len(self.classes))
            ]
            predictions.append(self.classes[max(range(len(scores)), key=scores.__getitem__)])
        return predictions


def classification_metrics(true: Sequence[str], predicted: Sequence[str]) -> dict[str, Any]:
    if not true or len(true) != len(predicted):
        raise ValueError("Prediction/label length mismatch")
    classes = sorted(set(true) | set(predicted))
    matrix = {actual: {guess: 0 for guess in classes} for actual in classes}
    for actual, guess in zip(true, predicted, strict=True):
        matrix[actual][guess] += 1
    recalls = []
    f1s = []
    per_class = {}
    for label in classes:
        tp = matrix[label][label]
        fn = sum(matrix[label].values()) - tp
        fp = sum(matrix[other][label] for other in classes if other != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        recalls.append(recall)
        f1s.append(f1)
        per_class[label] = {
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
            "support": sum(matrix[label].values()),
        }
    return {
        "accuracy": round(sum(actual == guess for actual, guess in zip(true, predicted, strict=True)) / len(true), 6),
        "balanced_accuracy": round(statistics.fmean(recalls), 6),
        "macro_f1": round(statistics.fmean(f1s), 6),
        "per_class": per_class,
        "confusion_matrix": {
            "labels": classes,
            "values": [[matrix[actual][guess] for guess in classes] for actual in classes],
        },
    }


def _fit_predict(
    train_records: Sequence[Mapping[str, Any]],
    test_records: Sequence[Mapping[str, Any]],
    label: Callable[[Mapping[str, Any]], str],
) -> list[str]:
    train_matrix, test_matrix = standardize(_matrix(train_records), _matrix(test_records))
    classifier = SoftmaxClassifier(
        epochs=int(MODEL_CONFIG["epochs"]),
        learning_rate=float(MODEL_CONFIG["learning_rate"]),
        l2=float(MODEL_CONFIG["l2"]),
    ).fit(train_matrix, [label(record) for record in train_records])
    return classifier.predict(test_matrix)


def grouped_cross_validation(
    records: Sequence[Mapping[str, Any]],
    *,
    group_field: str,
    label: Callable[[Mapping[str, Any]], str],
) -> dict[str, Any]:
    groups = sorted({str(record[group_field]) for record in records})
    if len(groups) < 2:
        raise ValueError(f"Need at least two {group_field} groups")
    folds = []
    all_true: list[str] = []
    all_predicted: list[str] = []
    expected_classes = sorted({label(record) for record in records})
    for group in groups:
        train = [record for record in records if str(record[group_field]) != group]
        test = [record for record in records if str(record[group_field]) == group]
        train_classes = sorted({label(record) for record in train})
        if train_classes != expected_classes:
            raise ValueError(
                f"Fold {group_field}={group} lacks training classes: "
                f"expected {expected_classes}, got {train_classes}"
            )
        predicted = _fit_predict(train, test, label)
        true = [label(record) for record in test]
        all_true.extend(true)
        all_predicted.extend(predicted)
        folds.append(
            {
                "held_out_group": group,
                "train_count": len(train),
                "test_count": len(test),
                "metrics": classification_metrics(true, predicted),
            }
        )
    return {
        "group_field": group_field,
        "fold_count": len(folds),
        "folds": folds,
        "aggregate": classification_metrics(all_true, all_predicted),
    }


def descriptive_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return task_descriptive_summary(
        records,
        feature_names=FEATURE_NAMES,
        task_field="task_id",
        group_field="actor",
        group_collection_key="actors",
    )


def validate_formal_matrix(
    records: Sequence[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Require the exact 3 actors x 3 tasks x 3 repetitions formal matrix."""

    if manifest is None:
        manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    actors = tuple(actor["id"] for actor in manifest["actors"])
    tasks = tuple(task["id"] for task in manifest["tasks"])
    expected = {(actor, task, repetition) for actor in actors for task in tasks for repetition in (1, 2, 3)}
    observed: Counter[tuple[str, str, int]] = Counter()
    sessions: set[str] = set()
    for record in records:
        session_id = str(record.get("session_id", ""))
        if not session_id or session_id in sessions:
            raise ValueError("Formal matrix contains a missing or duplicate session_id")
        sessions.add(session_id)
        if record.get("evidence_valid") is not True:
            raise ValueError(f"Session {session_id} has not passed evidence/hash validation")
        if record.get("protocol_version") != manifest["protocol_version"]:
            raise ValueError(f"Session {session_id} uses another protocol version")
        observed[(str(record["actor"]), str(record["task_id"]), int(record["repetition"]))] += 1
    if len(records) != 27 or set(observed) != expected or any(count != 1 for count in observed.values()):
        missing = sorted(expected - set(observed))
        extra = sorted(set(observed) - expected)
        duplicates = sorted(cell for cell, count in observed.items() if count != 1)
        raise ValueError(
            f"Formal analysis requires exactly 27 unique cells; count={len(records)}, "
            f"missing={missing}, extra={extra}, duplicates={duplicates}"
        )
    fingerprints = {str(record.get("provenance_fingerprint")) for record in records}
    if len(fingerprints) != 1 or "None" in fingerprints:
        raise ValueError("Formal sessions do not share one complete provenance binding")
    return {"session_count": 27, "cell_count": 27, "provenance_fingerprint": next(iter(fingerprints))}


def analyze_records(
    records: Sequence[Mapping[str, Any]],
    *,
    allow_incomplete_diagnostic: bool = False,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        matrix_gate = validate_formal_matrix(records, manifest=manifest)
        formal_matrix_valid, matrix_error = True, None
    except ValueError as error:
        if not allow_incomplete_diagnostic:
            raise
        formal_matrix_valid, matrix_error, matrix_gate = False, str(error), None
    if len(records) < 6:
        raise ValueError("Diagnostic analysis requires at least six valid session bundles")
    for record in records:
        validate_feature_vector(record["features"])
    actor_counts = Counter(str(record["actor"]) for record in records)
    task_counts = Counter(str(record["task_id"]) for record in records)
    if set(actor_counts) != {"human", "aio", "seeact"}:
        raise ValueError(f"Expected human/aio/seeact actors, got {sorted(actor_counts)}")
    if len(task_counts) != 3:
        raise ValueError(f"Expected exactly three tasks, got {len(task_counts)}")

    binary = grouped_cross_validation(
        records,
        group_field="task_id",
        label=lambda record: "human" if record["actor"] == "human" else "agent",
    )
    three_class = grouped_cross_validation(
        records,
        group_field="task_id",
        label=lambda record: str(record["actor"]),
    )
    # This negative control asks whether the behavioral features identify the
    # task when repetition, rather than task, is held out. A high result warns
    # that task mechanics may confound apparent actor differences.
    task_negative_control = grouped_cross_validation(
        records,
        group_field="repetition",
        label=lambda record: str(record["task_id"]),
    )
    return {
        "schema_version": "scienceboard-exploratory-analysis-v1",
        "analysis_status": (
            "formal_single_participant_pipeline_probe"
            if formal_matrix_valid
            else "non_formal_diagnostic_allow_incomplete"
        ),
        "formal_matrix_valid": formal_matrix_valid,
        "formal_matrix_gate": matrix_gate,
        "diagnostic_warning": (
            None
            if formal_matrix_valid
            else "INCOMPLETE MATRIX: diagnostic output is non-formal and must not be reported as the planned result. "
            + str(matrix_error)
        ),
        "sample_count": len(records),
        "actor_counts": dict(sorted(actor_counts.items())),
        "task_counts": dict(sorted(task_counts.items())),
        "feature_names": list(FEATURE_NAMES),
        "model": MODEL_CONFIG,
        "descriptive": descriptive_summary(records),
        "human_vs_agent_leave_one_task_out": binary,
        "human_vs_aio_vs_seeact_leave_one_task_out": three_class,
        "task_identification_leave_one_repetition_out_negative_control": task_negative_control,
        "claim_boundary": (
            "Single-participant, three-task exploratory pipeline probe. Metrics do not estimate "
            "population performance, future-agent generalization, or deployable detector accuracy."
        ),
    }


def load_records(root: Path, *, manifest_path: Path = DEFAULT_MANIFEST) -> list[dict[str, Any]]:
    protocol = json.loads(manifest_path.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = []
    for manifest_path in sorted(root.rglob("run_manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "scienceboard-session-run-v2":
            continue
        if manifest.get("evidence_valid") is not True:
            raise ValueError(f"Run manifest is not evidence-valid: {manifest_path}")
        verify_stored_artifact_hashes(manifest_path.parent, manifest)
        expected = {
            "session_id": manifest["session_id"], "actor": manifest["actor"],
            "actor_type": manifest["actor_type"], "source_class": manifest["source_class"],
            "task_id": manifest["task_id"], "repetition": manifest["repetition"],
        }
        evidence = validate_session_evidence(manifest_path.parent, expected=expected, manifest=protocol)
        trace_path = (manifest_path.parent / manifest["artifacts"]["trace"]["path"]).resolve()
        metadata, events, terminal = load_trace(trace_path)
        feature_result = extract_features(
            events,
            trace_origin_ms=float(metadata["monotonic_origin_ms"]),
            session_duration_ms=float(terminal["duration_ms"]),
        )
        fingerprint_payload = {
            "protocol_version": manifest["protocol_version"],
            "source_provenance": manifest["source_provenance"],
        }
        fingerprint = json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"))
        records.append(
            {
                "session_id": manifest["session_id"],
                "actor": manifest["actor"],
                "task_id": manifest["task_id"],
                "repetition": int(manifest["repetition"]),
                "validator_passed": manifest.get("validator_passed"),
                "source_class": manifest["source_class"],
                "protocol_version": manifest["protocol_version"],
                "evidence_valid": evidence["validator_passed"] is not None,
                "provenance_fingerprint": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest(),
                "features": feature_result["features"],
                "quality": feature_result["quality"],
            }
        )
    return records


def dry_run_payload() -> dict[str, Any]:
    return {
        "analysis_status": "designed_not_run",
        "writes_performed": False,
        "feature_names": list(FEATURE_NAMES),
        "model": MODEL_CONFIG,
        "primary_split": "leave-one-task-out",
        "negative_control_split": "leave-one-repetition-out task identification",
        "minimum_formal_matrix": "3 actors x 3 tasks x 3 repetitions = 27 sessions",
        "default_gate": "exact 27-cell evidence/hash-validated matrix; no partial fallback",
        "diagnostic_override": "--allow-incomplete-diagnostic produces explicitly non-formal output",
    }


def _private_analysis_path(path: Path) -> Path:
    """Analysis reads and writes private per-session data only under CUA_PRIVATE_ROOT."""

    return require_private_path(path, label="ScienceBoard analysis input/output")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-incomplete-diagnostic", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(json.dumps(dry_run_payload(), indent=2))
        return 0
    if args.input is None:
        print("--input is required unless --dry-run is used", file=sys.stderr)
        return 2
    try:
        input_root = _private_analysis_path(args.input)
        output = _private_analysis_path(args.output) if args.output else None
    except PrivateStorageError as error:
        print(str(error), file=sys.stderr)
        return 2
    records = load_records(input_root)
    report = analyze_records(records, allow_incomplete_diagnostic=args.allow_incomplete_diagnostic)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
