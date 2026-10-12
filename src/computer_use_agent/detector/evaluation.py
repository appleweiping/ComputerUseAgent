"""Leakage-controlled evaluation, controls, uncertainty, and ablations."""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from computer_use_agent.study.policy import HumanDataPolicy

from .contract import (
    FEATURE_ALLOWLIST,
    FEATURE_GROUPS,
    model_matrix,
    validate_dataset,
    validate_feature_names,
)
from .model import (
    BinaryLogisticRegression,
    PlattCalibrator,
    SoftmaxRegression,
    Standardizer,
)


@dataclass(frozen=True)
class EvaluationConfig:
    seed: int = 20260808
    epochs: int = 500
    learning_rate: float = 0.06
    l2: float = 0.03
    calibration_epochs: int = 500
    calibration_learning_rate: float = 0.03
    calibration_l2: float = 0.02
    bootstrap_repetitions: int = 1000
    permutation_repetitions: int = 100
    threshold: float = 0.5
    calibration_bins: int = 5

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "algorithm": "deterministic_full_batch_logistic_regression",
            "epochs": self.epochs,
            "learning_rate": self.learning_rate,
            "l2": self.l2,
            "class_weight": "balanced_from_outer_training_fold",
            "calibration": "nested_task_oof_platt_scaling",
            "calibration_epochs": self.calibration_epochs,
            "calibration_learning_rate": self.calibration_learning_rate,
            "calibration_l2": self.calibration_l2,
            "bootstrap": "task_cluster_percentile",
            "bootstrap_repetitions": self.bootstrap_repetitions,
            "permutation": "within_task_label_permutation",
            "permutation_repetitions": self.permutation_repetitions,
            "threshold": self.threshold,
            "calibration_bins": self.calibration_bins,
        }


#: Named analysis profiles. ``default`` is the documented protocol; ``smoke``
#: is a fast engineering check of every code path (fewer epochs, bootstrap
#: draws and permutations) whose numbers must never be compared with a
#: default-profile run.
EVALUATION_PROFILES: Mapping[str, EvaluationConfig] = MappingProxyType(
    {
        "default": EvaluationConfig(),
        "smoke": EvaluationConfig(
            epochs=60,
            calibration_epochs=60,
            bootstrap_repetitions=200,
            permutation_repetitions=5,
        ),
    }
)


def evaluation_config_for_profile(
    profile: str = "default",
    *,
    seed: int | None = None,
    epochs: int | None = None,
    bootstrap_repetitions: int | None = None,
    permutation_repetitions: int | None = None,
) -> EvaluationConfig:
    """Return a profile's configuration with optional explicit overrides.

    ``epochs`` sets both the logistic-regression and the calibration epochs.
    """

    try:
        config = EVALUATION_PROFILES[profile]
    except KeyError:
        raise ValueError(
            f"Unknown analysis profile {profile!r}; choose one of {sorted(EVALUATION_PROFILES)}"
        ) from None
    overrides: dict[str, int] = {}
    if seed is not None:
        overrides["seed"] = seed
    if epochs is not None:
        overrides["epochs"] = epochs
        overrides["calibration_epochs"] = epochs
    if bootstrap_repetitions is not None:
        overrides["bootstrap_repetitions"] = bootstrap_repetitions
    if permutation_repetitions is not None:
        overrides["permutation_repetitions"] = permutation_repetitions
    if any(value < 1 for name, value in overrides.items() if name != "seed"):
        raise ValueError("epochs, bootstrap repetitions, and permutations must be positive")
    return replace(config, **overrides)


def _label_value(record: Mapping[str, Any]) -> int:
    return int(record["label"] == "agent")


