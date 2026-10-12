"""Re-evaluate FP-Agent's released classifiers on its released test split.

This is an inference-only reproduction: it loads the three all-class XGBoost
models committed in the official FP-Agent repository and evaluates them on the
released OSF train/test split.  It intentionally does not retrain the models.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import time
from datetime import datetime, timezone
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
from research.fp_agent.retrospective_features import validate_private_artifact

if TYPE_CHECKING:
    import numpy as np

OUTPUT_FILENAME = "fp_agent_classifier_eval.json"

MODEL_SPECS = {
    "browser": {
        "feature_key": "fpjs",
        "model": "browser_fingerprint_all_classes.json",
    },
    "behavioral": {
        "feature_key": "behavioral",
        "model": "behavioral_fingerprint_all_classes.json",
    },
    "combined": {
        "feature_key": "combined",
        "model": "combined_fingerprint_all_classes.json",
    },
}


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


def feature_matrix(records: np.ndarray, feature_key: str) -> np.ndarray:
    import numpy as np

    if feature_key == "combined":
        return np.asarray(
            [record["fpjs"] + record["behavioral"] for record in records],
            dtype=float,
        )
    return np.asarray([record[feature_key] for record in records], dtype=float)


def rounded(value: float) -> float:
    return round(float(value), 10)


def evaluate(
    *,
    repo: Path,
    split_path: Path,
    feature_vectors_path: Path,
) -> dict[str, Any]:
    # The numerical stack (the ``research`` extra) is imported lazily so that
    # importing this module, e.g. from the retrospective driver or a test, never
    # requires xgboost.
    import numpy as np
    import xgboost as xgb
    from sklearn.metrics import (
        accuracy_score,
        confusion_matrix,
        precision_recall_fscore_support,
    )

    # The released split stores per-row dicts as object arrays, so loading it
    # needs allow_pickle. Unpickling is only safe for the exact released bytes:
    # both files are verified against the tracked byte counts and SHA-256 values
    # in raw_artifacts_manifest.json before anything is loaded.
    validate_private_artifact(split_path, "train_test_split.npz")
    validate_private_artifact(feature_vectors_path, "feature_vectors.json")
    split = np.load(split_path, allow_pickle=True)
    x_train_records = split["X_train"]
    x_test_records = split["X_test"]
    y_train = split["y_train"].astype(int)
    y_test = split["y_test"].astype(int)

    # AgentClassificationDataset.create_label_mapping() sorts class names.
    raw_dataset = json.loads(feature_vectors_path.read_text(encoding="utf-8"))
    class_names = sorted(raw_dataset.keys())

    results: dict[str, dict[str, Any]] = {}
    classifiers_dir = repo / "classifier_training" / "classifiers"
    for name, spec in MODEL_SPECS.items():
        model_path = classifiers_dir / spec["model"]
        model = xgb.XGBClassifier(importance_type="total_gain")
        model.load_model(model_path)

        x_train = feature_matrix(x_train_records, spec["feature_key"])
        x_test = feature_matrix(x_test_records, spec["feature_key"])
        y_pred = model.predict(x_test).astype(int)

        macro_precision, macro_recall, macro_f1, _ = precision_recall_fscore_support(
            y_test,
            y_pred,
            average="macro",
            zero_division=0,
        )
        per_class_precision, per_class_recall, per_class_f1, support = (
            precision_recall_fscore_support(
                y_test,
                y_pred,
                labels=list(range(len(class_names))),
                zero_division=0,
            )
        )
        matrix = confusion_matrix(
            y_test,
            y_pred,
            labels=list(range(len(class_names))),
        )

        results[name] = {
            "model_file": str(model_path.relative_to(repo)).replace("\\", "/"),
            "model_sha256": sha256(model_path),
            "feature_dimensions": int(x_test.shape[1]),
            "train_examples": int(x_train.shape[0]),
            "test_examples": int(x_test.shape[0]),
            "accuracy": rounded(accuracy_score(y_test, y_pred)),
            "macro_precision": rounded(macro_precision),
            "macro_recall": rounded(macro_recall),
            "macro_f1": rounded(macro_f1),
            "errors": int(np.count_nonzero(y_pred != y_test)),
            "per_class": {
                class_name: {
                    "precision": rounded(per_class_precision[index]),
                    "recall": rounded(per_class_recall[index]),
                    "f1": rounded(per_class_f1[index]),
                    "support": int(support[index]),
                }
                for index, class_name in enumerate(class_names)
            },
            "confusion_matrix": matrix.tolist(),
        }

    return {
        "experiment": "FP-Agent released-model inference reproduction",
        "scope": (
            "Inference-only evaluation of the three released all-class "
            "classifiers on the released random test split; no retraining."
        ),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "repository": UPSTREAM_REPOSITORY_URL,
        "repository_commit": git_head(repo),
        "dataset": {
            # The public project URL only; the share link used to download the
            # files is never written into an output.
            "source": OSF_PROJECT_URL,
            "feature_vectors_file": feature_vectors_path.name,
            "feature_vectors_sha256": sha256(feature_vectors_path),
            "split_file": split_path.name,
            "split_sha256": sha256(split_path),
            "train_examples": len(y_train),
            "validation_examples": len(split["y_val"]),
            "test_examples": len(y_test),
            "classes": class_names,
            "class_mapping": {class_name: index for index, class_name in enumerate(class_names)},
        },
        "results": results,
        "interpretation_limit": (
            "Scores quantify re-execution on the released within-corpus split. "
            "They do not measure out-of-distribution or future-agent generalization."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream-repo",
        "--repo",
        dest="upstream_repo",
        type=Path,
        default=None,
        help="FP-Agent git checkout (default: $CUA_FP_AGENT_UPSTREAM_REPO)",
    )
    parser.add_argument(
        "--data-root",
        "--data-dir",
        dest="data_root",
        type=Path,
        default=None,
        help="directory holding train_test_split.npz and feature_vectors.json "
        "(default: $CUA_PRIVATE_ROOT/fp_agent)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"result JSON (default: <data-root>/outputs/{OUTPUT_FILENAME}); "
        "paths under research/evidence are refused",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        data_root = require_data_root(args.data_root)
        repo = require_upstream_repo(args.upstream_repo)
        output = refuse_evidence_output(
            args.output or default_output_dir(data_root) / OUTPUT_FILENAME
        )
    except PrivatePathError as error:
        parser.error(str(error))

    started_at = datetime.now(timezone.utc)
    started_clock = time.perf_counter()
    payload = evaluate(
        repo=repo,
        split_path=data_root / "train_test_split.npz",
        feature_vectors_path=data_root / "feature_vectors.json",
    )
    completed_at = datetime.now(timezone.utc)
    payload.update(
        {
            "schema_version": "1.0",
            "run_id": started_at.strftime("%Y%m%dT%H%M%SZ"),
            "system": "fp-agent",
            "repo": payload["repository"],
            "repo_commit": payload["repository_commit"],
            "package_versions": {
                "python": platform.python_version(),
                "numpy": importlib.metadata.version("numpy"),
                "scikit-learn": importlib.metadata.version("scikit-learn"),
                "xgboost": importlib.metadata.version("xgboost"),
            },
            "model": "three released all-class XGBoost classifiers",
            "task": "released-model inference on the released 1,544-example test split",
            "repetition": 1,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": round(time.perf_counter() - started_clock, 3),
            "steps": None,
            "validator": (
                "exact released split, model/data SHA-256 provenance, "
                "and deterministic metric recomputation"
            ),
            "success": True,
            "error_category": None,
            "reported_cost_usd": 0.0,
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload["results"], indent=2))
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
