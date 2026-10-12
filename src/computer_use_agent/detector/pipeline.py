"""Run extract-ready records through fit, calibrate, evaluate, and explain.

Datasets that carry ``provenance`` are accepted only through a validator
registered for their ``source_stage`` (see
:mod:`computer_use_agent.detector.provenance`); everything else is validated
by the closed v1/v2 contract. Formal human data are denied unless the
registered stage also supplies a ``human_data_policy`` hook that authorizes
them; accepting provenance never authorizes human-research use by itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from computer_use_agent.study.policy import DENY_FORMAL_HUMAN_DATA, HumanDataPolicy

from .bundle import verify_bundle, write_bundle
from .contract import (
    DATASET_SCHEMA_V1,
    DATASET_SCHEMA_V2,
    DATASET_SCHEMAS,
    FEATURE_ALLOWLIST,
    RECORD_SCHEMA_V1,
    canonical_json,
    validate_dataset,
)
from .evaluation import (
    EVALUATION_PROFILES,
    EvaluationConfig,
    ablation_analysis,
    class_and_task_counts,
    evaluate_loto,
    evaluation_config_for_profile,
    permutation_test,
    task_cluster_bootstrap,
    task_identification_control,
)
from .provenance import (
    ProvenanceError,
    ProvenanceRegistration,
    generic_analysis_provenance,
    load_provenance_modules,
    require_provenance_registration,
    validate_dataset_provenance,
)
from .reporting import (
    RESULT_SCHEMA,
    build_trajectory_projection,
    detector_descriptive_summary,
    trajectory_artifact_binding,
    validate_reporting_contract,
)

DATASET_ROOT_FIELDS = frozenset(
    {"schema_version", "records", "audit", "provenance", "source_manifest"}
)


class _StructuralAuditOnly:
    """Computes the structural audit that is then handed to a stage validator."""

    def authorize(self, *, use: str, participant_scope: str) -> None:
        return None


def _dataset_schema_for(rows: Sequence[Mapping[str, Any]]) -> str:
    if rows and all(row.get("schema_version") == RECORD_SCHEMA_V1 for row in rows):
        return DATASET_SCHEMA_V1
    return DATASET_SCHEMA_V2


def _analysis_binding(
    registration: ProvenanceRegistration,
    payload: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    if registration.analysis_provenance is None:
        return generic_analysis_provenance(payload, audit=audit)
    built = registration.analysis_provenance(payload)
    if not isinstance(built, Mapping) or not isinstance(built.get("schema_version"), str):
        raise ProvenanceError("analysis provenance must be an object with a schema_version")
    if built.get("raw_sequences_embedded", False) is not False:
        raise ProvenanceError("analysis provenance must not embed raw event sequences")
    binding: dict[str, Any] = json.loads(canonical_json(dict(built)))
    return binding


def _provenance_context(
    rows: list[dict[str, Any]],
    provenance: Mapping[str, Any],
) -> tuple[dict[str, Any], HumanDataPolicy, dict[str, Any]]:
    """Validate provenance through the registry; return audit, policy and binding."""

    require_provenance_registration(provenance)  # fail closed before any work
    structural_audit = validate_dataset(rows, human_data_policy=_StructuralAuditOnly())
    payload = {
        "schema_version": _dataset_schema_for(rows),
        "records": rows,
        "audit": structural_audit,
        "provenance": json.loads(canonical_json(dict(provenance))),
    }
    registration = validate_dataset_provenance(payload)
    policy: HumanDataPolicy
    if registration.human_data_policy is not None:
        # Hooks get their own detached copy, like the validator.
        policy = registration.human_data_policy(json.loads(canonical_json(payload)))
    else:
        # No hook: accepting provenance does not authorize formal human data
        # (fail closed, same default as validate_dataset without a policy).
        policy = DENY_FORMAL_HUMAN_DATA
    audit = validate_dataset(rows, human_data_policy=policy)
    binding = _analysis_binding(registration, json.loads(canonical_json(payload)), audit)
    return audit, policy, binding


def run_pipeline(
    records: Sequence[Mapping[str, Any]],
    *,
    config: EvaluationConfig | None = None,
    dataset_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic analysis object; no output implies no publication."""

    config = config or EvaluationConfig()
    # Copy to detach subsequent analysis from caller mutation while preserving
    # the validated closed record representation.
    rows: list[dict[str, Any]] = json.loads(canonical_json(list(records)))
    formal = any(row.get("evidence_class") == "formal_trace" for row in rows)
    human_data_policy: HumanDataPolicy | None = None
    result_provenance: dict[str, Any] | None = None
    if dataset_provenance is not None:
        dataset_audit, human_data_policy, result_provenance = _provenance_context(
            rows, dataset_provenance
        )
    else:
        if formal:
            raise ValueError(
                "Formal detector analysis requires stage-specific immutable-ledger provenance"
            )
        dataset_audit = validate_dataset(rows)
    descriptive = detector_descriptive_summary(rows)
    trajectory_projection = build_trajectory_projection(descriptive)
    trajectory_binding = trajectory_artifact_binding(trajectory_projection)
    primary = evaluate_loto(rows, config=config, human_data_policy=human_data_policy)
    primary_score = float(primary["aggregate"]["balanced_accuracy"])
    bootstrap = task_cluster_bootstrap(primary["predictions"], config=config)
    task_control = task_identification_control(
        rows, config=config, human_data_policy=human_data_policy
    )
    permutation = permutation_test(
        rows,
        observed_balanced_accuracy=primary_score,
        feature_names=FEATURE_ALLOWLIST,
        config=config,
        human_data_policy=human_data_policy,
    )
    ablations = ablation_analysis(
        rows,
        baseline_balanced_accuracy=primary_score,
        config=config,
        human_data_policy=human_data_policy,
    )
    synthetic = "synthetic_test_fixture" in dataset_audit["evidence_classes"]
    claim_status = (
        "Synthetic pipeline check only; no empirical detector result."
        if synthetic
        else "Exploratory held-out-task probe; not population or deployment validation."
    )
    limitations = [
        "The protocol estimates transfer to the observed held-out tasks, not unseen people, "
        "environments, or agent families.",
        "Task-cluster intervals have limited resolution with only three tasks.",
        "Behavior can change with interface, model, latency, accessibility tools, and "
        "recorder modality.",
        "Framework attribution and person-level identity are outside the primary question.",
        "Actor, framework, task, URL, selector, UUID, absolute-time, software-version, "
        "validator, trust, and action-text fields are excluded from model inputs.",
    ]
    if synthetic:
        limitations.insert(
            0,
            "All rows include synthetic test fixtures; numerical metrics validate code paths "
            "only and must not be reported as study findings.",
        )
    if any("single" in scope.lower() for scope in dataset_audit["participant_scopes"]):
        limitations.insert(
            0,
            "Human observations come from a single-participant scope and cannot support "
            "population claims.",
        )
    result = {
        "schema_version": RESULT_SCHEMA,
        "pipeline_order": ["extract", "fit", "calibrate", "evaluate", "explain"],
        "claim_status": claim_status,
        "formal_claim_permitted": False,
        "source_provenance": result_provenance,
        "dataset": dataset_audit,
        "class_and_task_counts": class_and_task_counts(rows),
        "descriptive": descriptive,
        "trajectory_artifact": trajectory_binding,
        "feature_contract": {
            "allowlist": list(FEATURE_ALLOWLIST),
            "selection": "exact_name_allowlist_only",
            "provenance_is_never_implicitly_numeric": True,
        },
        "configuration": config.as_dict(),
        "fit": {
            "scope": "cross_validation_folds_only_no_deployable_full_data_model",
            "algorithm": config.as_dict()["algorithm"],
            "fold_count": primary["fold_count"],
        },
        "calibrate": {
            "method": config.as_dict()["calibration"],
            "source": "nested_out_of_task_training_predictions_only",
            "outer_test_labels_used": False,
        },
        "evaluation": {
            "primary_loto": primary,
            "task_cluster_bootstrap": bootstrap,
            "task_identification_negative_control": task_control,
            "label_permutation": permutation,
            "ablations": ablations,
        },
        "explanation": {
            "method": "mean_calibrated_standardized_fold_coefficients",
            "causal": False,
            "coefficient_summary": primary["coefficient_summary"],
        },
        "limitations": limitations,
    }
    validate_reporting_contract(result)
    return result


