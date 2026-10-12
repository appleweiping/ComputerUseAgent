"""Leave-one-agent-family-out evaluation on FP-Agent's released features.

The published FP-Agent split is an in-distribution, multi-class split.  This
script asks a different and deliberately narrower question: if one named agent
family is absent during training, does a binary human-vs-agent classifier still
recognize that family as agent-generated?

No source identifiers or row-level predictions are written to the outputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import platform
import statistics
import subprocess
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from research.fp_agent.paths import (
    OSF_PROJECT_URL,
    UPSTREAM_REPOSITORY_URL,
    PrivatePathError,
    default_output_dir,
    refuse_evidence_output,
    require_data_root,
    require_upstream_repo,
)

if TYPE_CHECKING:
    import numpy as np

# numpy, scikit-learn and xgboost (the ``research`` extra) are imported inside
# the functions that need them, so the pure data-handling helpers below (and
# their tests) work without the numerical stack.

DATA_FILENAME = "feature_vectors.json"
JSON_FILENAME = "fp_agent_loafo.json"
CSV_FILENAME = "fp_agent_loafo.csv"

EXPECTED_DATA_SHA256 = (
    "54aa9eb6e74a509b86a3504f97e1f2c6fee99467c1ac935b92a5385e9e1cf641"
)
EXPECTED_REPO_COMMIT = "e614e2f0f935211e22d856195d56e91fa88a7b16"
PROTOCOL_VERSION = "fp-agent-loafo-v1"
HUMAN_LABEL = "Human"
FEATURE_SETS = ("browser", "behavioral", "combined")
FEATURE_KEYS = {"browser": "fpjs", "behavioral": "behavioral"}
TASK_PREFIXES = ("Flight-booking", "Forums", "Shopping")


@dataclass(frozen=True)
class Record:
    family: str
    task: str
    group: str | None
    browser: tuple[float, ...]
    behavioral: tuple[float, ...]
    combined_digest: str

    def vector(self, feature_set: str) -> tuple[float, ...]:
        if feature_set == "browser":
            return self.browser
        if feature_set == "behavioral":
            return self.behavioral
        if feature_set == "combined":
            return self.browser + self.behavioral
        raise ValueError(f"Unknown feature set: {feature_set}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_head(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
        encoding="utf-8",
    ).strip()


def _task_prefix(task_name: str) -> str:
    for prefix in TASK_PREFIXES:
        if task_name.startswith(prefix):
            return prefix
    raise ValueError(f"Unrecognized task prefix: {task_name[:80]!r}")


def _human_group(task_name: str) -> str:
    """Return the public dataset's stable, opaque human participant token."""

    pieces = task_name.split()
    if len(pieces) < 2 or pieces[0] not in TASK_PREFIXES:
        raise ValueError("Human task metadata lacks an opaque participant group")
    return pieces[1]


