"""Build deterministic 68-item literature truth, reviews, claims, CSV, Markdown, and QA.

All timestamps come from persisted source/retrieval snapshots.  Rebuilding the
same inputs therefore produces byte-identical tracked artifacts.  Use ``--check``
in CI to detect drift without rewriting files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from corpus_common import (  # type: ignore[import-not-found]
        ACCESS_BLOCKED_DOIS,
        EXPECTED_PAPERS,
        EXPECTED_REPOSITORIES,
        EXPECTED_TOTAL,
        GENERATED_DIR,
        REPO_ROOT,
        SNAPSHOTS_DIR,
        SOURCES_DIR,
        doi_from_persistent_id,
        is_repository,
        load_corpus_rows,
        normalize_persistent_id,
        read_json,
        recorded_path,
        sha256_file,
    )
else:
    from .corpus_common import (
        ACCESS_BLOCKED_DOIS,
        EXPECTED_PAPERS,
        EXPECTED_REPOSITORIES,
        EXPECTED_TOTAL,
        GENERATED_DIR,
        REPO_ROOT,
        SNAPSHOTS_DIR,
        SOURCES_DIR,
        doi_from_persistent_id,
        is_repository,
        load_corpus_rows,
        normalize_persistent_id,
        read_json,
        recorded_path,
        sha256_file,
    )


FULLTEXT_MANIFEST_PATH = SNAPSHOTS_DIR / "fulltext_evidence_manifest.json"
MANUAL_CLAIMS_PATH = SOURCES_DIR / "manual_claims.json"
REPOSITORY_SNAPSHOTS_PATH = SOURCES_DIR / "repository_review_snapshots.json"

OUTPUTS = {
    "truth": GENERATED_DIR / "literature_truth_68.json",
    "reviews": GENERATED_DIR / "deep_read_reviews_68.json",
    "claims": GENERATED_DIR / "claim_ledger_68.json",
    "sources_csv": GENERATED_DIR / "literature_sources_68.csv",
    "claims_csv": GENERATED_DIR / "literature_claims_68.csv",
    "markdown": GENERATED_DIR / "LITERATURE_TRUTH_68.md",
    "qa": GENERATED_DIR / "literature_qa_68.json",
}

SECRET_PATTERN = re.compile(
    r"(?:api[_-]?key|authorization|bearer|token=|secret=)", re.IGNORECASE
)
NUMERIC_PATTERN = re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)?%?")


def json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def csv_bytes(fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                field: (
                    json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else value
                )
                for field, value in row.items()
            }
        )
    return buffer.getvalue().encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_manifest_records() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    payload = read_json(FULLTEXT_MANIFEST_PATH)
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("Full-text manifest has no records list")
    by_id = {
        normalize_persistent_id(str(record["persistentId"])): record
        for record in records
    }
    if len(by_id) != EXPECTED_TOTAL:
        raise ValueError(f"Full-text manifest must cover {EXPECTED_TOTAL} items")
    return by_id, payload


def load_repository_snapshots() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    payload = read_json(REPOSITORY_SNAPSHOTS_PATH)
    values = payload.get("repositories")
    if not isinstance(values, dict):
        raise ValueError("Repository snapshot file has no repositories object")
    snapshots = {
        normalize_persistent_id(key): value
        for key, value in values.items()
        if isinstance(value, dict)
    }
    if len(snapshots) != EXPECTED_REPOSITORIES:
        raise ValueError("Repository snapshot file must cover all four repositories")
    return snapshots, payload


def load_manual_claims() -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    payload = read_json(MANUAL_CLAIMS_PATH)
    claims = payload.get("claims")
    if not isinstance(claims, list):
        raise ValueError("manual_claims.json has no claims list")
    result: dict[str, list[dict[str, Any]]] = {}
    keys: set[str] = set()
    for claim in claims:
        key = str(claim["claimKey"])
        if key in keys:
            raise ValueError(f"Duplicate manual claimKey: {key}")
        keys.add(key)
        persistent_id = normalize_persistent_id(str(claim["persistentId"]))
        result.setdefault(persistent_id, []).append(dict(claim))
    return result, payload


def arxiv_license(record: dict[str, Any]) -> tuple[str, str]:
    for link in record.get("extraction", {}).get("licenseLinks", []):
        if not isinstance(link, dict):
            continue
        url = str(link.get("url", "")).lower()
        label = str(link.get("label", ""))
        match = re.search(r"creativecommons\.org/licenses/([^/]+)/([0-9.]+)", url)
        if match:
            license_name = f"CC-{match.group(1).upper()}-{match.group(2)}"
            return license_name, str(link.get("url", record.get("licenseEvidenceUrl", "")))
        if label.lower().startswith("license:"):
            return label.removeprefix("License:").strip(), str(
                link.get("url", record.get("licenseEvidenceUrl", ""))
            )
    fallback = str(record.get("license", "not-machine-verified"))
    if fallback == "machine-read-from-arxiv-page-when-present":
        fallback = "not-machine-verified"
    return fallback, str(record.get("licenseEvidenceUrl", record.get("finalUrl", "")))


def review_status(record: dict[str, Any]) -> str:
    status = str(record["status"])
    if status.startswith("verified-full-text"):
        return "Full text reviewed"
    if status == "access-blocked":
        return "Access blocked"
    if status == "repository-not-paper":
        return "Repository/project reviewed"
    raise ValueError(f"Unacceptable review status: {status}")


def locator_candidates(
    row: dict[str, Any],
    record: dict[str, Any],
    manual_for_source: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_url = str(record.get("finalUrl") or record.get("recordUrl") or row["Primary Source URL"])
    candidates = record.get("extraction", {}).get("locatorCandidates", [])
    locators: list[dict[str, Any]] = []
    for candidate in candidates[:12]:
        if isinstance(candidate, str):
            locators.append({"sourceUrl": source_url, "section": candidate})
        elif isinstance(candidate, dict):
            locator = {"sourceUrl": source_url}
            if "page" in candidate:
                locator["page"] = candidate["page"]
            if "text" in candidate:
                locator["section"] = candidate["text"]
            if "level" in candidate:
                locator["headingLevel"] = candidate["level"]
            locators.append(locator)
    if not locators and manual_for_source:
        manual_locator = manual_for_source[0].get("locator")
        if isinstance(manual_locator, dict):
            locators.append({"sourceUrl": source_url, **manual_locator})
    if not locators:
        status = str(record["status"])
        if status == "repository-not-paper":
            locators.append(
                {
                    "sourceUrl": row["Primary Source URL"],
                    "section": row["Evidence Locator"],
                }
            )
        elif status == "access-blocked":
            locators.append(
                {
                    "sourceUrl": record["recordUrl"],
                    "metadataField": "title/authors/venue/abstract only",
                }
            )
        else:
            raise ValueError(f"No claim locator available for {row['Persistent ID']}")
    return locators


def canonical_locator_text(
    row: dict[str, Any], record: dict[str, Any], locators: list[dict[str, Any]]
) -> str:
    existing = str(row["Evidence Locator"])
    if (
        review_status(record) == "Full text reviewed"
        and "full text not claimed" not in existing.lower()
        and "abstract" not in existing.lower()
    ):
        return existing
    parts: list[str] = []
    for locator in locators[:6]:
        if locator.get("section"):
            parts.append(str(locator["section"]))
        elif locator.get("page"):
            parts.append(f"page {locator['page']}")
        elif locator.get("metadataField"):
            parts.append(str(locator["metadataField"]))
    return "; ".join(parts)


def make_generic_claim(
    row: dict[str, Any],
    record: dict[str, Any],
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    status = review_status(record)
    if status == "Access blocked":
        return {
            "claimKind": "availability-boundary",
            "claimText": "This item is retained for corpus completeness, but no substantive empirical claim is promoted because full-text access is blocked.",
            "scientificClaim": False,
            "numericClaim": False,
            "evidenceLevel": "metadata-and-abstract-only",
            "locator": locators[0],
            "claimBoundary": "Metadata/abstract evidence cannot support reported magnitudes, detailed methods, or figure/table reuse.",
            "derivation": "access-boundary",
        }
    if status == "Repository/project reviewed":
        text = (
            f"The reviewed repository is used as {row['Research Role']} and exposes the "
            f"{row['Action Channel']} action channel described in its project documentation."
        )
        return {
            "claimKind": "repository-scope",
            "claimText": text,
            "scientificClaim": True,
            "numericClaim": bool(NUMERIC_PATTERN.search(text)),
            "evidenceLevel": "repository-snapshot",
            "locator": locators[0],
            "claimBoundary": "Repository behavior and license are snapshot-specific; they are not peer-reviewed detector evidence.",
            "derivation": "structured-repository-review",
        }
    text = (
        f"This source is used as {row['Research Role']} in the {row['Interface Domain']} "
        "setting; transfer of empirical results requires matching its observer position, "
        "observation signal, and evaluation protocol."
    )
    return {
        "claimKind": "scope-and-transfer-boundary",
        "claimText": text,
        "scientificClaim": True,
        "numericClaim": bool(NUMERIC_PATTERN.search(text)),
        "evidenceLevel": "full-text",
        "locator": locators[0],
        "claimBoundary": str(row["Notes/Blockers"]),
        "derivation": "structured-fulltext-scope-review",
    }


def legacy_key_section_claim(
    row: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any] | None:
    if row["Read Status"] != "Key sections reviewed":
        return None
    text = str(row["Key Takeaway"])
    return {
        "claimKind": "located-legacy-synthesis",
        "claimText": text,
        "scientificClaim": True,
        "numericClaim": bool(NUMERIC_PATTERN.search(text)),
        "evidenceLevel": "full-text-key-sections",
        "locator": {
            "sourceUrl": record.get("finalUrl") or row["Primary Source URL"],
            "sectionFigureTable": row["Evidence Locator"],
        },
        "claimBoundary": str(row["Notes/Blockers"]),
        "derivation": "pre-existing-key-section-review-reconciled-to-fulltext-snapshot",
    }


def validate_url_without_secret(url: str) -> bool:
    if not url:
        return True
    parsed = urlparse(url)
    return not SECRET_PATTERN.search(parsed.query or "")


def build() -> dict[str, bytes]:
    columns, rows, corpus_provenance = load_corpus_rows()
    manifest_records, manifest = load_manifest_records()
    repo_snapshots, repository_payload = load_repository_snapshots()
    manual_by_id, manual_payload = load_manual_claims()
    corpus_ids = {normalize_persistent_id(str(row["Persistent ID"])) for row in rows}
    unknown_claim_sources = set(manual_by_id) - corpus_ids
    if unknown_claim_sources:
        raise ValueError(f"Manual claims reference unknown sources: {unknown_claim_sources}")

    truth_rows: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    claim_rows: list[dict[str, Any]] = []
    paper_count = 0
    repository_count = 0
    full_text_count = 0
    blocked_count = 0
    repository_review_count = 0

    for corpus_index, row in enumerate(rows, start=1):
        persistent_id = normalize_persistent_id(str(row["Persistent ID"]))
        record = manifest_records[persistent_id]
        item_class = "repository" if is_repository(row) else "paper-or-preprint"
        status = review_status(record)
        if item_class == "repository":
            repository_count += 1
            repository_review_count += int(status == "Repository/project reviewed")
        else:
            paper_count += 1
            full_text_count += int(status == "Full text reviewed")
            blocked_count += int(status == "Access blocked")

        manual_claims = sorted(
            manual_by_id.get(persistent_id, []), key=lambda item: str(item["claimKey"])
        )
        locators = locator_candidates(row, record, manual_claims)
        if item_class == "repository":
            snapshot = repo_snapshots[persistent_id]
            license_name = str(snapshot["license"])
            license_evidence = str(snapshot["licenseEvidenceUrl"])
            reuse_boundary = str(snapshot["reuseBoundary"])
            reviewed_at = str(repository_payload["retrievedAt"])
            source_hash = str(snapshot["headCommit"])
            source_url = str(snapshot["headCommitUrl"])
        else:
            snapshot = None
            license_name, license_evidence = arxiv_license(record)
            reuse_boundary = str(record.get("reuseBoundary", "not resolved"))
            reviewed_at = str(record.get("accessedAt") or record.get("attemptedAt") or "not available")
            source_hash = str(record.get("sha256") or "not available for browser-render review")
            source_url = str(record.get("finalUrl") or record.get("recordUrl") or row["Primary Source URL"])

        canonical_row = {column: row[column] for column in columns}
        canonical_row["Read Status"] = status
        canonical_row["Evidence Locator"] = canonical_locator_text(row, record, locators)
        if status == "Full text reviewed" and "follow-up: full-text review" in str(
            canonical_row["QA Flag"]
        ).lower():
            canonical_row["QA Flag"] = "Full-text evidence reconciled in 68-item truth package"
        if status == "Access blocked":
            canonical_row["QA Flag"] = (
                "Access blocked: do not promote empirical numbers or detailed methods from metadata/abstract"
            )

        generic = make_generic_claim(row, record, locators)
        source_claims: list[dict[str, Any]] = [generic]
        legacy = legacy_key_section_claim(row, record)
        if legacy is not None and not manual_claims:
            source_claims.append(legacy)
        for manual in manual_claims:
            if status != "Full text reviewed":
                raise ValueError(
                    f"Manual scientific claim cannot use non-fulltext source {persistent_id}"
                )
            locator = manual.get("locator")
            if not isinstance(locator, dict) or not locator:
                raise ValueError(f"Manual claim has no locator: {manual['claimKey']}")
            source_claims.append(
                {
                    "claimKey": manual["claimKey"],
                    "claimKind": manual["claimKind"],
                    "claimText": manual["claimText"],
                    "scientificClaim": True,
                    "numericClaim": bool(manual["numericClaim"]),
                    "evidenceLevel": "full-text-manual-review",
                    "locator": {"sourceUrl": source_url, **locator},
                    "claimBoundary": manual["claimBoundary"],
                    "derivation": "manual-fulltext-review",
                }
            )

        claim_ids: list[str] = []
        for claim_number, claim in enumerate(source_claims, start=1):
            claim_id = f"CUI-LIT-{corpus_index:03d}-C{claim_number:02d}"
            claim_ids.append(claim_id)
            claim_row = {
                "claimId": claim_id,
                "claimKey": claim.get("claimKey", claim_id.lower()),
                "persistentId": persistent_id,
                "title": row["Title"],
                "itemClass": item_class,
                "sourceReviewStatus": status,
                "claimKind": claim["claimKind"],
                "claimText": claim["claimText"],
                "scientificClaim": claim["scientificClaim"],
                "numericClaim": claim["numericClaim"],
                "evidenceLevel": claim["evidenceLevel"],
                "locator": claim["locator"],
                "claimBoundary": claim["claimBoundary"],
                "derivation": claim["derivation"],
                "sourceLicense": license_name,
                "licenseEvidenceUrl": license_evidence,
                "sourceSnapshotSha256OrCommit": source_hash,
                "reviewedAt": reviewed_at,
            }
            claim_rows.append(claim_row)

        promoted_claims = [
            claim for claim in source_claims if claim["scientificClaim"]
        ]
        main_synthesis = (
            promoted_claims[-1]["claimText"]
            if promoted_claims
            else "No substantive empirical claim promoted."
        )
        review = {
            "reviewId": f"CUI-REVIEW-{corpus_index:03d}",
            "corpusIndex": corpus_index,
            "persistentId": persistent_id,
            "title": row["Title"],
            "itemClass": item_class,
            "reviewStatus": status,
            "legacyReadStatus": row["Read Status"],
            "sourceAccess": {
                "sourceUrl": source_url,
                "accessCopyKind": record.get("accessCopyKind", item_class),
                "accessedOrReviewedAt": reviewed_at,
                "sourceSnapshotSha256OrCommit": source_hash,
                "byteHashAvailable": bool(record.get("sha256") or item_class == "repository"),
                "accessOutcome": record.get("accessOutcome", record["status"]),
            },
            "license": {
                "name": license_name,
                "evidenceUrl": license_evidence,
                "reuseBoundary": reuse_boundary,
            },
            "researchQuestion": (
                f"What evidence does this item provide for {row['Research Role']} in "
                f"the {row['Interface Domain']} setting?"
            ),
            "researchRole": row["Research Role"],
            "interfaceEnvironment": row["Interface Domain"],
            "observerPosition": row["Observer Position"],
            "observableSignal": row["Observation Signal"],
            "architectureAndActionChannel": {
                "architecture": row["Agent Architecture"],
                "actionChannel": row["Action Channel"],
            },
            "evaluationProtocol": row["Evaluation Setting"],
            "mainSynthesis": main_synthesis,
            "failureAndClaimBoundary": row["Notes/Blockers"],
            "projectDesignConsequence": (
                f"Use this item to scope {row['Research Role']}; before transferring a result, "
                "match actor labels, task, executor, observer position, signal, split, and metric."
            ),
            "evidenceLocators": locators,
            "claimIds": claim_ids,
            "legacySurveyTakeaway": {
                "text": row["Key Takeaway"],
                "promotionStatus": (
                    "promoted-only-through-explicit-claim-locator"
                    if legacy is not None or manual_claims
                    else "context-only-not-promoted-to-claim"
                ),
            },
            "numericClaimPolicy": (
                "No substantive numeric claim permitted"
                if status == "Access blocked"
                else "Numeric results may appear only in claim_ledger_68 with a full-text locator"
            ),
        }
        reviews.append(review)

        truth_row = {
            **canonical_row,
            "Corpus Origin": row["Corpus Origin"],
            "Corpus Index": corpus_index,
            "Item Class": item_class,
            "Canonical Review Status": status,
            "Source License": license_name,
            "License Evidence URL": license_evidence,
            "Full Text Accessed/Reviewed At": reviewed_at,
            "Source Snapshot SHA256/Commit": source_hash,
            "Primary Claim ID": claim_ids[0],
            "Claim Count": len(claim_ids),
        }
        truth_rows.append(truth_row)

    blocked_ids = {
        doi_from_persistent_id(str(row["Persistent ID"]))
        for row in truth_rows
        if row["Canonical Review Status"] == "Access blocked"
    }
    claim_ids_all = [str(claim["claimId"]) for claim in claim_rows]
    located_claims = [
        claim
        for claim in claim_rows
        if isinstance(claim.get("locator"), dict) and bool(claim["locator"])
    ]
    numeric_claims = [claim for claim in claim_rows if claim["numericClaim"]]
    numeric_from_abstract = [
        claim
        for claim in numeric_claims
        if claim["evidenceLevel"] in {"metadata-and-abstract-only", "abstract-only"}
    ]
    url_values = [
        str(claim["locator"].get("sourceUrl", "")) for claim in claim_rows
    ] + [str(review["license"]["evidenceUrl"]) for review in reviews]

    checks = {
        "corpusCount68": len(truth_rows) == EXPECTED_TOTAL,
        "paperCount64": paper_count == EXPECTED_PAPERS,
        "repositoryCount4": repository_count == EXPECTED_REPOSITORIES,
        "originCounts43Plus24Plus1": corpus_provenance["originCounts"]
        == {"existing-43": 43, "foundational-24": 24, "arxiv-2607.26935": 1},
        "normalizedPersistentIdsUnique": len(corpus_ids) == EXPECTED_TOTAL,
        "fullTextReviewed60": full_text_count == 60,
        "accessBlockedExactly4": blocked_count == 4,
        "accessBlockedDoiSetExact": blocked_ids == ACCESS_BLOCKED_DOIS,
        "repositoriesReviewed4": repository_review_count == 4,
        "claimIdsUnique": len(claim_ids_all) == len(set(claim_ids_all)),
        "everyClaimHasLocator": len(located_claims) == len(claim_rows),
        "noNumericClaimFromAbstract": not numeric_from_abstract,
        "everyReviewHasLicense": all(review["license"]["name"] for review in reviews),
        "noCredentialLikeQueryInPublicUrls": all(
            validate_url_without_secret(url) for url in url_values
        ),
        "latestPaperPresent": "arXiv:2607.26935" in corpus_ids,
        "foundationalExpansionPreserved": any(
            value["path"] == "literature/foundational_expansion_sources.json"
            for value in corpus_provenance["inputs"]
        ),
    }
    qa_status = "pass" if all(checks.values()) else "fail"
    if qa_status != "pass":
        failed = [name for name, passed in checks.items() if not passed]
        raise ValueError(f"Literature artifact QA failed: {failed}")

    source_inputs = [
        *corpus_provenance["inputs"],
        {
            "path": recorded_path(FULLTEXT_MANIFEST_PATH),
            "sha256": sha256_file(FULLTEXT_MANIFEST_PATH),
        },
        {
            "path": recorded_path(MANUAL_CLAIMS_PATH),
            "sha256": sha256_file(MANUAL_CLAIMS_PATH),
        },
        {
            "path": recorded_path(REPOSITORY_SNAPSHOTS_PATH),
            "sha256": sha256_file(REPOSITORY_SNAPSHOTS_PATH),
        },
    ]
    snapshot_at = str(manifest["retrievalRun"]["startedOrCompletedAt"])
    truth_payload = {
        "schemaVersion": "cui-literature-truth-v2",
        "builtFromSnapshotAt": snapshot_at,
        "determinism": "Timestamp is inherited from the persisted full-text manifest; rebuild does not call the clock.",
        "counts": {
            "total": len(truth_rows),
            "papersAndPreprints": paper_count,
            "repositories": repository_count,
            "fullTextReviewed": full_text_count,
            "accessBlocked": blocked_count,
            "repositoryReviewed": repository_review_count,
        },
        "blockedDoiSet": sorted(ACCESS_BLOCKED_DOIS),
        "columns": columns,
        "supplementalColumns": [
            "Corpus Origin",
            "Corpus Index",
            "Item Class",
            "Canonical Review Status",
            "Source License",
            "License Evidence URL",
            "Full Text Accessed/Reviewed At",
            "Source Snapshot SHA256/Commit",
            "Primary Claim ID",
            "Claim Count",
        ],
        "workbookBuilderContract": {
            "primaryKey": "Persistent ID",
            "rowOrder": "Corpus Index ascending",
            "closedCanonicalColumns": 41,
            "numericClaimSource": "claim_ledger_68.json only",
            "accessBlockedRule": "Do not promote empirical numbers, detailed methods, or figure/table reuse.",
        },
        "provenance": {"inputs": source_inputs},
        "rows": truth_rows,
    }
    reviews_payload = {
        "schemaVersion": "cui-literature-deep-read-reviews-v2",
        "builtFromSnapshotAt": snapshot_at,
        "counts": {
            "reviews": len(reviews),
            "fullTextReviewed": full_text_count,
            "accessBlocked": blocked_count,
            "repositoryReviewed": repository_review_count,
        },
        "fieldContract": [
            "researchQuestion",
            "researchRole",
            "interfaceEnvironment",
            "observerPosition",
            "observableSignal",
            "architectureAndActionChannel",
            "evaluationProtocol",
            "mainSynthesis",
            "failureAndClaimBoundary",
            "projectDesignConsequence",
            "evidenceLocators",
            "license",
            "claimIds",
        ],
        "reviews": reviews,
    }
    claims_payload = {
        "schemaVersion": "cui-literature-claim-ledger-v2",
        "builtFromSnapshotAt": snapshot_at,
        "policy": {
            "numericClaims": "Only full-text-located numeric claims are promoted.",
            "blockedItems": "Availability boundary only; no substantive empirical claim.",
            "locator": "Each claim names a source URL and page, section, figure, table, equation, appendix, README section, or metadata field.",
            "license": "A source-copy license and evidence URL accompany every claim; figure/table screenshots require a compatible license or separately approved rationale.",
        },
        "counts": {
            "claims": len(claim_rows),
            "scientificClaims": sum(bool(claim["scientificClaim"]) for claim in claim_rows),
            "numericClaims": len(numeric_claims),
            "availabilityBoundaries": sum(
                claim["claimKind"] == "availability-boundary" for claim in claim_rows
            ),
        },
        "claims": claim_rows,
    }
    qa_payload = {
        "schemaVersion": "cui-literature-qa-v2",
        "builtFromSnapshotAt": snapshot_at,
        "status": qa_status,
        "checks": checks,
        "metrics": {
            "rows": len(truth_rows),
            "claims": len(claim_rows),
            "numericClaims": len(numeric_claims),
            "locators": len(located_claims),
            "licenses": len(reviews),
            "inputFiles": len(source_inputs),
        },
        "sourceSchemas": {
            "manualClaims": manual_payload["schemaVersion"],
            "repositorySnapshots": repository_payload["schemaVersion"],
            "fullTextManifest": manifest["schemaVersion"],
        },
    }

    supplemental_columns = truth_payload["supplementalColumns"]
    sources_fields = [*columns, *supplemental_columns]
    claims_fields = [
        "claimId",
        "claimKey",
        "persistentId",
        "title",
        "itemClass",
        "sourceReviewStatus",
        "claimKind",
        "claimText",
        "scientificClaim",
        "numericClaim",
        "evidenceLevel",
        "locator",
        "claimBoundary",
        "derivation",
        "sourceLicense",
        "licenseEvidenceUrl",
        "sourceSnapshotSha256OrCommit",
        "reviewedAt",
    ]

    # The rebuild hint below names the source repository's script path. It is part
    # of the hash-bound generated bytes, so it stays as recorded; the current command
    # is ``python -m research.literature.scripts.build_literature_artifacts --check``.
    markdown_lines = [
        "# Canonical Literature Truth - 68 Items",
        "",
        f"Built from the persisted evidence snapshot `{snapshot_at}`. This file is derived; run `python literature/scripts/build_literature_artifacts.py --check` to verify byte-for-byte reproducibility.",
        "",
        "## Corpus contract",
        "",
        "| Check | Result |",
        "|---|---:|",
        f"| Total items | {len(truth_rows)} |",
        f"| Papers/preprints | {paper_count} |",
        f"| Repositories | {repository_count} |",
        f"| Full text reviewed | {full_text_count} |",
        f"| Access blocked | {blocked_count} |",
        f"| Repository reviews | {repository_review_count} |",
        f"| Claim-ledger entries | {len(claim_rows)} |",
        f"| Located numeric claims | {len(numeric_claims)} |",
        "",
        "The four access-blocked DOI items are retained as metadata/abstract inventory only. No empirical number from those items is promoted. Monrose and Rubin is not blocked: a university-hosted full paper was byte-verified, while its access-copy license/reuse boundary is recorded separately.",
        "",
        "## Exact access-blocked set",
        "",
        *[f"- `{doi}`" for doi in sorted(ACCESS_BLOCKED_DOIS)],
        "",
        "## Latest direct comparator",
        "",
        "`arXiv:2607.26935` is included as item 68. Its full-text review explicitly separates dataset composition, DOM-event schema, task mismatch, Playwright executor signature, OS-level threat-model exclusion, minimal-feature in-corpus results, and unreleased-artifact status. The report must not restate its executor-specific result as universal reasoning detection.",
        "",
        "## Reuse and screenshot boundary",
        "",
        "Prefer project-created plots. Reusing a source figure/table requires the access-copy license to allow it or a separately documented quotation/fair-use decision. Every reused visual must retain the paper title, figure/table locator, and citation; `NOASSERTION` and publisher-copyright sources are not default screenshot sources.",
        "",
        "## Machine-readable outputs",
        "",
        "- `literature_truth_68.json` / `literature_sources_68.csv`: workbook-builder source truth.",
        "- `deep_read_reviews_68.json`: unified review fields and source-access/license evidence.",
        "- `claim_ledger_68.json` / `literature_claims_68.csv`: claim-level locators and boundaries.",
        "- `literature_qa_68.json`: closed-schema, count, license, locator, and numeric-claim checks.",
        "",
    ]

    result = {
        "truth": json_bytes(truth_payload),
        "reviews": json_bytes(reviews_payload),
        "claims": json_bytes(claims_payload),
        "sources_csv": csv_bytes(sources_fields, truth_rows),
        "claims_csv": csv_bytes(claims_fields, claim_rows),
        "markdown": ("\n".join(markdown_lines)).encode("utf-8"),
        "qa": json_bytes(qa_payload),
    }
    return result


def write_or_check(outputs: dict[str, bytes], check: bool) -> int:
    mismatches: list[str] = []
    for name, content in outputs.items():
        path = OUTPUTS[name]
        if check:
            if not path.exists() or path.read_bytes() != content:
                mismatches.append(path.relative_to(REPO_ROOT).as_posix())
            continue
        path.write_bytes(content)
    if mismatches:
        print(json.dumps({"status": "drift", "mismatches": mismatches}, sort_keys=True))
        return 2
    summary = {
        "status": "pass" if check else "built",
        "outputs": {
            OUTPUTS[name].relative_to(REPO_ROOT).as_posix(): {
                "bytes": len(content),
                "sha256": sha256_bytes(content),
            }
            for name, content in outputs.items()
        },
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare deterministic output bytes without modifying files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return write_or_check(build(), check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