def load_dataset_payload(
    path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Load and fully validate a detector dataset; return its records and provenance."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Dataset root must be an object")
    if not {"schema_version", "records"}.issubset(payload) or not set(payload).issubset(
        DATASET_ROOT_FIELDS
    ):
        raise ValueError("Dataset root does not match the closed schema")
    schema_version = payload.get("schema_version")
    if schema_version not in DATASET_SCHEMAS or not isinstance(payload.get("records"), list):
        raise ValueError("Unsupported detector dataset")
    records: list[dict[str, Any]] = payload["records"]
    provenance = payload.get("provenance")
    if schema_version == DATASET_SCHEMA_V2 and provenance is None:
        from computer_use_agent.contracts.validation import validate_contract

        validate_contract(payload, schema_version=DATASET_SCHEMA_V2)
    if schema_version != DATASET_SCHEMA_V2 and "source_manifest" in payload:
        raise ValueError("Legacy detector dataset v1 cannot contain source_manifest")
    if provenance is not None:
        if not isinstance(provenance, dict):
            raise ValueError("Detector dataset provenance must be an object")
        validate_dataset_provenance(payload)
    else:
        if any(row.get("evidence_class") == "formal_trace" for row in records):
            raise ValueError(
                "Formal detector datasets require stage-specific immutable-ledger provenance"
            )
        audit = validate_dataset(records, dataset_schema=str(schema_version))
        if "audit" in payload and payload["audit"] != audit:
            raise ValueError("Stored detector dataset audit differs from revalidation")
    return records, provenance


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """Backward-compatible record loader with full provenance validation."""

    return load_dataset_payload(path)[0]


