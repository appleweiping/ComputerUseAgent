"""Build a ledger-bound private detector dataset from one formal ScienceBoard batch.

This is the only supported path from formal ScienceBoard traces into the shared
detector pipeline.  It revalidates all 27 artifact bundles, the immutable study
ledger, sealed config/capsule snapshots, and the operator's private
human-research determination record before extracting path-free behavioral
features.  Raw event sequences are never copied into the dataset or its
analysis provenance.

Importing this module registers :func:`validate_provenance_payload` as the
detector's provenance validator for ``source_stage == "scienceboard"``. The
detector CLI loads it with ``--provenance-module research.scienceboard.detector_bridge``;
a dataset whose provenance names a stage without a registered validator fails
closed in the detector.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from computer_use_agent.detector.contract import (
    DATASET_SCHEMA,
    DATASET_SCHEMAS,
    FEATURE_ALLOWLIST,
    canonical_json,
    dataset_fingerprint,
    validate_dataset,
)
from computer_use_agent.detector.extract import trace_to_record
from computer_use_agent.detector.provenance import register_provenance_validator
from computer_use_agent.study.policy import (
    FormalStudyError,
    require_human_research_use,
    validate_ledger_binding_snapshot,
)
from computer_use_agent.study.ledger import LedgerError, atomic_write_json, validate_ledger

from .orchestrate import (
    _private_output,
    build_schedule,
    canonical_json_sha256,
    collect_ledger_sessions,
    load_manifest,
    sha256,
)
from .preflight import DEFAULT_MANIFEST
from .publication import (
    PROVENANCE_FIELDS,
    build_public_summary,
    session_bundle_root_sha256,
)


BRIDGE_SCHEMA = "scienceboard-detector-bridge-v1"
SOURCE_STAGE = "scienceboard"
BRIDGE_FIELDS = frozenset(
    {
        "schema_version",
        "source_stage",
        "source_protocol_sha256",
        "source_batch_manifest_sha256",
        "study_ledger",
        "source_provenance",
        "human_research_attestation",
        "record_count",
        "record_bindings",
        "dataset_sha256",
        "feature_allowlist_sha256",
        "raw_sequences_embedded",
        "claim_class",
    }
)
RECORD_BINDING_FIELDS = frozenset(
    {
        "session_id",
        "record_ref",
        "source_sha256",
        "bundle_root_sha256",
    }
)
ANALYSIS_PROVENANCE_SCHEMA = "scienceboard-detector-analysis-source-v1"


def _hex_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read JSON object {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"Expected one JSON object in {path}")
    return payload


def _expected_record_identity(cell: Mapping[str, Any]) -> dict[str, Any]:
    human = cell["actor"] == "human"
    return {
        "label": "human" if human else "agent",
        "task_group": cell["task_id"],
        "agent_family": None if human else cell["scienceboard_community"],
        "evidence_class": "formal_trace",
        "participant_scope": "single_self_participant",
    }


def _validate_source_provenance(
    provenance: Any,
    *,
    manifest: Mapping[str, Any],
) -> None:
    if not isinstance(provenance, dict) or set(provenance) != PROVENANCE_FIELDS:
        raise ValueError("ScienceBoard detector source provenance is incomplete")
    for field in (
        "orchestrator_repository_commit",
        "scienceboard_commit",
        "driver_config_sha256",
        "guest_capability_report_sha256",
        "task_matrix_sha256",
        "task_matrix_canonical_sha256",
        "vm_zip_sha256",
    ):
        value = provenance[field]
        expected_length = 40 if field.endswith("commit") else 64
        if not isinstance(value, str) or len(value) != expected_length or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError(f"ScienceBoard detector provenance {field} is invalid")
    exact = {
        "scienceboard_commit": manifest["upstream"]["commit"],
        "task_matrix_sha256": sha256(DEFAULT_MANIFEST),
        "task_matrix_canonical_sha256": canonical_json_sha256(DEFAULT_MANIFEST),
        "snapshot": manifest["snapshot"],
        "agent_model": manifest["agent_model"],
        "selection_status": manifest["selection_status"],
    }
    mismatched = [key for key, value in exact.items() if provenance.get(key) != value]
    if mismatched:
        raise ValueError(
            "ScienceBoard detector provenance differs from the canonical protocol: "
            + ", ".join(mismatched)
        )


def validate_bridge_provenance(
    records: Sequence[Mapping[str, Any]],
    provenance: Mapping[str, Any],
    *,
    verify_private_record: bool = True,
) -> dict[str, Any]:
    """Validate that 27 path-free records remain bound to one formal batch."""

    if set(provenance) != BRIDGE_FIELDS or provenance.get("schema_version") != BRIDGE_SCHEMA:
        raise ValueError("ScienceBoard detector provenance does not match the closed schema")
    if provenance.get("source_stage") != SOURCE_STAGE:
        raise ValueError("ScienceBoard detector provenance has the wrong source stage")
    if provenance.get("record_count") != 27 or len(records) != 27:
        raise ValueError("ScienceBoard detector bridge requires exactly 27 records")
    if provenance.get("raw_sequences_embedded") is not False:
        raise ValueError("ScienceBoard detector datasets must not embed raw event sequences")
    if provenance.get("claim_class") != "single_participant_pipeline_probe":
        raise ValueError("ScienceBoard detector provenance has an invalid claim class")
    if not _hex_sha256(provenance.get("source_batch_manifest_sha256")):
        raise ValueError("ScienceBoard detector source batch hash is invalid")
    if provenance.get("feature_allowlist_sha256") != hashlib.sha256(
        canonical_json(list(FEATURE_ALLOWLIST))
    ).hexdigest():
        raise ValueError("ScienceBoard detector feature allowlist binding is invalid")

    manifest = load_manifest(DEFAULT_MANIFEST)
    schedule = build_schedule(manifest)
    expected_ids = [cell["session_id"] for cell in schedule]
    if provenance.get("source_protocol_sha256") != sha256(DEFAULT_MANIFEST):
        raise ValueError("ScienceBoard detector protocol hash differs from the task manifest")

    ledger = provenance.get("study_ledger")
    if not isinstance(ledger, dict):
        raise ValueError("ScienceBoard detector provenance has no immutable study ledger")
    try:
        validate_ledger(ledger, ledger)
    except LedgerError as error:
        raise ValueError(f"ScienceBoard detector study ledger is invalid: {error}") from error
    if (
        ledger.get("stage") != "scienceboard"
        or ledger.get("planned_count") != 27
        or ledger.get("planned_ids") != expected_ids
        or ledger.get("protocol_sha256") != provenance["source_protocol_sha256"]
    ):
        raise ValueError("ScienceBoard detector ledger differs from the canonical 27-cell protocol")
    binding_errors = validate_ledger_binding_snapshot(
        ledger.get("binding"), require_sealed=True
    )
    if binding_errors:
        raise ValueError(
            "ScienceBoard detector ledger binding is invalid: " + "; ".join(binding_errors)
        )
    config = ledger["binding"]["formal_config_snapshot"]
    try:
        require_human_research_use(
            config,
            use="detector_training",
            participant_scope="single_self_participant",
            verify_private_record=verify_private_record,
        )
    except FormalStudyError as error:
        raise ValueError(f"ScienceBoard detector human evidence is not authorized: {error}") from error

    policy = config["human_research_policy"]
    expected_attestation = {
        "determination_scope": policy["determination_scope"],
        "determination_record_sha256": policy["determination_record_sha256"],
        "verification_method": "runtime_sha256_match_before_detector_extraction",
        "private_record_published": False,
    }
    if provenance.get("human_research_attestation") != expected_attestation:
        raise ValueError("ScienceBoard detector human-research attestation is inconsistent")
    _validate_source_provenance(provenance.get("source_provenance"), manifest=manifest)

    audit = validate_dataset(
        records,
        human_research_config=config,
        verify_private_record=verify_private_record,
    )
    if audit["dataset_sha256"] != provenance.get("dataset_sha256"):
        raise ValueError("ScienceBoard detector dataset hash differs from provenance")
    if audit["class_counts"] != {"agent": 18, "human": 9}:
        raise ValueError("ScienceBoard detector dataset must contain 18 agent and 9 human records")

    bindings = provenance.get("record_bindings")
    if not isinstance(bindings, list) or len(bindings) != 27:
        raise ValueError("ScienceBoard detector provenance requires 27 record bindings")
    expected_by_id = {cell["session_id"]: cell for cell in schedule}
    records_by_ref = {str(record["record_ref"]): record for record in records}
    observed_ids: list[str] = []
    observed_refs: list[str] = []
    for item in bindings:
        if not isinstance(item, dict) or set(item) != RECORD_BINDING_FIELDS:
            raise ValueError("ScienceBoard detector record binding has an invalid schema")
        session_id = item.get("session_id")
        if not isinstance(session_id, str) or session_id not in expected_by_id:
            raise ValueError("ScienceBoard detector record binding has an unknown session")
        record_ref = item.get("record_ref")
        if not isinstance(record_ref, str) or record_ref not in records_by_ref:
            raise ValueError("ScienceBoard detector record binding has an unknown record")
        record = records_by_ref[record_ref]
        expected_identity = _expected_record_identity(expected_by_id[session_id])
        if any(record.get(key) != value for key, value in expected_identity.items()):
            raise ValueError("ScienceBoard detector record identity differs from the schedule")
        if item.get("source_sha256") != record.get("source_sha256"):
            raise ValueError("ScienceBoard detector trace hash differs from its extracted record")
        if not _hex_sha256(item.get("bundle_root_sha256")):
            raise ValueError("ScienceBoard detector bundle-root hash is invalid")
        observed_ids.append(session_id)
        observed_refs.append(record_ref)
    if observed_ids != sorted(expected_ids) or len(set(observed_refs)) != 27:
        raise ValueError("ScienceBoard detector record bindings are not unique and canonical")
    if observed_refs != [str(record["record_ref"]) for record in records]:
        raise ValueError("ScienceBoard detector record order differs from its bindings")
    return dict(audit)


def analysis_provenance(provenance: Mapping[str, Any]) -> dict[str, Any]:
    """Return the publication-safe binding included in detector result bundles."""

    ledger = provenance["study_ledger"]
    binding = ledger["binding"]
    return {
        "schema_version": ANALYSIS_PROVENANCE_SCHEMA,
        "source_stage": "scienceboard",
        "claim_class": provenance["claim_class"],
        "record_count": provenance["record_count"],
        "dataset_sha256": provenance["dataset_sha256"],
        "source_protocol_sha256": provenance["source_protocol_sha256"],
        "source_batch_manifest_sha256": provenance["source_batch_manifest_sha256"],
        "study_ledger_integrity_sha256": ledger["integrity_sha256"],
        "formal_config_sha256": binding["formal_config_sha256"],
        "release_capsule_sha256": binding["release_capsule_sha256"],
        "source_provenance": provenance["source_provenance"],
        "human_research_attestation": provenance["human_research_attestation"],
        "record_bindings_sha256": hashlib.sha256(
            canonical_json(provenance["record_bindings"])
        ).hexdigest(),
        "raw_sequences_embedded": False,
    }


def validate_bridge_dataset(
    payload: Mapping[str, Any],
    *,
    verify_private_record: bool = True,
) -> dict[str, Any]:
    """Validate a complete bridge dataset, including its exact stored audit."""

    if set(payload) != {"schema_version", "records", "audit", "provenance"}:
        raise ValueError("ScienceBoard detector dataset does not match the closed root schema")
    if payload.get("schema_version") not in DATASET_SCHEMAS:
        raise ValueError("ScienceBoard detector dataset has an unsupported schema")
    records = payload.get("records")
    provenance = payload.get("provenance")
    if not isinstance(records, list) or not isinstance(provenance, Mapping):
        raise ValueError("ScienceBoard detector dataset records/provenance are malformed")
    audit = validate_bridge_provenance(
        records,
        provenance,
        verify_private_record=verify_private_record,
    )
    if payload.get("audit") != audit:
        raise ValueError("ScienceBoard detector stored audit differs from revalidation")
    return audit


@dataclass(frozen=True, eq=False)
class LedgerHumanDataPolicy:
    """Re-authorize formal human records through the ledger's config snapshot.

    The detector calls :meth:`authorize` inside every evaluation fold; each
    call re-runs the shared fail-closed policy, including the private
    determination-record hash check.
    """

    config: Mapping[str, Any]

    def authorize(self, *, use: str, participant_scope: str) -> None:
        require_human_research_use(
            json.loads(json.dumps(dict(self.config), allow_nan=False)),
            use=use,
            participant_scope=participant_scope,
            verify_private_record=True,
        )


def _provenance_of(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or not {"records", "provenance"} <= set(payload):
        raise ValueError(
            "ScienceBoard provenance validation requires the complete dataset payload"
        )
    provenance = payload["provenance"]
    if not isinstance(provenance, Mapping):
        raise ValueError("ScienceBoard detector dataset provenance must be an object")
    return provenance


def validate_provenance_payload(payload: Mapping[str, Any]) -> None:
    """Registered detector provenance validator for ScienceBoard datasets.

    ``payload`` is the complete detector dataset (``schema_version``,
    ``records``, ``audit`` and ``provenance``). It is accepted only when
    :func:`validate_bridge_dataset` accepts it: records and provenance are
    revalidated (including the private human-research record check) and the
    stored ``audit`` must equal the revalidated audit. Raises ``ValueError``.
    """

    _provenance_of(payload)
    validate_bridge_dataset(payload)


def _analysis_provenance_hook(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return analysis_provenance(_provenance_of(payload))


def _human_data_policy_hook(payload: Mapping[str, Any]) -> LedgerHumanDataPolicy:
    provenance = _provenance_of(payload)
    config = provenance["study_ledger"]["binding"]["formal_config_snapshot"]
    return LedgerHumanDataPolicy(config)


register_provenance_validator(
    SOURCE_STAGE,
    validate_provenance_payload,
    analysis_provenance=_analysis_provenance_hook,
    human_data_policy=_human_data_policy_hook,
)


def build_detector_dataset(private_batch: Path) -> dict[str, Any]:
    """Revalidate one exact private batch and extract its 27 detector records."""

    batch_dir = _private_output(private_batch)
    public_summary = build_public_summary(batch_dir)
    manifest = load_manifest(DEFAULT_MANIFEST)
    schedule = build_schedule(manifest)
    ledger = public_summary["study_ledger"]
    config = ledger["binding"]["formal_config_snapshot"]
    require_human_research_use(
        config,
        use="detector_training",
        participant_scope="single_self_participant",
        verify_private_record=True,
    )
    source_provenance = public_summary["source_provenance"]
    results, missing, unknown = collect_ledger_sessions(
        batch_dir,
        schedule=schedule,
        manifest=manifest,
        expected_provenance=source_provenance,
    )
    if missing or unknown or len(results) != 27:
        raise ValueError("ScienceBoard detector bridge did not revalidate exactly 27 sessions")
    result_by_id = {str(result["session_id"]): result for result in results}
    descriptor_by_id = {
        str(item["session_id"]): item
        for item in public_summary["session_descriptors"]
    }

    records: list[dict[str, Any]] = []
    record_bindings: list[dict[str, Any]] = []
    for cell in sorted(schedule, key=lambda item: str(item["session_id"])):
        session_id = str(cell["session_id"])
        session_dir = batch_dir / session_id
        result = result_by_id[session_id]
        trace_descriptor = result["artifacts"]["trace"]
        trace_path = (session_dir / str(trace_descriptor["path"])).resolve(strict=True)
        identity = _expected_record_identity(cell)
        record = trace_to_record(
            trace_path,
            label=identity["label"],
            task_group=identity["task_group"],
            agent_family=identity["agent_family"],
            evidence_class=identity["evidence_class"],
            participant_scope=identity["participant_scope"],
            human_research_config=config,
        )
        if record["source_sha256"] != trace_descriptor["sha256"]:
            raise ValueError("Extracted trace hash differs from the revalidated artifact")
        bundle_root = session_bundle_root_sha256(
            result,
            session_dir / "run_manifest.json",
        )
        if bundle_root != descriptor_by_id[session_id]["bundle_root_sha256"]:
            raise ValueError("Detector bridge bundle root differs from public projection")
        records.append(record)
        record_bindings.append(
            {
                "session_id": session_id,
                "record_ref": record["record_ref"],
                "source_sha256": record["source_sha256"],
                "bundle_root_sha256": bundle_root,
            }
        )

    audit = validate_dataset(records, human_research_config=config)
    provenance = {
        "schema_version": BRIDGE_SCHEMA,
        "source_stage": "scienceboard",
        "source_protocol_sha256": sha256(DEFAULT_MANIFEST),
        "source_batch_manifest_sha256": public_summary[
            "source_batch_manifest_sha256"
        ],
        "study_ledger": ledger,
        "source_provenance": source_provenance,
        "human_research_attestation": {
            "determination_scope": config["human_research_policy"][
                "determination_scope"
            ],
            "determination_record_sha256": config["human_research_policy"][
                "determination_record_sha256"
            ],
            "verification_method": "runtime_sha256_match_before_detector_extraction",
            "private_record_published": False,
        },
        "record_count": 27,
        "record_bindings": record_bindings,
        "dataset_sha256": dataset_fingerprint(records),
        "feature_allowlist_sha256": hashlib.sha256(
            canonical_json(list(FEATURE_ALLOWLIST))
        ).hexdigest(),
        "raw_sequences_embedded": False,
        "claim_class": "single_participant_pipeline_probe",
    }
    payload = {
        "schema_version": DATASET_SCHEMA,
        "records": records,
        "audit": audit,
        "provenance": provenance,
    }
    validate_bridge_dataset(payload)
    return payload


def _private_dataset_output(path: Path) -> Path:
    output = Path(path).resolve()
    if output.suffix.lower() != ".json":
        raise ValueError("ScienceBoard detector dataset output must be a .json file")
    _private_output(output.parent)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite private detector dataset: {output}")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        output = _private_dataset_output(args.output)
        payload = build_detector_dataset(args.input_dir)
        atomic_write_json(output, payload, replace=False)
    except (FormalStudyError, LedgerError, OSError, ValueError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "output": str(output),
                "record_count": payload["audit"]["record_count"],
                "dataset_sha256": payload["audit"]["dataset_sha256"],
                "raw_sequences_embedded": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