def _vector_digest(browser: Sequence[float], behavioral: Sequence[float]) -> str:
    canonical = json.dumps(
        [list(browser), list(behavioral)],
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_records(path: Path) -> tuple[dict[str, list[Record]], dict[str, Any]]:
    """Load and validate released feature vectors.

    Exact duplicate *combined* vectors within a family are collapsed to one
    deterministic record.  This prevents repeated identical rows from
    receiving extra weight.  Counts before and after this step are retained in
    the data-quality summary.
    """

    raw = json.loads(path.read_text(encoding="utf-8"))
    if HUMAN_LABEL not in raw:
        raise ValueError("Dataset has no Human class")
    agent_families = sorted(label for label in raw if label != HUMAN_LABEL)
    if len(agent_families) < 2:
        raise ValueError("LOAFO requires at least two agent families")

    records: dict[str, list[Record]] = {}
    source_counts: dict[str, int] = {}
    duplicate_counts: dict[str, int] = {}
    task_counts: dict[str, dict[str, int]] = {}
    source_labels_by_family: dict[str, dict[str, int]] = {}
    dimensions: dict[str, set[int]] = {"browser": set(), "behavioral": set()}
    nonfinite_values = 0
    digest_to_families: dict[str, set[str]] = {}

    for family in sorted(raw):
        family_records: list[Record] = []
        seen: set[str] = set()
        source_counts[family] = len(raw[family])
        duplicate_counts[family] = 0
        tasks: Counter[str] = Counter()
        source_labels: Counter[str] = Counter()
        for source_json, payload in sorted(raw[family].items()):
            source = json.loads(source_json)
            # The released top-level taxonomy intentionally normalizes several
            # raw product names (for example, "ChatGPT" -> "ChatGPT Agent").
            # Retain that mapping for QA instead of incorrectly treating it as
            # label corruption.
            source_labels[str(source.get("class_label", ""))] += 1
            task = _task_prefix(str(source.get("task_name", "")))
            browser = tuple(float(value) for value in payload["fpjs"])
            behavioral = tuple(float(value) for value in payload["behavioral"])
            dimensions["browser"].add(len(browser))
            dimensions["behavioral"].add(len(behavioral))
            nonfinite_values += sum(
                not math.isfinite(value) for value in browser + behavioral
            )
            digest = _vector_digest(browser, behavioral)
            digest_to_families.setdefault(digest, set()).add(family)
            if digest in seen:
                duplicate_counts[family] += 1
                continue
            seen.add(digest)
            tasks[task] += 1
            family_records.append(
                Record(
                    family=family,
                    task=task,
                    group=(
                        _human_group(source["task_name"])
                        if family == HUMAN_LABEL
                        else None
                    ),
                    browser=browser,
                    behavioral=behavioral,
                    combined_digest=digest,
                )
            )
        records[family] = family_records
        task_counts[family] = dict(sorted(tasks.items()))
        source_labels_by_family[family] = dict(sorted(source_labels.items()))

    if dimensions["browser"] != {418} or dimensions["behavioral"] != {50}:
        raise ValueError(f"Unexpected feature dimensions: {dimensions}")
    if nonfinite_values:
        raise ValueError(f"Found {nonfinite_values} non-finite feature values")

    cross_family_duplicate_vectors = sum(
        len(families) > 1 for families in digest_to_families.values()
    )
    quality = {
        "source_rows": sum(source_counts.values()),
        "deduplicated_rows": sum(len(items) for items in records.values()),
        "source_rows_by_family": source_counts,
        "exact_combined_duplicates_removed_by_family": duplicate_counts,
        "cross_family_exact_combined_duplicate_vectors": (
            cross_family_duplicate_vectors
        ),
        "feature_dimensions": {"browser": 418, "behavioral": 50, "combined": 468},
        "nonfinite_feature_values": nonfinite_values,
        "task_counts_after_deduplication": task_counts,
        "raw_source_labels_by_normalized_family": source_labels_by_family,
    }
    return records, quality


def split_humans(
    records: Sequence[Record], *, seed: int, test_group_fraction: float
) -> tuple[list[Record], list[Record], dict[str, Any]]:
    """Split humans by participant group using a stable seeded hash ordering."""

    groups = sorted({record.group for record in records if record.group is not None})
    if len(groups) < 2:
        raise ValueError("At least two human participant groups are required")
    if not 0.0 < test_group_fraction < 1.0:
        raise ValueError("test_group_fraction must be between zero and one")
    ranked = sorted(
        groups,
        key=lambda group: hashlib.sha256(f"{seed}:{group}".encode("utf-8")).hexdigest(),
    )
    test_group_count = max(1, min(len(groups) - 1, math.ceil(len(groups) * test_group_fraction)))
    test_groups = set(ranked[:test_group_count])
    train = [record for record in records if record.group not in test_groups]
    test = [record for record in records if record.group in test_groups]
    train_groups = {record.group for record in train}
    observed_test_groups = {record.group for record in test}
    if train_groups & observed_test_groups:
        raise AssertionError("Human participant group leakage")
    if set(record.task for record in test) != set(TASK_PREFIXES):
        raise ValueError("Human test split does not cover all three tasks")

    split_summary = {
        "method": "stable seeded hash split of opaque participant groups",
        "test_group_fraction_requested": test_group_fraction,
        "participant_groups_total": len(groups),
        "participant_groups_train": len(train_groups),
        "participant_groups_test": len(observed_test_groups),
        "group_overlap": 0,
        "rows_train": len(train),
        "rows_test": len(test),
        "task_counts_train": dict(sorted(Counter(x.task for x in train).items())),
        "task_counts_test": dict(sorted(Counter(x.task for x in test).items())),
    }
    return train, test, split_summary


def _matrix(records: Iterable[Record], feature_set: str) -> np.ndarray:
    import numpy as np

    return np.asarray([record.vector(feature_set) for record in records], dtype=np.float32)


def _balanced_sample_weights(labels: Sequence[int]) -> list[float]:
    """Inverse-frequency weights for 0/1 labels; each class gets equal total weight."""

    values = [int(label) for label in labels]
    if any(label not in (0, 1) for label in values):
        raise ValueError("Labels must be 0 (human) or 1 (agent)")
    counts = (values.count(0), values.count(1))
    if 0 in counts:
        raise ValueError("Training fold must contain both human and agent labels")
    total = len(values)
    return [total / (2.0 * counts[label]) for label in values]


def model_parameters(seed: int) -> dict[str, Any]:
    return {
        "objective": "binary:logistic",
        "n_estimators": 200,
        "max_depth": 4,
        "learning_rate": 0.05,
        "min_child_weight": 1.0,
        "subsample": 1.0,
        "colsample_bytree": 1.0,
        "reg_alpha": 0.0,
        "reg_lambda": 1.0,
        "eval_metric": "logloss",
        "tree_method": "hist",
        "random_state": seed,
        "n_jobs": 1,
        "verbosity": 0,
    }


def _rounded(value: float) -> float:
    return round(float(value), 10)


def _fold_metrics(
    y_true: np.ndarray, y_pred: np.ndarray, probabilities: np.ndarray
) -> dict[str, Any]:
    from sklearn.metrics import (
        accuracy_score,
        average_precision_score,
        balanced_accuracy_score,
        confusion_matrix,
        precision_recall_fscore_support,
        roc_auc_score,
    )

    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=[0, 1], zero_division=0
    )
    return {
        "confusion_matrix_human_agent": [[int(tn), int(fp)], [int(fn), int(tp)]],
        "accuracy": _rounded(accuracy_score(y_true, y_pred)),
        "balanced_accuracy": _rounded(balanced_accuracy_score(y_true, y_pred)),
        "macro_f1": _rounded(statistics.mean(float(value) for value in f1)),
        "roc_auc": _rounded(roc_auc_score(y_true, probabilities)),
        "average_precision": _rounded(average_precision_score(y_true, probabilities)),
        "human_specificity": _rounded(tn / (tn + fp)),
        "human_false_positive_rate": _rounded(fp / (tn + fp)),
        "heldout_agent_recall": _rounded(tp / (tp + fn)),
        "per_class": {
            "human": {
                "precision": _rounded(precision[0]),
                "recall": _rounded(recall[0]),
                "f1": _rounded(f1[0]),
                "support": int(support[0]),
            },
            "agent": {
                "precision": _rounded(precision[1]),
                "recall": _rounded(recall[1]),
                "f1": _rounded(f1[1]),
                "support": int(support[1]),
            },
        },
    }


