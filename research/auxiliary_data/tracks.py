"""Legally scoped, aggregate-only auxiliary dataset tracks."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .common import (
    categorical_counts,
    distribution_comparison,
    ledger_for_sample,
    population_fingerprint,
    sha256_bytes,
)
from .remote import PublicRangeReader, head_public
from .sampling import (
    allocate_largest_remainder,
    hash_priority_sample,
    stratified_hash_priority_sample,
)


CAPTCHA_SAMPLE_COUNT = 5_000
CAPTCHA_SEED = 2026081101
AALTO_SAMPLE_COUNT = 5_000
AALTO_SEED = 2026081102
MOLMOWEB_HUMAN_SAMPLE_COUNT = 10_000
MOLMOWEB_HUMAN_SEED = 2026081103
MOLMOWEB_SYNTHETIC_SAMPLE_COUNT = 10_000
MOLMOWEB_SYNTHETIC_SEED = 2026081104
MOLMOWEB_FOLD_SEED = 2026081105

AALTO_PROVIDER_PAGE = "https://userinterfaces.aalto.fi/136Mkeystrokes/"
AALTO_ARCHIVE_URL = "https://userinterfaces.aalto.fi/136Mkeystrokes/data/Keystrokes.zip"
AALTO_ARCHIVE_BYTES = 1_572_785_433
AALTO_ARCHIVE_ETAG = '"5dbecd19-5688a4ae747c0"'
AALTO_ARCHIVE_LAST_MODIFIED = "Thu, 29 Mar 2018 10:07:35 GMT"
AALTO_METADATA_MEMBER = "Keystrokes/files/metadata_participants.txt"
AALTO_README_MEMBER = "Keystrokes/files/readme.txt"
AALTO_PHYSICAL_KEYBOARD_TYPES = frozenset({"full", "laptop", "small physical"})

CAPTCHA_COLUMNS = (
    "index",
    "inputSampleCount",
    "duration",
    "touchscreen",
    "gameType",
    "physicsTickCount",
    "puzzleSeed",
)
CAPTCHA_METRICS = ("duration", "inputSampleCount", "physicsTickCount")

AALTO_METRICS = ("ERROR_RATE", "AVG_WPM_15", "AVG_IKI", "ECPC", "KSPC", "ROR")
MOLMOWEB_COLUMNS = ("sample_id", "trajectory")
TIMESTAMP_KEY = re.compile(r"timestamp", re.IGNORECASE)
ACTION_NAME = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class ProjectedShardResult:
    path: str
    rows: list[dict[str, Any]]
    source_row_count: int
    row_group_count: int
    range_request_count: int
    transferred_bytes: int


def _read_projected_shard(
    artifact: Mapping[str, Any],
    *,
    columns: Sequence[str],
    prefetch_bytes: int,
    transform: Any,
) -> ProjectedShardResult:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:  # pragma: no cover - exercised by the CLI failure path
        raise RuntimeError("pyarrow is required for bounded Parquet projection") from exc

    reader = PublicRangeReader(
        str(artifact["url"]),
        int(artifact["bytes"]),
        prefetch_bytes=prefetch_bytes,
        cache_entries=8,
    )
    output: list[dict[str, Any]] = []
    try:
        parquet_file = parquet.ParquetFile(reader)
        available = set(parquet_file.schema.names)
        missing = set(columns) - available
        if missing:
            raise ValueError(f"{artifact['path']} is missing projected columns: {sorted(missing)}")
        local_offset = 0
        for row_group_index in range(parquet_file.metadata.num_row_groups):
            table = parquet_file.read_row_group(
                row_group_index,
                columns=list(columns),
                use_threads=False,
            )
            for row_index, row in enumerate(table.to_pylist(), start=local_offset):
                output.append(transform(row, row_index))
            local_offset += table.num_rows
        if local_offset != parquet_file.metadata.num_rows:
            raise AssertionError("projected Parquet row count mismatch")
        stats = reader.access_stats
        return ProjectedShardResult(
            path=str(artifact["path"]),
            rows=output,
            source_row_count=local_offset,
            row_group_count=parquet_file.metadata.num_row_groups,
            range_request_count=stats.request_count,
            transferred_bytes=stats.transferred_bytes,
        )
    finally:
        reader.close()


def _parallel_project(
    artifacts: Sequence[Mapping[str, Any]],
    *,
    columns: Sequence[str],
    prefetch_bytes: int,
    transform_factory: Any,
    max_workers: int,
) -> list[ProjectedShardResult]:
    results: list[ProjectedShardResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending = {
            executor.submit(
                _read_projected_shard,
                artifact,
                columns=columns,
                prefetch_bytes=prefetch_bytes,
                transform=transform_factory(artifact),
            ): artifact
            for artifact in artifacts
        }
        for future in as_completed(pending):
            results.append(future.result())
    return sorted(results, key=lambda result: result.path)


def run_captchasolve30k(
    source: Mapping[str, Any], *, max_workers: int = 5
) -> dict[str, Any]:
    """Build an exact 5,000-row mouse-only stratified calibration sample."""

    artifacts = list(source["artifacts"])

    def transform_factory(artifact: Mapping[str, Any]) -> Any:
        parts = str(artifact["path"]).split("/")
        split = parts[-2]
        shard = parts[-1]

        def transform(row: dict[str, Any], local_row: int) -> dict[str, Any]:
            return {
                "identity": f"{split}/{shard}/{local_row}",
                "split": split,
                "shard": shard,
                "source_index": row["index"],
                "gameType": row["gameType"],
                "touchscreen": row["touchscreen"],
                "duration": row["duration"],
                "inputSampleCount": row["inputSampleCount"],
                "physicsTickCount": row["physicsTickCount"],
            }

        return transform

    shard_results = _parallel_project(
        artifacts,
        columns=CAPTCHA_COLUMNS,
        prefetch_bytes=128 * 1024,
        transform_factory=transform_factory,
        max_workers=max_workers,
    )
    population = [row for shard in shard_results for row in shard.rows]
    if len(population) != 30_000:
        raise ValueError(f"CaptchaSolve30k row count changed: {len(population)}")
    source_indices = [row["source_index"] for row in population]
    if len(source_indices) != len(set(source_indices)):
        raise ValueError("CaptchaSolve30k source index is not unique")

    eligible = [row for row in population if row["touchscreen"] is False]
    excluded_touchscreen = len(population) - len(eligible)
    if len(eligible) < CAPTCHA_SAMPLE_COUNT:
        raise ValueError("CaptchaSolve30k has fewer than 5,000 mouse rows")
    stratum = lambda row: f"{row['split']}|{row['gameType']}"
    strata_population = Counter(stratum(row) for row in eligible)
    allocation = allocate_largest_remainder(strata_population, CAPTCHA_SAMPLE_COUNT)
    sample = stratified_hash_priority_sample(
        eligible,
        allocation=allocation,
        seed=CAPTCHA_SEED,
        identity=lambda row: str(row["identity"]),
        stratum=stratum,
    )
    if len(sample) != CAPTCHA_SAMPLE_COUNT:
        raise AssertionError("CaptchaSolve30k sample is not exactly 5,000 rows")
    if any(row["touchscreen"] is not False for row in sample):
        raise AssertionError("CaptchaSolve30k mouse-only sample contains touch rows")

    sample_strata = Counter(stratum(row) for row in sample)
    fingerprint = population_fingerprint(
        eligible,
        identity=lambda row: str(row["identity"]),
        payload=lambda row: [
            row["split"],
            row["gameType"],
            row["duration"],
            row["inputSampleCount"],
            row["physicsTickCount"],
        ],
    )
    by_game: dict[str, Any] = {}
    for game_type in sorted({str(row["gameType"]) for row in eligible}):
        game_population = [row for row in eligible if row["gameType"] == game_type]
        game_sample = [row for row in sample if row["gameType"] == game_type]
        by_game[game_type] = {
            "eligible_population_count": len(game_population),
            "sample_count": len(game_sample),
            "distribution_calibration": distribution_comparison(
                game_population, game_sample, CAPTCHA_METRICS
            ),
        }

    ledger = ledger_for_sample(
        dataset="Capycap-AI/CaptchaSolve30k",
        seed=CAPTCHA_SEED,
        population_count=len(eligible),
        selected_identities=[str(row["identity"]) for row in sample],
        strata_population=strata_population,
        strata_sample=sample_strata,
        population_fingerprint=fingerprint,
    )
    return {
        "schema_version": "1.0",
        "track": "captchasolve30k_mouse_distribution_calibration",
        "status": "complete",
        "sample_count": len(sample),
        "required_sample_count": CAPTCHA_SAMPLE_COUNT,
        "seed": CAPTCHA_SEED,
        "scope": "mouse-only aggregate distribution calibration",
        "full_source_row_count": len(population),
        "eligible_mouse_row_count": len(eligible),
        "excluded_touchscreen_row_count": excluded_touchscreen,
        "sample_touchscreen_row_count": sum(bool(row["touchscreen"]) for row in sample),
        "stratification": ["split", "gameType"],
        "sample_game_type_counts": categorical_counts(sample, "gameType"),
        "sample_split_counts": categorical_counts(sample, "split"),
        "overall_distribution_calibration": distribution_comparison(
            eligible, sample, CAPTCHA_METRICS
        ),
        "by_game_type": by_game,
        "sampling_ledger": ledger,
        "data_quality": {
            "source_index_unique": True,
            "projected_columns": list(CAPTCHA_COLUMNS),
            "prohibited_columns_read": [],
            "coordinates_read": False,
            "input_stream_read": False,
        },
        "access": {
            "method": "pinned Hugging Face auto-Parquet with HTTP Range projection",
            "artifact_count": len(shard_results),
            "range_request_count": sum(result.range_request_count for result in shard_results),
            "transferred_bytes": sum(result.transferred_bytes for result in shard_results),
            "source_rows_by_artifact": {
                result.path: result.source_row_count for result in shard_results
            },
        },
        "use_restrictions_enforced": {
            "biometric_or_reidentification_use": False,
            "row_level_output": False,
            "combined_with_fp_agent": False,
            "classifier_trained": False,
        },
    }


def _parse_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def run_aalto_136m(*, retrieved_at_utc: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Range-extract metadata and calibrate exactly 5,000 physical-keyboard participants."""

    head = head_public(AALTO_ARCHIVE_URL)
    if head["content_length"] != AALTO_ARCHIVE_BYTES:
        raise ValueError("Aalto archive byte count changed")
    if head.get("etag") != AALTO_ARCHIVE_ETAG:
        raise ValueError("Aalto archive ETag changed")
    if str(head.get("accept_ranges", "")).lower() != "bytes":
        raise ValueError("Aalto archive no longer advertises byte ranges")

    reader = PublicRangeReader(
        AALTO_ARCHIVE_URL,
        AALTO_ARCHIVE_BYTES,
        prefetch_bytes=64 * 1024,
        cache_entries=8,
    )
    try:
        with zipfile.ZipFile(reader) as archive:
            metadata_info = archive.getinfo(AALTO_METADATA_MEMBER)
            readme_info = archive.getinfo(AALTO_README_MEMBER)
            metadata_bytes = archive.read(AALTO_METADATA_MEMBER)
            readme_bytes = archive.read(AALTO_README_MEMBER)
        access_stats = reader.access_stats
    finally:
        reader.close()

    readme_text = readme_bytes.decode("utf-8", errors="replace")
    required_terms = ("non-commercial use", "attribution", "Vivek Dhakal")
    if not all(term.lower() in readme_text.lower() for term in required_terms):
        raise ValueError("Aalto embedded license/attribution text changed")

    decoded = metadata_bytes.decode("utf-8-sig", errors="strict")
    parsed_rows = list(csv.DictReader(io.StringIO(decoded), delimiter="\t"))
    participant_ids = [str(row.get("PARTICIPANT_ID", "")).strip() for row in parsed_rows]
    if not participant_ids or any(not participant_id for participant_id in participant_ids):
        raise ValueError("Aalto metadata contains a missing participant ID")
    if len(participant_ids) != len(set(participant_ids)):
        raise ValueError("Aalto metadata participant IDs are not unique")

    participants: list[dict[str, Any]] = []
    for raw, participant_id in zip(parsed_rows, participant_ids, strict=True):
        keyboard_type = str(raw.get("KEYBOARD_TYPE", "")).strip().lower()
        layout = str(raw.get("LAYOUT", "")).strip().lower() or "<missing>"
        participants.append(
            {
                "identity": participant_id,
                "keyboard_type": keyboard_type or "<missing>",
                "layout": layout,
                **{metric: _parse_float(raw.get(metric)) for metric in AALTO_METRICS},
            }
        )
    eligible = [
        participant
        for participant in participants
        if participant["keyboard_type"] in AALTO_PHYSICAL_KEYBOARD_TYPES
    ]
    if len(eligible) < AALTO_SAMPLE_COUNT:
        raise ValueError("Aalto has fewer than 5,000 eligible physical-keyboard participants")

    stratum = lambda row: f"{row['keyboard_type']}|{row['layout']}"
    strata_population = Counter(stratum(row) for row in eligible)
    allocation = allocate_largest_remainder(strata_population, AALTO_SAMPLE_COUNT)
    sample = stratified_hash_priority_sample(
        eligible,
        allocation=allocation,
        seed=AALTO_SEED,
        identity=lambda row: str(row["identity"]),
        stratum=stratum,
    )
    if len(sample) != AALTO_SAMPLE_COUNT:
        raise AssertionError("Aalto sample is not exactly 5,000 unique participants")
    if len({row["identity"] for row in sample}) != AALTO_SAMPLE_COUNT:
        raise AssertionError("Aalto sample participant IDs are not unique")

    fingerprint = population_fingerprint(
        eligible,
        identity=lambda row: str(row["identity"]),
        payload=lambda row: [
            row["keyboard_type"],
            row["layout"],
            *[row[metric] for metric in AALTO_METRICS],
        ],
    )
    sample_strata = Counter(stratum(row) for row in sample)
    ledger = ledger_for_sample(
        dataset="Aalto 136M Keystrokes",
        seed=AALTO_SEED,
        population_count=len(eligible),
        selected_identities=[str(row["identity"]) for row in sample],
        strata_population=strata_population,
        strata_sample=sample_strata,
        population_fingerprint=fingerprint,
    )
    result = {
        "schema_version": "1.0",
        "track": "aalto_136m_keyboard_distribution_calibration",
        "status": "complete",
        "sample_count": len(sample),
        "required_sample_count": AALTO_SAMPLE_COUNT,
        "seed": AALTO_SEED,
        "scope": "physical-keyboard participant-level aggregate distribution calibration",
        "metadata_participant_count": len(participants),
        "eligible_physical_keyboard_participant_count": len(eligible),
        "excluded_nonphysical_or_missing_keyboard_count": len(participants) - len(eligible),
        "stratification": ["KEYBOARD_TYPE", "LAYOUT"],
        "sample_keyboard_type_counts": categorical_counts(sample, "keyboard_type"),
        "sample_layout_counts": categorical_counts(sample, "layout"),
        "distribution_calibration": distribution_comparison(eligible, sample, AALTO_METRICS),
        "sampling_ledger": ledger,
        "data_quality": {
            "participant_id_unique": True,
            "participant_id_missing_count": 0,
            "metrics": list(AALTO_METRICS),
            "raw_keystroke_members_read": 0,
            "demographic_metrics_exported": False,
        },
        "access": {
            "method": "official ZIP central directory and two members via HTTP Range",
            "range_request_count": access_stats.request_count,
            "transferred_bytes": access_stats.transferred_bytes,
            "archive_members_read": [AALTO_README_MEMBER, AALTO_METADATA_MEMBER],
        },
        "use_restrictions_enforced": {
            "research_noncommercial_only": True,
            "attribution_required": True,
            "row_level_output": False,
            "raw_public_output": False,
            "combined_with_fp_agent": False,
            "classifier_trained": False,
        },
    }
    source_manifest = {
        "source_key": "aalto_136m_keystrokes",
        "provider": "Aalto University",
        "provider_page": AALTO_PROVIDER_PAGE,
        "retrieved_at_utc": retrieved_at_utc,
        "access": "public_read_only_range",
        "license": "research and non-commercial use with attribution to the authors",
        "permitted_scope": "keyboard-only aggregate distribution calibration",
        "restrictions": [
            "research/non-commercial use only",
            "attribute Dhakal, Feit, Kristensson, and Oulasvirta",
            "do not publish raw or sampled participant rows",
        ],
        "citation": (
            "Dhakal, V.; Feit, A. M.; Kristensson, P. O.; Oulasvirta, A. (2018). "
            "Observations on Typing from 136 Million Keystrokes. CHI '18. "
            "https://doi.org/10.1145/3173574.3174220"
        ),
        "archive": {
            "url": AALTO_ARCHIVE_URL,
            "bytes": AALTO_ARCHIVE_BYTES,
            "etag": AALTO_ARCHIVE_ETAG,
            "last_modified": head.get("last_modified"),
            "sha256": None,
            "sha256_status": (
                "not provider-published and not recomputed because the scoped range workflow "
                "does not download the 1.57 GB full archive"
            ),
        },
        "accessed_members": [
            {
                "path": AALTO_README_MEMBER,
                "bytes": len(readme_bytes),
                "compressed_bytes": readme_info.compress_size,
                "zip_crc32": f"{readme_info.CRC:08x}",
                "sha256": sha256_bytes(readme_bytes),
            },
            {
                "path": AALTO_METADATA_MEMBER,
                "bytes": len(metadata_bytes),
                "compressed_bytes": metadata_info.compress_size,
                "zip_crc32": f"{metadata_info.CRC:08x}",
                "sha256": sha256_bytes(metadata_bytes),
            },
        ],
    }
    return result, source_manifest