def _round(value: float) -> float:
    return round(float(value), 8)


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("Cannot calculate a percentile of an empty sample")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _roc_auc(labels: Sequence[int], probabilities: Sequence[float]) -> float:
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        raise ValueError("ROC AUC requires both classes")
    ordered = sorted(enumerate(probabilities), key=lambda item: (float(item[1]), item[0]))
    rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average_rank = ((index + 1) + end) / 2.0
        rank_sum += average_rank * sum(labels[row_id] for row_id, _ in ordered[index:end])
        index = end
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def binary_metrics(
    predictions: Sequence[Mapping[str, Any]],
    *,
    threshold: float = 0.5,
    calibration_bins: int = 5,
) -> dict[str, Any]:
    if not predictions:
        raise ValueError("No predictions to score")
    labels = [int(row["true_label"] == "agent") for row in predictions]
    probabilities = [float(row["agent_probability"]) for row in predictions]
    if any(not 0 <= value <= 1 for value in probabilities):
        raise ValueError("Probability outside [0, 1]")
    predicted = [int(value >= threshold) for value in probabilities]
    true_positive = sum(
        actual == guess == 1 for actual, guess in zip(labels, predicted, strict=True)
    )
    true_negative = sum(
        actual == guess == 0 for actual, guess in zip(labels, predicted, strict=True)
    )
    false_positive = sum(
        actual == 0 and guess == 1 for actual, guess in zip(labels, predicted, strict=True)
    )
    false_negative = sum(
        actual == 1 and guess == 0 for actual, guess in zip(labels, predicted, strict=True)
    )
    positive_recall = true_positive / (true_positive + false_negative)
    negative_recall = true_negative / (true_negative + false_positive)
    precision = (
        true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    )
    recall = positive_recall
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    brier = statistics.fmean(
        (probability - label) ** 2 for probability, label in zip(probabilities, labels, strict=True)
    )
    epsilon = 1e-12
    log_loss = -statistics.fmean(
        label * math.log(max(epsilon, probability))
        + (1 - label) * math.log(max(epsilon, 1 - probability))
        for probability, label in zip(probabilities, labels, strict=True)
    )
    calibration = []
    expected_calibration_error = 0.0
    for bin_id in range(calibration_bins):
        lower = bin_id / calibration_bins
        upper = (bin_id + 1) / calibration_bins
        members = [
            index
            for index, probability in enumerate(probabilities)
            if probability >= lower
            and (probability < upper or (bin_id == calibration_bins - 1 and probability <= upper))
        ]
        if not members:
            continue
        mean_probability = statistics.fmean(probabilities[index] for index in members)
        observed_fraction = statistics.fmean(labels[index] for index in members)
        expected_calibration_error += (
            len(members) / len(labels) * abs(mean_probability - observed_fraction)
        )
        calibration.append(
            {
                "lower": _round(lower),
                "upper": _round(upper),
                "count": len(members),
                "mean_probability": _round(mean_probability),
                "observed_agent_fraction": _round(observed_fraction),
            }
        )
    return {
        "count": len(labels),
        "threshold": _round(threshold),
        "accuracy": _round((true_positive + true_negative) / len(labels)),
        "balanced_accuracy": _round((positive_recall + negative_recall) / 2),
        "agent_precision": _round(precision),
        "agent_recall": _round(recall),
        "agent_f1": _round(f1),
        "roc_auc": _round(_roc_auc(labels, probabilities)),
        "brier_score": _round(brier),
        "log_loss": _round(log_loss),
        "expected_calibration_error": _round(expected_calibration_error),
        "confusion_matrix": {
            "labels": ["human", "agent"],
            "values": [[true_negative, false_positive], [false_negative, true_positive]],
        },
        "calibration_curve": calibration,
    }


def _new_model(config: EvaluationConfig) -> BinaryLogisticRegression:
    return BinaryLogisticRegression(
        epochs=config.epochs,
        learning_rate=config.learning_rate,
        l2=config.l2,
    )


def _fit_base(
    train: Sequence[Mapping[str, Any]],
    test: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str],
    config: EvaluationConfig,
) -> tuple[Standardizer, BinaryLogisticRegression, list[float]]:
    standardizer = Standardizer.fit(model_matrix(train, feature_names))
    train_matrix = standardizer.transform(model_matrix(train, feature_names))
    test_matrix = standardizer.transform(model_matrix(test, feature_names))
    model = _new_model(config).fit(train_matrix, [_label_value(row) for row in train])
    return standardizer, model, model.decision_function(test_matrix)


