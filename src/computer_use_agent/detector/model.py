"""Small deterministic models used by the exploratory detector pipeline."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


def _sigmoid(value: float) -> float:
    if value >= 0:
        exponent = math.exp(-min(value, 700.0))
        return 1.0 / (1.0 + exponent)
    exponent = math.exp(max(value, -700.0))
    return exponent / (1.0 + exponent)


@dataclass(frozen=True)
class Standardizer:
    means: tuple[float, ...]
    scales: tuple[float, ...]

    @classmethod
    def fit(cls, matrix: Sequence[Sequence[float]]) -> Standardizer:
        if not matrix:
            raise ValueError("Cannot fit a standardizer on an empty matrix")
        width = len(matrix[0])
        if width == 0 or any(len(row) != width for row in matrix):
            raise ValueError("Model matrix width is empty or inconsistent")
        means = tuple(
            statistics.fmean(float(row[index]) for row in matrix) for index in range(width)
        )
        scales = []
        for index, mean in enumerate(means):
            variance = statistics.fmean((float(row[index]) - mean) ** 2 for row in matrix)
            deviation = math.sqrt(variance)
            scales.append(deviation if deviation > 1e-12 else 1.0)
        return cls(means=means, scales=tuple(scales))

    def transform(self, matrix: Sequence[Sequence[float]]) -> list[list[float]]:
        if any(len(row) != len(self.means) for row in matrix):
            raise ValueError("Matrix does not match fitted feature width")
        return [
            [
                (float(value) - self.means[index]) / self.scales[index]
                for index, value in enumerate(row)
            ]
            for row in matrix
        ]


class BinaryLogisticRegression:
    """Deterministic full-batch L2-regularized logistic regression."""

    def __init__(
        self,
        *,
        epochs: int = 500,
        learning_rate: float = 0.06,
        l2: float = 0.03,
    ) -> None:
        if epochs < 1 or learning_rate <= 0 or l2 < 0:
            raise ValueError("Invalid logistic-regression hyperparameters")
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.l2 = l2
        self.weights: list[float] = []
        self.bias = 0.0

    def fit(
        self,
        matrix: Sequence[Sequence[float]],
        labels: Sequence[int],
    ) -> BinaryLogisticRegression:
        if not matrix or len(matrix) != len(labels):
            raise ValueError("Training matrix/label length mismatch")
        if set(labels) != {0, 1}:
            raise ValueError("Binary training fold must contain both classes")
        width = len(matrix[0])
        if width == 0 or any(len(row) != width for row in matrix):
            raise ValueError("Training matrix width is empty or inconsistent")
        self.weights = [0.0] * width
        self.bias = 0.0
        sample_count = len(matrix)
        class_counts = {label: labels.count(label) for label in (0, 1)}
        class_weights = {label: sample_count / (2.0 * class_counts[label]) for label in (0, 1)}
        for _ in range(self.epochs):
            weight_gradient = [0.0] * width
            bias_gradient = 0.0
            for row, label in zip(matrix, labels, strict=True):
                score = self.bias + sum(
                    weight * float(value) for weight, value in zip(self.weights, row, strict=True)
                )
                error = (_sigmoid(score) - label) * class_weights[label]
                bias_gradient += error
                for index, value in enumerate(row):
                    weight_gradient[index] += error * float(value)
            self.bias -= self.learning_rate * bias_gradient / sample_count
            for index in range(width):
                gradient = weight_gradient[index] / sample_count + self.l2 * self.weights[index]
                self.weights[index] -= self.learning_rate * gradient
        return self

    def decision_function(self, matrix: Sequence[Sequence[float]]) -> list[float]:
        if not self.weights:
            raise ValueError("Model is not fitted")
        if any(len(row) != len(self.weights) for row in matrix):
            raise ValueError("Prediction matrix width mismatch")
        return [
            self.bias
            + sum(weight * float(value) for weight, value in zip(self.weights, row, strict=True))
            for row in matrix
        ]

    def predict_proba(self, matrix: Sequence[Sequence[float]]) -> list[float]:
        return [_sigmoid(score) for score in self.decision_function(matrix)]


class PlattCalibrator:
    """Regularized scalar logistic calibration fitted on nested OOF scores."""

    def __init__(
        self,
        *,
        epochs: int = 500,
        learning_rate: float = 0.03,
        l2: float = 0.02,
    ) -> None:
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.l2 = l2
        self.slope = 1.0
        self.intercept = 0.0

    def fit(self, scores: Sequence[float], labels: Sequence[int]) -> PlattCalibrator:
        if not scores or len(scores) != len(labels) or set(labels) != {0, 1}:
            raise ValueError("Calibration requires matched scores with both classes")
        self.slope = 0.0
        prevalence = (sum(labels) + 1.0) / (len(labels) + 2.0)
        self.intercept = math.log(prevalence / (1.0 - prevalence))
        sample_count = len(scores)
        for _ in range(self.epochs):
            slope_gradient = 0.0
            intercept_gradient = 0.0
            for score, label in zip(scores, labels, strict=True):
                error = _sigmoid(self.slope * float(score) + self.intercept) - label
                slope_gradient += error * float(score)
                intercept_gradient += error
            self.slope -= self.learning_rate * (
                slope_gradient / sample_count + self.l2 * self.slope
            )
            self.intercept -= self.learning_rate * intercept_gradient / sample_count
        return self

    def transform(self, scores: Sequence[float]) -> list[float]:
        return [_sigmoid(self.slope * float(score) + self.intercept) for score in scores]


class SoftmaxRegression:
    """Deterministic multiclass model for the task-identification control."""

    def __init__(
        self,
        *,
        epochs: int = 500,
        learning_rate: float = 0.05,
        l2: float = 0.03,
    ) -> None:
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.l2 = l2
        self.classes: list[str] = []
        self.weights: list[list[float]] = []
        self.bias: list[float] = []

    @staticmethod
    def _softmax(scores: Sequence[float]) -> list[float]:
        maximum = max(scores)
        exponents = [math.exp(score - maximum) for score in scores]
        total = sum(exponents)
        return [value / total for value in exponents]

    def fit(
        self,
        matrix: Sequence[Sequence[float]],
        labels: Sequence[str],
    ) -> SoftmaxRegression:
        if not matrix or len(matrix) != len(labels):
            raise ValueError("Multiclass matrix/label length mismatch")
        self.classes = sorted(set(labels))
        if len(self.classes) < 3:
            raise ValueError("Task-identification control requires at least three classes")
        width = len(matrix[0])
        if width == 0 or any(len(row) != width for row in matrix):
            raise ValueError("Multiclass matrix width is empty or inconsistent")
        class_index = {label: index for index, label in enumerate(self.classes)}
        self.weights = [[0.0] * width for _ in self.classes]
        self.bias = [0.0] * len(self.classes)
        sample_count = len(matrix)
        for _ in range(self.epochs):
            weight_gradient = [[0.0] * width for _ in self.classes]
            bias_gradient = [0.0] * len(self.classes)
            for row, label in zip(matrix, labels, strict=True):
                scores = [
                    self.bias[class_id]
                    + sum(
                        weight * float(value)
                        for weight, value in zip(self.weights[class_id], row, strict=True)
                    )
                    for class_id in range(len(self.classes))
                ]
                probabilities = self._softmax(scores)
                expected = class_index[label]
                for class_id, probability in enumerate(probabilities):
                    error = probability - float(class_id == expected)
                    bias_gradient[class_id] += error
                    for feature_id, value in enumerate(row):
                        weight_gradient[class_id][feature_id] += error * float(value)
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
        if not self.weights:
            raise ValueError("Multiclass model is not fitted")
        predictions: list[str] = []
        for row in matrix:
            scores = [
                self.bias[class_id]
                + sum(
                    weight * float(value)
                    for weight, value in zip(self.weights[class_id], row, strict=True)
                )
                for class_id in range(len(self.classes))
            ]
            predictions.append(self.classes[max(range(len(scores)), key=scores.__getitem__)])
        return predictions