def _step_order(value: Any) -> tuple[int, str]:
    try:
        return (0, f"{int(value):020d}")
    except (TypeError, ValueError):
        return (1, str(value))


def _timestamp_field_names(value: Any, output: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if TIMESTAMP_KEY.search(str(key)):
                output.add(str(key))
            _timestamp_field_names(child, output)
    elif isinstance(value, list):
        for child in value:
            _timestamp_field_names(child, output)


def _normalize_action_name(step: Mapping[str, Any]) -> str:
    action = step.get("action")
    if isinstance(action, dict):
        action_output = action.get("action_output")
        if isinstance(action_output, dict):
            name = action_output.get("action_name")
            if isinstance(name, str) and name.strip():
                return name.strip().lower()
        direct_name = action.get("action_name")
        if isinstance(direct_name, str) and direct_name.strip():
            return direct_name.strip().lower()
        action_string = action.get("action_str")
        if isinstance(action_string, str) and (match := ACTION_NAME.match(action_string)):
            return match.group(1).lower()
    return "unknown_action"


def _normalize_domain(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    for prefix in ("www.", "m.", "amp."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    return host or None


def parse_molmoweb_trajectory(raw_trajectory: str) -> dict[str, Any]:
    """Extract only ordered high-level actions/domain; timestamp values are discarded."""

    parsed = json.loads(raw_trajectory)
    if isinstance(parsed, dict):
        steps = [parsed[key] for key in sorted(parsed, key=_step_order)]
    elif isinstance(parsed, list):
        steps = parsed
    else:
        raise ValueError("MolmoWeb trajectory must be a JSON object or list")
    timestamp_fields: set[str] = set()
    _timestamp_field_names(parsed, timestamp_fields)
    actions: list[str] = []
    domains: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        actions.append(_normalize_action_name(step))
        other_observation = step.get("other_obs")
        if isinstance(other_observation, dict):
            candidates: list[Any] = [other_observation.get("url")]
            open_urls = other_observation.get("open_pages_urls")
            if isinstance(open_urls, list):
                candidates.extend(open_urls)
            for candidate in candidates:
                domain = _normalize_domain(candidate)
                if domain is not None:
                    domains.append(domain)
    return {
        "action_sequence": " ".join(actions) if actions else "unknown_action",
        "action_count": len(actions),
        "domain": domains[0] if domains else "<missing-domain>",
        "timestamp_fields": sorted(timestamp_fields),
    }


def _molmo_transform_factory(label: str, config: str, artifact_path: str) -> Any:
    def transform(row: dict[str, Any], local_row: int) -> dict[str, Any]:
        parsed = parse_molmoweb_trajectory(str(row["trajectory"]))
        return {
            "identity": f"{label}/{config}/{artifact_path}/{local_row}",
            "sample_id": str(row["sample_id"]),
            "label": label,
            "config": config,
            **parsed,
        }

    return transform


def _molmo_config(path: str, label: str) -> str:
    filename = path.rsplit("/", 1)[-1]
    if label == "human":
        return "default"
    match = re.match(r"(.+)-\d{5}\.parquet$", filename)
    if not match:
        raise ValueError(f"unexpected MolmoWeb shard name: {path}")
    return match.group(1)


def _metric_bundle(labels: Sequence[int], predictions: Sequence[int], scores: Sequence[float]) -> dict[str, Any]:
    from sklearn.metrics import (
        balanced_accuracy_score,
        brier_score_loss,
        confusion_matrix,
        f1_score,
        roc_auc_score,
    )

    matrix = confusion_matrix(labels, predictions, labels=[0, 1])
    return {
        "balanced_accuracy": float(balanced_accuracy_score(labels, predictions)),
        "macro_f1": float(f1_score(labels, predictions, average="macro", zero_division=0)),
        "auroc": float(roc_auc_score(labels, scores)),
        "brier": float(brier_score_loss(labels, scores)),
        "confusion_matrix": {
            "labels": ["human", "synthetic"],
            "values": matrix.astype(int).tolist(),
        },
    }


def evaluate_molmoweb_domain_held_out(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare action-sequence and domain-only models under disjoint-domain folds."""

    import numpy as np
    import sklearn
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedGroupKFold
    from sklearn.preprocessing import OneHotEncoder

    labels = np.asarray([1 if row["label"] == "synthetic" else 0 for row in rows], dtype=int)
    actions = np.asarray([str(row["action_sequence"]) for row in rows], dtype=object)
    domains = np.asarray([str(row["domain"]) for row in rows], dtype=object)
    if len(set(domains)) < 5:
        raise ValueError("MolmoWeb sample has fewer than five domains")
    splitter = StratifiedGroupKFold(
        n_splits=5,
        shuffle=True,
        random_state=MOLMOWEB_FOLD_SEED,
    )
    action_predictions = np.zeros(len(rows), dtype=int)
    action_scores = np.zeros(len(rows), dtype=float)
    domain_predictions = np.zeros(len(rows), dtype=int)
    domain_scores = np.zeros(len(rows), dtype=float)
    folds: list[dict[str, Any]] = []

    for fold_index, (train_indices, test_indices) in enumerate(
        splitter.split(actions, labels, groups=domains), start=1
    ):
        train_domains = set(domains[train_indices])
        test_domains = set(domains[test_indices])
        overlap = train_domains & test_domains
        if overlap:
            raise AssertionError("domain-held-out fold leaked a domain")
        if len(set(labels[train_indices])) != 2 or len(set(labels[test_indices])) != 2:
            raise ValueError("domain-held-out fold does not contain both source labels")

        action_vectorizer = TfidfVectorizer(
            lowercase=False,
            token_pattern=r"(?u)\b[a-z][a-z0-9_]*\b",
            ngram_range=(1, 3),
            min_df=2,
            max_features=5_000,
            sublinear_tf=True,
        )
        action_train = action_vectorizer.fit_transform(actions[train_indices])
        action_test = action_vectorizer.transform(actions[test_indices])
        action_model = LogisticRegression(
            class_weight="balanced",
            max_iter=2_000,
            random_state=MOLMOWEB_FOLD_SEED + fold_index,
            solver="liblinear",
        )
        action_model.fit(action_train, labels[train_indices])
        action_fold_scores = action_model.predict_proba(action_test)[:, 1]
        action_scores[test_indices] = action_fold_scores
        action_predictions[test_indices] = (action_fold_scores >= 0.5).astype(int)

        encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=True)
        domain_train = encoder.fit_transform(domains[train_indices].reshape(-1, 1))
        domain_test = encoder.transform(domains[test_indices].reshape(-1, 1))
        domain_model = LogisticRegression(
            class_weight="balanced",
            max_iter=2_000,
            random_state=MOLMOWEB_FOLD_SEED + fold_index,
            solver="liblinear",
        )
        domain_model.fit(domain_train, labels[train_indices])
        domain_fold_scores = domain_model.predict_proba(domain_test)[:, 1]
        domain_scores[test_indices] = domain_fold_scores
        domain_predictions[test_indices] = (domain_fold_scores >= 0.5).astype(int)

        folds.append(
            {
                "fold": fold_index,
                "train_rows": len(train_indices),
                "test_rows": len(test_indices),
                "train_domains": len(train_domains),
                "test_domains": len(test_domains),
                "domain_overlap_count": 0,
                "test_human_rows": int((labels[test_indices] == 0).sum()),
                "test_synthetic_rows": int((labels[test_indices] == 1).sum()),
                "action_sequence": _metric_bundle(
                    labels[test_indices],
                    action_predictions[test_indices],
                    action_scores[test_indices],
                ),
                "domain_only": _metric_bundle(
                    labels[test_indices],
                    domain_predictions[test_indices],
                    domain_scores[test_indices],
                ),
            }
        )

    return {
        "protocol": "5-fold stratified group holdout by normalized hostname",
        "fold_seed": MOLMOWEB_FOLD_SEED,
        "rows": len(rows),
        "unique_domains": len(set(domains)),
        "domain_overlap_in_every_fold": 0,
        "action_sequence_model": {
            "features": "ordered action-name 1-3 grams only",
            "metrics": _metric_bundle(labels, action_predictions, action_scores),
        },
        "domain_only_baseline": {
            "features": "one-hot normalized hostname only; unseen domains map to all-zero",
            "metrics": _metric_bundle(labels, domain_predictions, domain_scores),
        },
        "folds": folds,
        "runtime": {"scikit_learn": sklearn.__version__},
    }


def _timestamp_audit(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    affected = [row for row in rows if row["timestamp_fields"]]
    field_row_counts = Counter(
        field for row in rows for field in set(row["timestamp_fields"])
    )
    return {
        "rows_audited": len(rows),
        "rows_with_timestamp_fields": len(affected),
        "timestamp_field_row_counts": dict(sorted(field_row_counts.items())),
        "released_rows_verified_timestamp_free": len(affected) == 0,
        "timestamp_values_retained": False,
        "timestamp_features_computed": False,
    }


def molmoweb_status(timestamp_audit: Mapping[str, Any]) -> str:
    """Track status derived from the executed timestamp audit, never assumed."""

    if timestamp_audit.get("both_sources_verified_timestamp_free") is True:
        return "complete"
    return "complete_with_timestamp_absence_check_failed"


def run_molmoweb(
    human_source: Mapping[str, Any],
    synthetic_source: Mapping[str, Any],
    *,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Run a separate 10k+10k domain-held-out action-sequence analysis."""

    def project_source(source: Mapping[str, Any], label: str) -> list[ProjectedShardResult]:
        artifacts = [
            artifact
            for artifact in source["artifacts"]
            if (label != "human" or "/train-" in str(artifact["path"]))
        ]

        def transform_factory(artifact: Mapping[str, Any]) -> Any:
            path = str(artifact["path"])
            return _molmo_transform_factory(label, _molmo_config(path, label), path)

        return _parallel_project(
            artifacts,
            columns=MOLMOWEB_COLUMNS,
            prefetch_bytes=512 * 1024,
            transform_factory=transform_factory,
            max_workers=max_workers,
        )

    # Process the two sources independently; never concatenate with any non-MolmoWeb corpus.
    human_shards = project_source(human_source, "human")
    synthetic_shards = project_source(synthetic_source, "synthetic")
    human_population = [row for shard in human_shards for row in shard.rows]
    synthetic_population = [row for shard in synthetic_shards for row in shard.rows]
    if len(human_population) != 35_971:
        raise ValueError(f"MolmoWeb Human train row count changed: {len(human_population)}")
    if len(synthetic_population) != 108_254:
        raise ValueError(f"MolmoWeb Synthetic row count changed: {len(synthetic_population)}")

    human_sample = hash_priority_sample(
        human_population,
        MOLMOWEB_HUMAN_SAMPLE_COUNT,
        seed=MOLMOWEB_HUMAN_SEED,
        identity=lambda row: str(row["identity"]),
    )
    synthetic_config_counts = Counter(str(row["config"]) for row in synthetic_population)
    synthetic_allocation = allocate_largest_remainder(
        synthetic_config_counts, MOLMOWEB_SYNTHETIC_SAMPLE_COUNT
    )
    synthetic_sample = stratified_hash_priority_sample(
        synthetic_population,
        allocation=synthetic_allocation,
        seed=MOLMOWEB_SYNTHETIC_SEED,
        identity=lambda row: str(row["identity"]),
        stratum=lambda row: str(row["config"]),
    )
    if len(human_sample) != 10_000 or len(synthetic_sample) != 10_000:
        raise AssertionError("MolmoWeb samples must contain exactly 10,000 rows per source")

    human_fingerprint = population_fingerprint(
        human_population,
        identity=lambda row: str(row["identity"]),
        payload=lambda row: [
            row["domain"], row["action_sequence"], row["timestamp_fields"], row["sample_id"]
        ],
    )
    synthetic_fingerprint = population_fingerprint(
        synthetic_population,
        identity=lambda row: str(row["identity"]),
        payload=lambda row: [
            row["config"],
            row["domain"],
            row["action_sequence"],
            row["timestamp_fields"],
            row["sample_id"],
        ],
    )
    human_ledger = ledger_for_sample(
        dataset="allenai/MolmoWeb-HumanTrajs",
        seed=MOLMOWEB_HUMAN_SEED,
        population_count=len(human_population),
        selected_identities=[str(row["identity"]) for row in human_sample],
        strata_population={"human_train": len(human_population)},
        strata_sample={"human_train": len(human_sample)},
        population_fingerprint=human_fingerprint,
    )
    synthetic_ledger = ledger_for_sample(
        dataset="allenai/MolmoWeb-SyntheticTrajs",
        seed=MOLMOWEB_SYNTHETIC_SEED,
        population_count=len(synthetic_population),
        selected_identities=[str(row["identity"]) for row in synthetic_sample],
        strata_population=synthetic_config_counts,
        strata_sample=Counter(str(row["config"]) for row in synthetic_sample),
        population_fingerprint=synthetic_fingerprint,
    )
    analysis_rows = sorted(
        [*human_sample, *synthetic_sample], key=lambda row: str(row["identity"])
    )
    held_out = evaluate_molmoweb_domain_held_out(analysis_rows)

    all_sample_ids = [row["sample_id"] for row in analysis_rows]
    timestamp_audit = {
        "scope": "all released train rows, not only sampled rows",
        "human": _timestamp_audit(human_population),
        "synthetic": _timestamp_audit(synthetic_population),
    }
    timestamp_audit["both_sources_verified_timestamp_free"] = bool(
        timestamp_audit["human"]["released_rows_verified_timestamp_free"]
        and timestamp_audit["synthetic"]["released_rows_verified_timestamp_free"]
    )
    timestamp_audit["constraint_result"] = (
        "pass"
        if timestamp_audit["both_sources_verified_timestamp_free"]
        else "failed_absence_assumption; timestamps discarded and analysis kept non-timing"
    )

    shard_results = [*human_shards, *synthetic_shards]
    return {
        "schema_version": "1.0",
        "track": "molmoweb_domain_held_out_action_sequence",
        "status": molmoweb_status(timestamp_audit),
        "human_sample_count": len(human_sample),
        "synthetic_sample_count": len(synthetic_sample),
        "required_sample_count_each": 10_000,
        "human_seed": MOLMOWEB_HUMAN_SEED,
        "synthetic_seed": MOLMOWEB_SYNTHETIC_SEED,
        "human_population_count": len(human_population),
        "synthetic_population_count": len(synthetic_population),
        "synthetic_config_sample_counts": dict(
            sorted(Counter(str(row["config"]) for row in synthetic_sample).items())
        ),
        "sampling_ledgers": {"human": human_ledger, "synthetic": synthetic_ledger},
        "timestamp_audit": timestamp_audit,
        "action_count_distributions": {
            "human": distribution_comparison(human_population, human_sample, ["action_count"]),
            "synthetic": distribution_comparison(
                synthetic_population, synthetic_sample, ["action_count"]
            ),
        },
        "domain_held_out_analysis": held_out,
        "data_quality": {
            "sample_id_duplicate_count_in_20k_sample": len(all_sample_ids)
            - len(set(all_sample_ids)),
            "human_unique_domains_in_sample": len({row["domain"] for row in human_sample}),
            "synthetic_unique_domains_in_sample": len(
                {row["domain"] for row in synthetic_sample}
            ),
            "projected_columns": list(MOLMOWEB_COLUMNS),
            "image_columns_read": False,
            "instruction_text_read": False,
        },
        "access": {
            "method": "pinned source Parquet with HTTP Range projection",
            "artifact_count": len(shard_results),
            "range_request_count": sum(result.range_request_count for result in shard_results),
            "transferred_bytes": sum(result.transferred_bytes for result in shard_results),
            "source_rows_by_artifact": {
                result.path: result.source_row_count for result in shard_results
            },
        },
        "separation_guards": {
            "combined_with_fp_agent": False,
            "fed_to_23_feature_timing_model": False,
            "headline_cross_dataset_classifier_reported": False,
            "analysis_population": "MolmoWeb HumanTrajs and SyntheticTrajs only",
            "features": "ordered high-level action names; domain only for baseline/grouping",
        },
        "use_restrictions_enforced": {
            "research_educational_scope": True,
            "odc_by_attribution": True,
            "row_level_output": False,
            "raw_public_output": False,
            "timestamp_values_retained": False,
        },
    }