def run_fold(
    *,
    records: dict[str, list[Record]],
    human_train: Sequence[Record],
    human_test: Sequence[Record],
    heldout_family: str,
    feature_set: str,
    seed: int,
) -> dict[str, Any]:
    import numpy as np
    import xgboost as xgb

    train_agents = [
        record
        for family in sorted(records)
        if family not in (HUMAN_LABEL, heldout_family)
        for record in records[family]
    ]
    heldout_agents = records[heldout_family]
    train_records = list(human_train) + train_agents
    test_records = list(human_test) + heldout_agents
    y_train = np.asarray([0] * len(human_train) + [1] * len(train_agents), dtype=int)
    y_test = np.asarray([0] * len(human_test) + [1] * len(heldout_agents), dtype=int)

    classifier = xgb.XGBClassifier(**model_parameters(seed))
    classifier.fit(
        _matrix(train_records, feature_set),
        y_train,
        sample_weight=np.asarray(_balanced_sample_weights(y_train.tolist()), dtype=float),
    )
    probabilities = classifier.predict_proba(_matrix(test_records, feature_set))[:, 1]
    predictions = (probabilities >= 0.5).astype(int)
    metrics = _fold_metrics(y_test, predictions, probabilities)

    by_task: dict[str, dict[str, Any]] = {}
    for task in TASK_PREFIXES:
        mask = np.asarray([record.task == task for record in test_records])
        task_true = y_test[mask]
        task_pred = predictions[mask]
        task_prob = probabilities[mask]
        by_task[task] = _fold_metrics(task_true, task_pred, task_prob)

    return {
        "feature_set": feature_set,
        "heldout_agent_family": heldout_family,
        "training_agent_families": [
            family
            for family in sorted(records)
            if family not in (HUMAN_LABEL, heldout_family)
        ],
        "train_examples": {
            "human": len(human_train),
            "agent": len(train_agents),
            "total": len(train_records),
        },
        "test_examples": {
            "human": len(human_test),
            "heldout_agent": len(heldout_agents),
            "total": len(test_records),
        },
        "metrics": metrics,
        "by_task": by_task,
    }