def _nested_calibrator(
    train: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str],
    config: EvaluationConfig,
) -> tuple[PlattCalibrator, dict[str, Any]]:
    tasks = sorted({str(row["task_group"]) for row in train})
    if len(tasks) < 2:
        raise ValueError("Nested task calibration requires two training tasks")
    scores: list[float] = []
    labels: list[int] = []
    fold_counts = []
    for held_out in tasks:
        inner_train = [row for row in train if row["task_group"] != held_out]
        inner_test = [row for row in train if row["task_group"] == held_out]
        if {row["label"] for row in inner_train} != {"human", "agent"}:
            raise ValueError("Nested calibration training fold lacks a class")
        _, _, inner_scores = _fit_base(inner_train, inner_test, feature_names, config)
        scores.extend(inner_scores)
        labels.extend(_label_value(row) for row in inner_test)
        fold_counts.append(
            {
                "held_out_task": held_out,
                "train_count": len(inner_train),
                "calibration_count": len(inner_test),
            }
        )
    calibrator = PlattCalibrator(
        epochs=config.calibration_epochs,
        learning_rate=config.calibration_learning_rate,
        l2=config.calibration_l2,
    ).fit(scores, labels)
    return calibrator, {
        "method": "nested_task_oof_platt_scaling",
        "score_count": len(scores),
        "folds": fold_counts,
        "slope": _round(calibrator.slope),
        "intercept": _round(calibrator.intercept),
    }


def evaluate_loto(
    records: Sequence[Mapping[str, Any]],
    *,
    feature_names: Sequence[str] = FEATURE_ALLOWLIST,
    config: EvaluationConfig | None = None,
    human_research_config: Mapping[str, Any] | None = None,
    human_data_policy: HumanDataPolicy | None = None,
) -> dict[str, Any]:
    """Run strict leave-one-task-out detection with nested calibration."""

    config = config or EvaluationConfig()
    validate_dataset(
        records,
        human_research_config=human_research_config,
        human_data_policy=human_data_policy,
    )
    selected = validate_feature_names(feature_names)
    tasks = sorted({str(row["task_group"]) for row in records})
    folds = []
    all_predictions: list[dict[str, Any]] = []
    coefficient_rows: list[list[float]] = []
    for held_out in tasks:
        train = [row for row in records if row["task_group"] != held_out]
        test = [row for row in records if row["task_group"] == held_out]
        if {row["label"] for row in train} != {"human", "agent"}:
            raise ValueError(f"Outer training fold {held_out!r} lacks a class")
        calibrator, calibration_manifest = _nested_calibrator(train, selected, config)
        standardizer, model, scores = _fit_base(train, test, selected, config)
        probabilities = calibrator.transform(scores)
        predictions = [
            {
                "record_ref": str(row["record_ref"]),
                "task_group": held_out,
                "true_label": str(row["label"]),
                "predicted_label": "agent" if probability >= config.threshold else "human",
                "agent_probability": _round(probability),
            }
            for row, probability in zip(test, probabilities, strict=True)
        ]
        all_predictions.extend(predictions)
        effective_coefficients = [calibrator.slope * weight for weight in model.weights]
        coefficient_rows.append(effective_coefficients)
        folds.append(
            {
                "held_out_task": held_out,
                "train_count": len(train),
                "test_count": len(test),
                "train_only_standardization": {
                    "mean_sha256": hashlib.sha256(
                        repr(tuple(round(value, 12) for value in standardizer.means)).encode(
                            "ascii"
                        )
                    ).hexdigest(),
                    "scale_sha256": hashlib.sha256(
                        repr(tuple(round(value, 12) for value in standardizer.scales)).encode(
                            "ascii"
                        )
                    ).hexdigest(),
                },
                "calibration": calibration_manifest,
                "metrics": binary_metrics(
                    predictions,
                    threshold=config.threshold,
                    calibration_bins=config.calibration_bins,
                ),
            }
        )
    coefficient_summary: list[dict[str, Any]] = [
        {
            "feature": feature,
            "mean_standardized_effect": _round(
                statistics.fmean(row[index] for row in coefficient_rows)
            ),
            "mean_absolute_standardized_effect": _round(
                statistics.fmean(abs(row[index]) for row in coefficient_rows)
            ),
        }
        for index, feature in enumerate(selected)
    ]
    coefficient_summary.sort(
        key=lambda row: (-float(row["mean_absolute_standardized_effect"]), str(row["feature"]))
    )
    return {
        "protocol": "strict_leave_one_task_out_v1",
        "feature_names": list(selected),
        "fold_count": len(folds),
        "folds": folds,
        "aggregate": binary_metrics(
            all_predictions,
            threshold=config.threshold,
            calibration_bins=config.calibration_bins,
        ),
        "predictions": sorted(
            all_predictions, key=lambda row: (str(row["task_group"]), str(row["record_ref"]))
        ),
        "coefficient_summary": coefficient_summary,
    }


