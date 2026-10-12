"""Retrospective FP-Agent evaluation over raw-derived 23-feature records."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.metadata
import json
import platform
import random
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score, balanced_accuracy_score, brier_score_loss,
    confusion_matrix, f1_score, roc_auc_score,
)
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from computer_use_agent.detector.contract import FEATURE_ALLOWLIST, FEATURE_GROUPS
from research.fp_agent.evaluate_fp_agent_classifiers import evaluate as evaluate_released
from research.fp_agent.paths import (
    OSF_PROJECT_URL,
    PACKAGE_DIR,
    PrivatePathError,
    default_output_dir,
    refuse_evidence_output,
    require_data_root,
    require_upstream_repo,
)
from research.fp_agent.retrospective_features import (
    features_path,
    sha256,
    validate_private_artifact,
)

RESULT_FILENAME = "fp_agent_retrospective.json"
STATUS_FILENAME = "fp_agent_retrospective_status.json"
REQUIREMENTS_LOCK = PACKAGE_DIR / "requirements.lock"
SEED = 20260811
DEFAULT_CALIBRATION_BINS = 10
DEFAULT_CALIBRATION_SPLITS = 3
_PERMUTATION_RECORDS: list[Mapping[str, Any]] | None = None
_PERMUTATION_SEED = SEED
_PERMUTATION_CALIBRATION_SPLITS = DEFAULT_CALIBRATION_SPLITS
_PERMUTATION_CALIBRATION_BINS = DEFAULT_CALIBRATION_BINS


@dataclass(frozen=True)
class Protocol:
    seed: int = SEED
    bootstrap_repetitions: int = 2000
    permutation_repetitions: int = 1000
    permutation_workers: int = 4
    calibration_bins: int = DEFAULT_CALIBRATION_BINS
    calibration_splits: int = DEFAULT_CALIBRATION_SPLITS
    human_test_fraction: float = 0.2
    outer_group_purge: bool = True
    permutation_unit: str = "group_within_task_coverage"


def _record_ref(row: Mapping[str, Any], index: int) -> str:
    payload = json.dumps({"i": index, "family": row["family"], "task": row["task"],
                          "group": row["group"]}, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError("raw-derived private features_23.jsonl cache is absent")
    ledger_path = path.with_suffix(".ledger.json")
    if not ledger_path.is_file():
        raise FileNotFoundError("private features_23 ledger is absent")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if ledger.get("status") != "complete" or ledger.get("features") != list(FEATURE_ALLOWLIST):
        raise ValueError("private feature ledger is incomplete or has a changed allowlist")
    if ledger.get("derived_sha256") != sha256(path):
        raise ValueError("private features_23 SHA-256 does not match its extraction ledger")
    records = []
    with path.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            row = json.loads(line)
            if tuple(row.get("features", {})) != FEATURE_ALLOWLIST:
                raise ValueError(f"Row {index} violates the exact 23-feature allowlist")
            if set(row) != {"family", "label", "task", "group", "features"}:
                raise ValueError(f"Row {index} has an unexpected field")
            record = dict(row)
            record["record_ref"] = _record_ref(row, index)
            records.append(record)
    if len({row["record_ref"] for row in records}) != len(records):
        raise ValueError("Record reference collision")
    if {row["label"] for row in records} != {0, 1}:
        raise ValueError("Binary classes are incomplete")
    if {row["task"] for row in records} != {"Flight-booking", "Forums", "Shopping"}:
        raise ValueError("The prespecified three-task coverage is incomplete or changed")
    if len({row["family"] for row in records if row["label"] == 1}) != 7:
        raise ValueError("The prespecified seven agent families are incomplete or changed")
    return records


def _matrix(rows: Sequence[Mapping[str, Any]], names: Sequence[str]) -> np.ndarray:
    if not names or any(name not in FEATURE_ALLOWLIST for name in names):
        raise ValueError("Model matrix requested a non-allowlisted feature")
    return np.asarray([[float(row["features"][name]) for name in names] for row in rows])


def _model(kind: str, seed: int) -> Any:
    if kind == "logistic":
        return LogisticRegression(class_weight="balanced", max_iter=2000, random_state=seed)
    if kind == "xgboost":
        import xgboost as xgb
        return xgb.XGBClassifier(n_estimators=200, max_depth=4, learning_rate=0.05,
                                 subsample=1.0, colsample_bytree=1.0,
                                 eval_metric="logloss", random_state=seed, n_jobs=1)
    raise ValueError(kind)


def _fit_scores(train: Sequence[Mapping[str, Any]], test: Sequence[Mapping[str, Any]],
                names: Sequence[str], kind: str, seed: int) -> tuple[np.ndarray, dict[str, str]]:
    scaler = StandardScaler().fit(_matrix(train, names))
    x_train = scaler.transform(_matrix(train, names))
    x_test = scaler.transform(_matrix(test, names))
    model = _model(kind, seed).fit(x_train, [row["label"] for row in train])
    if hasattr(model, "decision_function"):
        scores = np.asarray(model.decision_function(x_test), dtype=float)
    else:
        probabilities = np.clip(model.predict_proba(x_test)[:, 1], 1e-9, 1 - 1e-9)
        scores = np.log(probabilities / (1 - probabilities))
    provenance = {
        "train_mean_sha256": hashlib.sha256(scaler.mean_.tobytes()).hexdigest(),
        "train_scale_sha256": hashlib.sha256(scaler.scale_.tobytes()).hexdigest(),
    }
    return scores, provenance


def _nested_probabilities(train: Sequence[Mapping[str, Any]], test: Sequence[Mapping[str, Any]],
                          names: Sequence[str], kind: str, seed: int,
                          splits: int = DEFAULT_CALIBRATION_SPLITS,
                          ) -> tuple[np.ndarray, dict[str, Any]]:
    if splits < 2:
        raise ValueError("Nested calibration requires at least two splits")
    labels = np.asarray([int(row["label"]) for row in train])
    groups = np.asarray([str(row["group"]) for row in train])
    if len(set(groups.tolist())) < splits:
        raise ValueError(f"Nested calibration requires at least {splits} training groups")
    splitter = StratifiedGroupKFold(n_splits=splits, shuffle=True, random_state=seed)
    oof_scores: list[float] = []
    oof_labels: list[int] = []
    folds = []
    placeholder = np.zeros((len(train), 1), dtype=float)
    for fold_index, (fit_indices, calibration_indices) in enumerate(
        splitter.split(placeholder, labels, groups)
    ):
        inner_train = [train[index] for index in fit_indices]
        inner_test = [train[index] for index in calibration_indices]
        if {row["label"] for row in inner_train} != {0, 1}:
            raise ValueError(f"Calibration fit fold {fold_index} lacks a class")
        if {row["label"] for row in inner_test} != {0, 1}:
            raise ValueError(f"Calibration score fold {fold_index} lacks a class")
        fit_groups = {str(row["group"]) for row in inner_train}
        calibration_groups = {str(row["group"]) for row in inner_test}
        overlap = fit_groups & calibration_groups
        if overlap:
            raise ValueError(f"Calibration fold {fold_index} leaks {len(overlap)} groups")
        scores, _ = _fit_scores(inner_train, inner_test, names, kind, seed)
        oof_scores.extend(scores.tolist())
        oof_labels.extend(int(row["label"]) for row in inner_test)
        folds.append({
            "fold": fold_index,
            "train": len(inner_train),
            "calibration": len(inner_test),
            "train_groups": len(fit_groups),
            "calibration_groups": len(calibration_groups),
            "group_overlap": 0,
        })
    calibrator = LogisticRegression(C=1e6, max_iter=2000, random_state=seed)
    calibrator.fit(np.asarray(oof_scores).reshape(-1, 1), oof_labels)
    test_scores, provenance = _fit_scores(train, test, names, kind, seed)
    probabilities = calibrator.predict_proba(test_scores.reshape(-1, 1))[:, 1]
    return probabilities, {"method": "nested_group_disjoint_oof_platt", "folds": folds,
                           "slope": float(calibrator.coef_[0, 0]),
                           "intercept": float(calibrator.intercept_[0]), **provenance}


def metrics(rows: Sequence[Mapping[str, Any]],
            bins: int = DEFAULT_CALIBRATION_BINS) -> dict[str, Any]:
    y = np.asarray([int(row["label"]) for row in rows])
    p = np.asarray([float(row["probability"]) for row in rows])
    guess = (p >= 0.5).astype(int)
    if set(y.tolist()) != {0, 1}:
        raise ValueError("Metrics require both classes")
    bin_ids = np.minimum((p * bins).astype(int), bins - 1)
    ece = sum(np.mean(bin_ids == index) * abs(np.mean(p[bin_ids == index]) -
              np.mean(y[bin_ids == index])) for index in range(bins) if np.any(bin_ids == index))
    frac, mean = calibration_curve(y, p, n_bins=bins, strategy="uniform")
    return {
        "count": len(rows), "macro_f1": float(f1_score(y, guess, average="macro")),
        "balanced_accuracy": float(balanced_accuracy_score(y, guess)),
        "auroc": float(roc_auc_score(y, p)), "auprc": float(average_precision_score(y, p)),
        "brier": float(brier_score_loss(y, p)), "ece": float(ece),
        "confusion": {"labels": ["human", "agent"],
                      "values": confusion_matrix(y, guess, labels=[0, 1]).tolist()},
        "calibration_curve": [{"mean_probability": float(x), "observed_fraction": float(z)}
                              for x, z in zip(mean, frac, strict=True)],
    }


def evaluate_folds(records: Sequence[Mapping[str, Any]], folds: Sequence[tuple[str, list, list]],
                   *, names: Sequence[str] = FEATURE_ALLOWLIST, kind: str = "logistic",
                   seed: int = SEED, calibration_splits: int = DEFAULT_CALIBRATION_SPLITS,
                   calibration_bins: int = DEFAULT_CALIBRATION_BINS) -> dict[str, Any]:
    predictions: list[dict[str, Any]] = []
    fold_results = []
    for fold_name, train, test in folds:
        try:
            train_groups = {str(row["group"]) for row in train}
            test_groups = {str(row["group"]) for row in test}
            overlap = train_groups & test_groups
            if overlap:
                raise ValueError(f"Outer fold {fold_name} leaks {len(overlap)} groups")
            probabilities, calibration = _nested_probabilities(
                train, test, names, kind, seed, calibration_splits
            )
            rows = [{"record_ref": row["record_ref"], "group": fold_name,
                     "label": row["label"], "probability": float(probability)}
                    for row, probability in zip(test, probabilities, strict=True)]
            predictions.extend(rows)
            fold_results.append({"fold": fold_name, "status": "complete", "train": len(train),
                                 "test": len(test), "calibration": calibration,
                                 "split_audit": {"train_groups": len(train_groups),
                                                 "test_groups": len(test_groups),
                                                 "group_overlap": 0},
                                 "metrics": metrics(rows, calibration_bins)})
        except Exception as exc:
            fold_results.append({"fold": fold_name, "status": "failed", "train": len(train),
                                 "test": len(test), "error_type": type(exc).__name__,
                                 "error": str(exc)})
    return {"model": kind, "features": list(names), "folds": fold_results,
            "failed_fold_count": sum(row["status"] == "failed" for row in fold_results),
            "aggregate": metrics(predictions, calibration_bins) if predictions else None,
            "predictions": predictions}


def loto_folds(records: Sequence[Mapping[str, Any]]) -> list[tuple[str, list, list]]:
    folds = []
    for task in sorted({r["task"] for r in records}):
        test = [row for row in records if row["task"] == task]
        test_groups = {str(row["group"]) for row in test}
        train = [
            row for row in records
            if row["task"] != task and str(row["group"]) not in test_groups
        ]
        folds.append((task, train, test))
    return folds


def _human_split(records: Sequence[Mapping[str, Any]], seed: int, fraction: float) -> tuple[list, list]:
    humans = [row for row in records if row["label"] == 0]
    groups = sorted({row["group"] for row in humans},
                    key=lambda value: hashlib.sha256(f"{seed}:{value}".encode()).hexdigest())
    test_groups = set(groups[:max(1, round(len(groups) * fraction))])
    return ([row for row in humans if row["group"] not in test_groups],
            [row for row in humans if row["group"] in test_groups])


def family_folds(records: Sequence[Mapping[str, Any]], protocol: Protocol) -> list[tuple[str, list, list]]:
    human_train, human_test = _human_split(records, protocol.seed, protocol.human_test_fraction)
    agents = [row for row in records if row["label"] == 1]
    families = sorted({row["family"] for row in agents})
    if len(families) != 7:
        raise ValueError(f"Expected seven agent families, found {len(families)}")
    return [(family, human_train + [r for r in agents if r["family"] != family],
             human_test + [r for r in agents if r["family"] == family]) for family in families]


def split_audit(
    records: Sequence[Mapping[str, Any]],
    task_folds: Sequence[tuple[str, list, list]],
    agent_folds: Sequence[tuple[str, list, list]],
) -> dict[str, Any]:
    grouped_tasks: dict[str, set[str]] = {}
    for row in records:
        grouped_tasks.setdefault(str(row["group"]), set()).add(str(row["task"]))

    def audit(folds: Sequence[tuple[str, list, list]]) -> list[dict[str, Any]]:
        rows = []
        for name, train, test in folds:
            train_groups = {str(row["group"]) for row in train}
            test_groups = {str(row["group"]) for row in test}
            rows.append({
                "fold": name,
                "train_rows": len(train),
                "test_rows": len(test),
                "purged_rows": len(records) - len(train) - len(test),
                "train_groups": len(train_groups),
                "test_groups": len(test_groups),
                "group_overlap": len(train_groups & test_groups),
            })
        return rows

    return {
        "group_semantics": "human participant token or agent trial index, irreversibly hashed",
        "groups": len(grouped_tasks),
        "multi_task_groups": sum(len(tasks) > 1 for tasks in grouped_tasks.values()),
        "outer_policy": "held-out task plus participant/trial-group purge",
        "calibration_policy": "training-only stratified group-disjoint OOF Platt",
        "task_folds": audit(task_folds),
        "agent_family_folds": audit(agent_folds),
    }


def group_bootstrap(predictions: Sequence[Mapping[str, Any]], repetitions: int, seed: int,
                    bins: int = DEFAULT_CALIBRATION_BINS) -> dict:
    grouped: dict[str, list] = {}
    for row in predictions:
        grouped.setdefault(str(row["group"]), []).append(row)
    names = sorted(grouped)
    rng = random.Random(seed)
    values = {name: [] for name in ("macro_f1", "balanced_accuracy", "auroc", "auprc", "brier", "ece")}
    for _ in range(repetitions):
        sample = [names[rng.randrange(len(names))] for _ in names]
        scored = metrics([row for name in sample for row in grouped[name]], bins)
        for name in values:
            values[name].append(scored[name])
    return {"method": "whole_outer_group_percentile", "groups": names,
            "repetitions": repetitions, "seed": seed,
            "intervals_95": {name: {"lower": float(np.quantile(rows, .025)),
                                     "upper": float(np.quantile(rows, .975))}
                             for name, rows in values.items()}}


def _initialize_permutation_worker(records: list[Mapping[str, Any]], seed: int,
                                   calibration_splits: int = DEFAULT_CALIBRATION_SPLITS,
                                   calibration_bins: int = DEFAULT_CALIBRATION_BINS) -> None:
    global _PERMUTATION_RECORDS, _PERMUTATION_SEED
    global _PERMUTATION_CALIBRATION_SPLITS, _PERMUTATION_CALIBRATION_BINS
    _PERMUTATION_RECORDS = records
    _PERMUTATION_SEED = seed
    _PERMUTATION_CALIBRATION_SPLITS = calibration_splits
    _PERMUTATION_CALIBRATION_BINS = calibration_bins


def _permutation_worker(repetition: int) -> float | None:
    if _PERMUTATION_RECORDS is None:
        raise RuntimeError("Permutation worker was not initialized")
    rng = random.Random(_PERMUTATION_SEED + 10000 + repetition)
    permuted = [dict(row) for row in _PERMUTATION_RECORDS]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in permuted:
        grouped.setdefault(str(row["group"]), []).append(row)
    strata: dict[tuple[str, ...], list[str]] = {}
    group_labels: dict[str, int] = {}
    for group, rows in grouped.items():
        labels = {int(row["label"]) for row in rows}
        if len(labels) != 1:
            raise ValueError(f"Group {group} has inconsistent labels")
        group_labels[group] = labels.pop()
        task_coverage = tuple(sorted({str(row["task"]) for row in rows}))
        strata.setdefault(task_coverage, []).append(group)
    for coverage in sorted(strata):
        members = sorted(strata[coverage])
        labels = [group_labels[group] for group in members]
        rng.shuffle(labels)
        for group, label in zip(members, labels, strict=True):
            for row in grouped[group]:
                row["label"] = label
    with threadpool_limits(limits=1):
        result = evaluate_folds(permuted, loto_folds(permuted), seed=_PERMUTATION_SEED,
                                calibration_splits=_PERMUTATION_CALIBRATION_SPLITS,
                                calibration_bins=_PERMUTATION_CALIBRATION_BINS)
    if result["aggregate"] is not None and not result["failed_fold_count"]:
        return float(result["aggregate"]["balanced_accuracy"])
    return None


def permutation_test(records: Sequence[Mapping[str, Any]], observed: float,
                     protocol: Protocol) -> dict:
    if not 1 <= protocol.permutation_workers <= 8:
        raise ValueError("permutation_workers must be between 1 and 8")

    if protocol.permutation_workers == 1:
        _initialize_permutation_worker(list(records), protocol.seed,
                                       protocol.calibration_splits, protocol.calibration_bins)
        outcomes = []
        for repetition in range(protocol.permutation_repetitions):
            outcomes.append(_permutation_worker(repetition))
            if (repetition + 1) % 100 == 0:
                print(json.dumps({"stage": "permutation", "completed": repetition + 1,
                                  "total": protocol.permutation_repetitions}), flush=True)
    else:
        outcomes = []
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=protocol.permutation_workers,
            initializer=_initialize_permutation_worker,
            initargs=(list(records), protocol.seed,
                      protocol.calibration_splits, protocol.calibration_bins),
        ) as pool:
            for repetition, score in enumerate(
                pool.map(_permutation_worker, range(protocol.permutation_repetitions)), start=1
            ):
                outcomes.append(score)
                if repetition % 100 == 0:
                    print(json.dumps({"stage": "permutation", "completed": repetition,
                                      "total": protocol.permutation_repetitions}), flush=True)
    scores = [score for score in outcomes if score is not None]
    return {"method": "group_label_within_task_coverage_full_purged_loto",
            "unit": "participant_or_trial_group",
            "stratum": "observed_task_coverage",
            "requested": protocol.permutation_repetitions,
            "workers": protocol.permutation_workers,
            "successful": len(scores), "failed": protocol.permutation_repetitions - len(scores),
            "p_value": (1 + sum(score >= observed for score in scores)) / (1 + len(scores)),
            "null_mean": float(np.mean(scores)) if scores else None}


def ablations(records: Sequence[Mapping[str, Any]], baseline: float, seed: int,
              calibration_splits: int = DEFAULT_CALIBRATION_SPLITS,
              calibration_bins: int = DEFAULT_CALIBRATION_BINS) -> dict:
    def score(names: Sequence[str]) -> dict[str, Any]:
        result = evaluate_folds(records, loto_folds(records), names=names, seed=seed,
                                calibration_splits=calibration_splits,
                                calibration_bins=calibration_bins)
        value = result["aggregate"]["balanced_accuracy"] if result["aggregate"] else None
        return {"balanced_accuracy": value, "delta": value - baseline if value is not None else None,
                "failed_folds": result["failed_fold_count"]}
    return {
        "group_drop": [{"dropped": group, **score([n for n in FEATURE_ALLOWLIST if n not in names])}
                       for group, names in FEATURE_GROUPS.items()],
        "single_feature_drop": [{"dropped": removed,
                                 **score([n for n in FEATURE_ALLOWLIST if n != removed])}
                                for removed in FEATURE_ALLOWLIST],
    }


def confound_control(records: Sequence[Mapping[str, Any]], target: str, seed: int) -> dict:
    labels = np.asarray([row[target] for row in records])
    groups = np.asarray([row["group"] for row in records])
    splitter = StratifiedGroupKFold(n_splits=3, shuffle=True, random_state=seed)
    true, predicted, folds = [], [], []
    x = _matrix(records, FEATURE_ALLOWLIST)
    for fold, (train, test) in enumerate(splitter.split(x, labels, groups)):
        scaler = StandardScaler().fit(x[train])
        model = LogisticRegression(max_iter=2000, class_weight="balanced", random_state=seed)
        model.fit(scaler.transform(x[train]), labels[train])
        guess = model.predict(scaler.transform(x[test]))
        true.extend(labels[test].tolist())
        predicted.extend(guess.tolist())
        folds.append({"fold": fold, "train": len(train), "test": len(test)})
    return {"purpose": f"negative_control_for_{target}_encoding", "folds": folds,
            "macro_f1": float(f1_score(true, predicted, average="macro")),
            "balanced_accuracy": float(balanced_accuracy_score(true, predicted)),
            "labels": sorted(set(str(v) for v in true))}


def released_baseline(data_dir: Path, upstream_repo: Path | None = None) -> dict[str, Any]:
    validate_private_artifact(data_dir / "feature_vectors.json", "feature_vectors.json")
    validate_private_artifact(data_dir / "train_test_split.npz", "train_test_split.npz")
    payload = evaluate_released(repo=require_upstream_repo(upstream_repo),
                                split_path=data_dir / "train_test_split.npz",
                                feature_vectors_path=data_dir / "feature_vectors.json")
    payload["dataset"]["source"] = OSF_PROJECT_URL
    return payload


def run(records: Sequence[Mapping[str, Any]], protocol: Protocol, data_dir: Path,
        feature_path: Path | None = None, *,
        upstream_repo: Path | None = None) -> dict[str, Any]:
    if feature_path is None:
        feature_path = features_path(data_dir)
    splits, bins = protocol.calibration_splits, protocol.calibration_bins
    task_fold_plan = loto_folds(records)
    agent_fold_plan = family_folds(records, protocol)
    primary = evaluate_folds(records, task_fold_plan, seed=protocol.seed,
                             calibration_splits=splits, calibration_bins=bins)
    if primary["aggregate"] is None:
        raise RuntimeError("All primary LOTO folds failed")
    family = evaluate_folds(records, agent_fold_plan, seed=protocol.seed,
                            calibration_splits=splits, calibration_bins=bins)
    secondary = evaluate_folds(records, task_fold_plan, kind="xgboost", seed=protocol.seed,
                               calibration_splits=splits, calibration_bins=bins)
    def without_rows(result: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in result.items() if key != "predictions"}

    return {
        "schema_version": "fp-agent-retrospective-v1", "status": "complete",
        "generated_at": datetime.now(timezone.utc).isoformat(), "protocol": asdict(protocol),
        "execution_environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "operating_system": platform.system(),
            "operating_system_release": platform.release(),
            "machine": platform.machine(),
            "packages": {
                name: importlib.metadata.version(name)
                for name in ("ijson", "numpy", "scikit-learn", "scipy", "xgboost")
            },
            "requirements_lock_sha256": sha256(REQUIREMENTS_LOCK),
            "isolated_python": sys.prefix != sys.base_prefix,
        },
        "sampling_ledger": {"dataset_sha256": sha256(feature_path),
                            "seed": protocol.seed, "rows": len(records),
                            "counts_by_family": dict(sorted(Counter(r["family"] for r in records).items())),
                            "counts_by_task": dict(sorted(Counter(r["task"] for r in records).items()))},
        "split_audit": split_audit(records, task_fold_plan, agent_fold_plan),
        "headline_scope": "raw-derived 23-feature FP-Agent corpus only",
        "released_80_20_reproduction": released_baseline(data_dir, upstream_repo),
        "primary_3_fold_loto_logistic": without_rows(primary),
        "primary_group_bootstrap": group_bootstrap(primary["predictions"],
                                                   protocol.bootstrap_repetitions, protocol.seed,
                                                   bins),
        "secondary_3_fold_loto_xgboost": without_rows(secondary),
        "seven_fold_agent_family_logistic": without_rows(family),
        "task_confound": confound_control(records, "task", protocol.seed),
        "source_confound": confound_control([r for r in records if r["label"] == 1], "family", protocol.seed),
        "permutation": permutation_test(records, primary["aggregate"]["balanced_accuracy"], protocol),
        "ablations": ablations(records, primary["aggregate"]["balanced_accuracy"], protocol.seed,
                               calibration_splits=splits, calibration_bins=bins),
        "auxiliary_datasets": {"status": "not_combined", "headline_inclusion": False,
                               "reason": "No separately licensed fixed-sample auxiliary cache was supplied."},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", "--data-dir", dest="data_root", type=Path, default=None,
                        help="private FP-Agent data root (default: $CUA_PRIVATE_ROOT/fp_agent)")
    parser.add_argument("--upstream-repo", type=Path, default=None,
                        help="FP-Agent checkout for the released-model baseline "
                             "(default: $CUA_FP_AGENT_UPSTREAM_REPO)")
    parser.add_argument("--features", type=Path, default=None,
                        help="private 23-feature cache (default: <data-root>/features_23.jsonl)")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="directory for the aggregate result and status "
                             "(default: <data-root>/outputs); research/evidence is refused")
    parser.add_argument("--output", type=Path, default=None,
                        help=f"result JSON (default: <output-dir>/{RESULT_FILENAME})")
    parser.add_argument("--status", type=Path, default=None,
                        help=f"status JSON (default: <output-dir>/{STATUS_FILENAME})")
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--permutations", type=int, default=1000)
    parser.add_argument("--permutation-workers", type=int, default=4)
    parser.add_argument("--calibration-splits", type=int, default=DEFAULT_CALIBRATION_SPLITS,
                        help="group-disjoint folds for the nested Platt calibration")
    parser.add_argument("--calibration-bins", type=int, default=DEFAULT_CALIBRATION_BINS,
                        help="uniform bins for ECE and the calibration curve")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        data_root = require_data_root(args.data_root)
        output_dir = args.output_dir or default_output_dir(data_root)
        output = refuse_evidence_output(args.output or output_dir / RESULT_FILENAME)
        status_path = refuse_evidence_output(args.status or output_dir / STATUS_FILENAME)
    except PrivatePathError as error:
        parser.error(str(error))
    feature_file = (args.features or features_path(data_root)).resolve()
    protocol = Protocol(bootstrap_repetitions=args.bootstrap,
                        permutation_repetitions=args.permutations,
                        permutation_workers=args.permutation_workers,
                        calibration_bins=args.calibration_bins,
                        calibration_splits=args.calibration_splits)
    output.parent.mkdir(parents=True, exist_ok=True)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status: dict[str, Any]
    try:
        payload = run(load_records(feature_file), protocol, data_root, feature_file,
                      upstream_repo=args.upstream_repo)
        output.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        status = {"status": "complete", "output": output.name,
                  "output_sha256": sha256(output)}
    except Exception as exc:
        status = {"status": "blocked", "error_type": type(exc).__name__, "error": str(exc),
                  "required_action": "complete and verify raw extraction; do not substitute released features",
                  "resource_state": {
                      "raw_dataset_complete": (data_root / "raw_dataset.json").is_file(),
                      "recoverable_partial_present": (data_root / "raw_dataset.json.part").is_file(),
                      "raw_expected_bytes": 4256528751,
                      "observed_single_connection_rate_kib_s": 21.6,
                      "estimated_transfer_hours_at_observed_rate": 53.5,
                      "released_split_present": (data_root / "train_test_split.npz").is_file(),
                      "headline_metrics_available": False,
                  }}
    status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(status, indent=2))
    if status["status"] != "complete":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