def analyze_dataset(
    dataset: Path,
    output_dir: Path,
    *,
    config: EvaluationConfig,
    provenance_modules: Sequence[str] = (),
) -> dict[str, Any]:
    """Load, analyze and write a verified bundle; return the summary printed by the CLIs."""

    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite result bundle: {output_dir}")
    load_provenance_modules(provenance_modules)
    records, provenance = load_dataset_payload(Path(dataset))
    result = run_pipeline(records, config=config, dataset_provenance=provenance)
    hashes = write_bundle(result, output_dir)
    verify_bundle(output_dir)
    return {"claim_status": result["claim_status"], "hashes": hashes}


def add_analysis_arguments(parser: argparse.ArgumentParser) -> None:
    """Arguments shared by this module's CLI and ``computer-use-agent analyze``."""

    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--profile",
        choices=sorted(EVALUATION_PROFILES),
        default="default",
        help="default: documented protocol; smoke: fast code-path check (not comparable)",
    )
    parser.add_argument("--seed", type=int, help="override the profile's seed")
    parser.add_argument("--epochs", type=int, help="override the profile's training epochs")
    parser.add_argument("--bootstrap", type=int, help="override the bootstrap repetitions")
    parser.add_argument("--permutations", type=int, help="override the label permutations")
    parser.add_argument(
        "--provenance-module",
        action="append",
        default=[],
        metavar="NAME",
        help="import NAME first so it can register a provenance validator (repeatable)",
    )


def run_analysis_arguments(args: argparse.Namespace) -> dict[str, Any]:
    config = evaluation_config_for_profile(
        args.profile,
        seed=args.seed,
        epochs=args.epochs,
        bootstrap_repetitions=args.bootstrap,
        permutation_repetitions=args.permutations,
    )
    return analyze_dataset(
        args.dataset,
        args.output_dir,
        config=config,
        provenance_modules=args.provenance_module,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_analysis_arguments(parser)
    args = parser.parse_args(argv)
    try:
        summary = run_analysis_arguments(args)
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