def task_cluster_bootstrap(
    predictions: Sequence[Mapping[str, Any]],
    *,
    config: EvaluationConfig,
) -> dict[str, Any]:
    """Percentile intervals from deterministic resampling of whole tasks."""

    by_task: dict[str, list[Mapping[str, Any]]] = {}
    for row in predictions:
        by_task.setdefault(str(row["task_group"]), []).append(row)
    tasks = sorted(by_task)
    if len(tasks) < 3:
        raise ValueError("Task-cluster bootstrap requires at least three tasks")
    rng = random.Random(config.seed)
    metric_names = ("accuracy", "balanced_accuracy", "agent_f1", "roc_auc", "brier_score")
    draws: dict[str, list[float]] = {name: [] for name in metric_names}
    for _ in range(config.bootstrap_repetitions):
        sampled = [tasks[rng.randrange(len(tasks))] for _ in tasks]
        rows = [item for task in sampled for item in by_task[task]]
        scores = binary_metrics(
            rows,
            threshold=config.threshold,
            calibration_bins=config.calibration_bins,
        )
        for name in metric_names:
            draws[name].append(float(scores[name]))
    return {
        "method": "task_cluster_percentile_bootstrap",
        "seed": config.seed,
        "repetitions": config.bootstrap_repetitions,
        "task_count": len(tasks),
        "intervals_95": {
            name: {
                "lower": _round(_percentile(values, 0.025)),
                "upper": _round(_percentile(values, 0.975)),
            }
            for name, values in draws.items()
        },
    }


def task_identification_control(
    records: Sequence[Mapping[str, Any]],
    *,
    feature_names: Sequence[str] = FEATURE_ALLOWLIST,
    config: EvaluationConfig,
    folds: int = 3,
    human_research_config: Mapping[str, Any] | None = None,
    human_data_policy: HumanDataPolicy | None = None,
) -> dict[str, Any]:
    """Measure task encoding using deterministic stratified three-fold CV."""

    validate_dataset(
        records,
        human_research_config=human_research_config,
        human_data_policy=human_data_policy,
    )
    selected = validate_feature_names(feature_names)
    tasks = sorted({str(row["task_group"]) for row in records})
    assignments: dict[str, int] = {}
    for task in tasks:
        task_rows = sorted(
            (row for row in records if row["task_group"] == task),
            key=lambda row: str(row["record_ref"]),
        )
        if len(task_rows) < folds:
            raise ValueError("Every task needs one record in every control fold")
        for index, row in enumerate(task_rows):
            assignments[str(row["record_ref"])] = index % folds
    true: list[str] = []
    predicted: list[str] = []
    fold_rows = []
    for fold in range(folds):
        train = [row for row in records if assignments[str(row["record_ref"])] != fold]
        test = [row for row in records if assignments[str(row["record_ref"])] == fold]
        if sorted({str(row["task_group"]) for row in train}) != tasks:
            raise ValueError("A task-identification training fold lacks a task")
        standardizer = Standardizer.fit(model_matrix(train, selected))
        train_matrix = standardizer.transform(model_matrix(train, selected))
        test_matrix = standardizer.transform(model_matrix(test, selected))
        model = SoftmaxRegression(
            epochs=config.epochs,
            learning_rate=config.learning_rate,
            l2=config.l2,
        ).fit(train_matrix, [str(row["task_group"]) for row in train])
        fold_true = [str(row["task_group"]) for row in test]
        fold_predicted = model.predict(test_matrix)
        true.extend(fold_true)
        predicted.extend(fold_predicted)
        fold_rows.append(
            {
                "fold": fold,
                "train_count": len(train),
                "test_count": len(test),
                "accuracy": _round(
                    statistics.fmean(
                        actual == guess
                        for actual, guess in zip(fold_true, fold_predicted, strict=True)
                    )
                ),
            }
        )
    confusion = {actual: dict.fromkeys(tasks, 0) for actual in tasks}
    for actual, guess in zip(true, predicted, strict=True):
        confusion[actual][guess] += 1
    recalls = [confusion[task][task] / sum(confusion[task].values()) for task in tasks]
    return {
        "purpose": "negative_control_for_task_encoding_not_detector_performance",
        "protocol": "deterministic_task_stratified_3_fold",
        "folds": fold_rows,
        "accuracy": _round(
            statistics.fmean(actual == guess for actual, guess in zip(true, predicted, strict=True))
        ),
        "balanced_accuracy": _round(statistics.fmean(recalls)),
        "chance_accuracy": _round(1 / len(tasks)),
        "confusion_matrix": {
            "labels": tasks,
            "values": [[confusion[actual][guess] for guess in tasks] for actual in tasks],
        },
    }