def summarize_folds(folds: Sequence[dict[str, Any]]) -> dict[str, Any]:
    metrics = (
        "balanced_accuracy",
        "macro_f1",
        "roc_auc",
        "human_specificity",
        "human_false_positive_rate",
        "heldout_agent_recall",
    )
    summary: dict[str, dict[str, Any]] = {}
    for feature_set in FEATURE_SETS:
        selected = [fold for fold in folds if fold["feature_set"] == feature_set]
        feature_summary: dict[str, dict[str, Any]] = {}
        for metric in metrics:
            values = [fold["metrics"][metric] for fold in selected]
            feature_summary[metric] = {
                "mean": _rounded(statistics.mean(values)),
                "median": _rounded(statistics.median(values)),
                "minimum": _rounded(min(values)),
                "maximum": _rounded(max(values)),
            }
        summary[feature_set] = feature_summary
    return summary


def build_payload(
    *, data_path: Path, repo: Path, seed: int, test_group_fraction: float
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data_digest = sha256(data_path)
    if data_digest != EXPECTED_DATA_SHA256:
        raise ValueError(
            "Feature-vector SHA-256 does not match the released file: "
            f"{data_digest}"
        )
    repo_commit = git_head(repo)
    if repo_commit != EXPECTED_REPO_COMMIT:
        raise ValueError(
            f"FP-Agent checkout is {repo_commit}, expected {EXPECTED_REPO_COMMIT}"
        )

    records, quality = load_records(data_path)
    human_train, human_test, human_split = split_humans(
        records[HUMAN_LABEL],
        seed=seed,
        test_group_fraction=test_group_fraction,
    )
    families = sorted(family for family in records if family != HUMAN_LABEL)
    folds = [
        run_fold(
            records=records,
            human_train=human_train,
            human_test=human_test,
            heldout_family=family,
            feature_set=feature_set,
            seed=seed,
        )
        for feature_set in FEATURE_SETS
        for family in families
    ]

    payload = {
        "schema_version": "1.0",
        "protocol": PROTOCOL_VERSION,
        "question": (
            "Can a binary classifier trained on Human plus six known agent "
            "families identify the seventh, unseen family as agent-generated?"
        ),
        "status": "completed",
        "source": {
            "repository": UPSTREAM_REPOSITORY_URL,
            "repository_commit": repo_commit,
            "feature_vectors_source": OSF_PROJECT_URL,
            "feature_vectors_file": data_path.name,
            "feature_vectors_sha256": data_digest,
        },
        "protocol_details": {
            "target": "agent=1, human=0",
            "agent_split": "leave one named agent family out in each fold",
            "human_split": (
                "one fixed 80/20-style split by opaque participant group; "
                "the same human test set is reused only to make family folds comparable"
            ),
            "human_test_group_fraction": test_group_fraction,
            "decision_threshold": 0.5,
            "training_balance": "inverse-frequency sample weights; mean weight is 1",
            "duplicate_handling": (
                "collapse exact duplicate 468-dimensional combined vectors within "
                "each family before every feature-set evaluation"
            ),
            "feature_sets": {
                "browser": "418 released browser-fingerprint features",
                "behavioral": "50 released interaction-behavior features",
                "combined": "browser followed by behavioral (468 features)",
            },
            "seed": seed,
            "model": "binary XGBoost classifier trained from scratch per fold",
            "model_parameters": model_parameters(seed),
            "primary_metrics": [
                "heldout_agent_recall",
                "human_specificity",
                "balanced_accuracy",
            ],
        },
        "data_quality": quality,
        "human_split_summary": human_split,
        "folds": folds,
        "macro_across_heldout_families": summarize_folds(folds),
        "limitations": [
            (
                "This is a retrospective family holdout inside one public corpus, "
                "not a prospective test on future agents or new collection environments."
            ),
            (
                "Agent family is confounded with its software, browser/OS setup, model, "
                "and collection period; browser-fingerprint results can exploit those "
                "environment differences rather than interaction style."
            ),
            (
                "The corpus contains three website tasks. This protocol holds out an "
                "agent family, not a task; task transfer is not established."
            ),
            (
                "The fixed human test set is reused across seven family folds. Macro "
                "summaries average family-level point estimates and are descriptive, "
                "not seven independent estimates of human performance."
            ),
            (
                "Rows are repeated task attempts, not an independently sampled population. "
                "No population-level deployment claim or uncertainty interval is made."
            ),
        ],
        "package_versions": {
            "python": platform.python_version(),
            "numpy": importlib.metadata.version("numpy"),
            "scikit-learn": importlib.metadata.version("scikit-learn"),
            "xgboost": importlib.metadata.version("xgboost"),
        },
        "determinism": (
            "Rows, families, and output keys use stable ordering; the model uses one "
            "thread and a fixed seed; runtime timestamps are intentionally omitted."
        ),
    }
    return payload, folds


def preflight(
    *, data_path: Path, repo: Path, seed: int, test_group_fraction: float
) -> dict[str, Any]:
    if not data_path.is_file():
        raise FileNotFoundError(f"Missing feature vectors: {data_path}")
    if not (repo / ".git").exists():
        raise FileNotFoundError(f"Missing FP-Agent git checkout: {repo}")
    data_digest = sha256(data_path)
    records, quality = load_records(data_path)
    _, _, human_split = split_humans(
        records[HUMAN_LABEL], seed=seed, test_group_fraction=test_group_fraction
    )
    repo_commit = git_head(repo)
    return {
        "status": "ready" if (
            data_digest == EXPECTED_DATA_SHA256 and repo_commit == EXPECTED_REPO_COMMIT
        ) else "blocked",
        "data_sha256": data_digest,
        "data_sha256_matches": data_digest == EXPECTED_DATA_SHA256,
        "repo_commit": repo_commit,
        "repo_commit_matches": repo_commit == EXPECTED_REPO_COMMIT,
        "agent_families": sorted(f for f in records if f != HUMAN_LABEL),
        "data_quality": quality,
        "human_split_summary": human_split,
    }


def write_csv(path: Path, folds: Sequence[dict[str, Any]]) -> None:
    columns = [
        "feature_set",
        "heldout_agent_family",
        "training_agent_examples",
        "human_train_examples",
        "heldout_agent_test_examples",
        "human_test_examples",
        "balanced_accuracy",
        "macro_f1",
        "roc_auc",
        "average_precision",
        "heldout_agent_recall",
        "human_specificity",
        "human_false_positive_rate",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for fold in folds:
            metrics = fold["metrics"]
            writer.writerow(
                {
                    "feature_set": fold["feature_set"],
                    "heldout_agent_family": fold["heldout_agent_family"],
                    "training_agent_examples": fold["train_examples"]["agent"],
                    "human_train_examples": fold["train_examples"]["human"],
                    "heldout_agent_test_examples": fold["test_examples"]["heldout_agent"],
                    "human_test_examples": fold["test_examples"]["human"],
                    "balanced_accuracy": metrics["balanced_accuracy"],
                    "macro_f1": metrics["macro_f1"],
                    "roc_auc": metrics["roc_auc"],
                    "average_precision": metrics["average_precision"],
                    "heldout_agent_recall": metrics["heldout_agent_recall"],
                    "human_specificity": metrics["human_specificity"],
                    "human_false_positive_rate": metrics["human_false_positive_rate"],
                }
            )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write deterministic UTF-8 JSON with LF endings on every platform."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help=f"private FP-Agent data root holding {DATA_FILENAME} "
        "(default: $CUA_PRIVATE_ROOT/fp_agent)",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help=f"explicit released feature-vector file (default: <data-root>/{DATA_FILENAME})",
    )
    parser.add_argument(
        "--upstream-repo",
        "--repo",
        dest="upstream_repo",
        type=Path,
        default=None,
        help="FP-Agent git checkout (default: $CUA_FP_AGENT_UPSTREAM_REPO)",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help=f"default: <data-root>/outputs/{JSON_FILENAME}; research/evidence is refused",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help=f"default: <data-root>/outputs/{CSV_FILENAME}; research/evidence is refused",
    )
    parser.add_argument("--seed", type=int, default=20260808)
    parser.add_argument("--human-test-group-fraction", type=float, default=0.2)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.data is not None and args.data_root is None:
            data_path = args.data.resolve()
            data_root = require_data_root(data_path.parent)
        else:
            data_root = require_data_root(args.data_root)
            data_path = (args.data or data_root / DATA_FILENAME).resolve()
        repo = require_upstream_repo(args.upstream_repo)
        outputs = default_output_dir(data_root)
        output_json = refuse_evidence_output(args.output_json or outputs / JSON_FILENAME)
        output_csv = refuse_evidence_output(args.output_csv or outputs / CSV_FILENAME)
    except PrivatePathError as error:
        parser.error(str(error))
    if args.dry_run:
        print(
            json.dumps(
                preflight(
                    data_path=data_path,
                    repo=repo,
                    seed=args.seed,
                    test_group_fraction=args.human_test_group_fraction,
                ),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return

    payload, folds = build_payload(
        data_path=data_path,
        repo=repo,
        seed=args.seed,
        test_group_fraction=args.human_test_group_fraction,
    )
    write_json(output_json, payload)
    write_csv(output_csv, folds)
    print(json.dumps(payload["macro_across_heldout_families"], indent=2))
    print(f"Wrote {output_json}")
    print(f"Wrote {output_csv}")


if __name__ == "__main__":
    main()