def permutation_test(
    records: Sequence[Mapping[str, Any]],
    *,
    observed_balanced_accuracy: float,
    feature_names: Sequence[str],
    config: EvaluationConfig,
    human_research_config: Mapping[str, Any] | None = None,
    human_data_policy: HumanDataPolicy | None = None,
) -> dict[str, Any]:
    """Shuffle labels within tasks and rerun the full held-out protocol."""

    null_scores = []
    for repetition in range(config.permutation_repetitions):
        rng = random.Random(config.seed + 10_000 + repetition)
        permuted: list[dict[str, Any]] = []
        for task in sorted({str(row["task_group"]) for row in records}):
            task_rows = [dict(row) for row in records if row["task_group"] == task]
            labels = [str(row["label"]) for row in task_rows]
            rng.shuffle(labels)
            for row, label in zip(task_rows, labels, strict=True):
                row["label"] = label
                # Family provenance describes the observed actor, while this
                # null deliberately replaces the target. It is not a model
                # input and must not be allowed to contradict the permuted
                # label contract.
                row["agent_family"] = None
                permuted.append(row)
        score = evaluate_loto(
            permuted,
            feature_names=feature_names,
            config=config,
            human_research_config=human_research_config,
            human_data_policy=human_data_policy,
        )["aggregate"]["balanced_accuracy"]
        null_scores.append(float(score))
    extreme = sum(score >= observed_balanced_accuracy for score in null_scores)
    return {
        "method": "within_task_label_permutation_full_loto",
        "seed": config.seed,
        "repetitions": config.permutation_repetitions,
        "observed_balanced_accuracy": _round(observed_balanced_accuracy),
        "null_mean_balanced_accuracy": _round(statistics.fmean(null_scores)),
        "null_p95_balanced_accuracy": _round(_percentile(null_scores, 0.95)),
        "p_value_one_sided": _round((extreme + 1) / (len(null_scores) + 1)),
    }


def ablation_analysis(
    records: Sequence[Mapping[str, Any]],
    *,
    baseline_balanced_accuracy: float,
    config: EvaluationConfig,
    human_research_config: Mapping[str, Any] | None = None,
    human_data_policy: HumanDataPolicy | None = None,
) -> dict[str, Any]:
    """Drop each modality group and each feature under the same LOTO protocol."""

    def score_without(selected: tuple[str, ...]) -> float:
        return float(
            evaluate_loto(
                records,
                feature_names=selected,
                config=config,
                human_research_config=human_research_config,
                human_data_policy=human_data_policy,
            )["aggregate"]["balanced_accuracy"]
        )

    modalities = []
    for group, removed_group in FEATURE_GROUPS.items():
        score = score_without(
            tuple(name for name in FEATURE_ALLOWLIST if name not in removed_group)
        )
        modalities.append(
            {
                "dropped_group": group,
                "dropped_features": list(removed_group),
                "balanced_accuracy": _round(score),
                "delta_from_full": _round(score - baseline_balanced_accuracy),
            }
        )
    features: list[dict[str, Any]] = []
    for removed_feature in FEATURE_ALLOWLIST:
        score = score_without(tuple(name for name in FEATURE_ALLOWLIST if name != removed_feature))
        features.append(
            {
                "dropped_feature": removed_feature,
                "balanced_accuracy": _round(score),
                "delta_from_full": _round(score - baseline_balanced_accuracy),
            }
        )
    features.sort(key=lambda row: (float(row["delta_from_full"]), str(row["dropped_feature"])))
    return {
        "interpretation": "negative delta means performance fell after removal; exploratory only",
        "modality_group_drop": modalities,
        "single_feature_drop": features,
    }


def class_and_task_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "by_class": dict(sorted(Counter(str(row["label"]) for row in records).items())),
        "by_task_and_class": {
            task: dict(
                sorted(
                    Counter(
                        str(row["label"]) for row in records if row["task_group"] == task
                    ).items()
                )
            )
            for task in sorted({str(row["task_group"]) for row in records})
        },
    }
