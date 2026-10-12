"""Contract tests for the frozen literature corpus and citation graph.

``LiteratureArtifactTests`` needs only tracked files. ``CitationGraphArtifactTests``
needs the two large artifacts in ``generated/large/`` (fetch them with
``python -m research.literature.fetch_large``); it is marked
``requires_large_artifacts`` and skipped when they are absent.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

import pytest

from research.literature.scripts.build_citation_artifacts import (
    OUTPUTS as CITATION_OUTPUTS,
)
from research.literature.scripts.build_citation_artifacts import (
    build as build_citation,
)
from research.literature.scripts.build_citation_artifacts import (
    private_raw_availability,
    verify_large_artifacts,
    verify_tracked_bundle_without_private_raw,
)
from research.literature.scripts.build_literature_artifacts import (
    OUTPUTS as LITERATURE_OUTPUTS,
)
from research.literature.scripts.build_literature_artifacts import (
    build as build_literature,
)
from research.literature.scripts.collect_fulltext_evidence import (
    extract_html,
    safe_license_url,
)
from research.literature.scripts.corpus_common import (
    ACCESS_BLOCKED_DOIS,
    FETCH_LARGE_COMMAND,
    GENERATED_DIR,
    LARGE_DIR,
    REPO_ROOT,
    SNAPSHOTS_DIR,
    large_artifacts_present,
    normalize_persistent_id,
)


def load(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return value


LARGE_ARTIFACTS_SKIP_REASON = (
    f"large literature artifacts are not in {LARGE_DIR.relative_to(REPO_ROOT).as_posix()}; "
    f"run `{FETCH_LARGE_COMMAND}`"
)


class LiteratureArtifactTests(unittest.TestCase):
    """Corpus, review, claim and retrieval-manifest contracts (tracked files only)."""

    truth: dict[str, Any]
    reviews: dict[str, Any]
    claims: dict[str, Any]
    qa: dict[str, Any]
    fulltext: dict[str, Any]
    citation_manifest: dict[str, Any]
    citation_qa: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.truth = load(GENERATED_DIR / "literature_truth_68.json")
        cls.reviews = load(GENERATED_DIR / "deep_read_reviews_68.json")
        cls.claims = load(GENERATED_DIR / "claim_ledger_68.json")
        cls.qa = load(GENERATED_DIR / "literature_qa_68.json")
        cls.fulltext = load(SNAPSHOTS_DIR / "fulltext_evidence_manifest.json")
        cls.citation_manifest = load(SNAPSHOTS_DIR / "citation_retrieval_manifest.json")
        cls.citation_qa = load(GENERATED_DIR / "citation_qa.json")

    def test_corpus_shape_and_origin_contract(self) -> None:
        self.assertEqual(self.truth["counts"]["total"], 68)
        self.assertEqual(self.truth["counts"]["papersAndPreprints"], 64)
        self.assertEqual(self.truth["counts"]["repositories"], 4)
        self.assertEqual(len(self.truth["columns"]), 41)
        origins: dict[str, int] = {}
        for row in self.truth["rows"]:
            origin = row["Corpus Origin"]
            origins[origin] = origins.get(origin, 0) + 1
        self.assertEqual(
            origins,
            {"existing-43": 43, "foundational-24": 24, "arxiv-2607.26935": 1},
        )

    def test_ids_are_unique_after_case_insensitive_doi_normalization(self) -> None:
        ids = [normalize_persistent_id(row["Persistent ID"]) for row in self.truth["rows"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_fulltext_blocked_and_repository_review_counts(self) -> None:
        statuses = [row["Canonical Review Status"] for row in self.truth["rows"]]
        self.assertEqual(statuses.count("Full text reviewed"), 60)
        self.assertEqual(statuses.count("Access blocked"), 4)
        self.assertEqual(statuses.count("Repository/project reviewed"), 4)

        blocked = {
            normalize_persistent_id(row["Persistent ID"]).split(":", 1)[1]
            for row in self.truth["rows"]
            if row["Canonical Review Status"] == "Access blocked"
        }
        self.assertEqual(blocked, ACCESS_BLOCKED_DOIS)
        monrose = next(
            row
            for row in self.truth["rows"]
            if row["Persistent ID"].startswith("DOI:10.1016/s0167")
        )
        self.assertEqual(monrose["Canonical Review Status"], "Full text reviewed")

    def test_fulltext_manifest_preserves_browser_hash_boundary(self) -> None:
        verified = [
            record
            for record in self.fulltext["records"]
            if record["status"].startswith("verified-full-text")
        ]
        byte_verified = [record for record in verified if record.get("sha256")]
        browser_verified = [
            record
            for record in verified
            if record["status"] == "verified-full-text-browser-review"
        ]
        self.assertEqual(len(verified), 60)
        self.assertEqual(len(byte_verified), 58)
        self.assertEqual(len(browser_verified), 2)
        self.assertTrue(all(record["byteHashAvailable"] is False for record in browser_verified))

    def test_every_review_has_unified_fields_and_license_boundary(self) -> None:
        required = set(self.reviews["fieldContract"])
        self.assertEqual(len(self.reviews["reviews"]), 68)
        for review in self.reviews["reviews"]:
            self.assertTrue(required.issubset(review))
            self.assertTrue(review["evidenceLocators"])
            self.assertTrue(review["license"]["name"])
            self.assertTrue(review["license"]["reuseBoundary"])

        _, extraction = extract_html(
            b"""
            <html><body>
              <a href="https://creativecommons.org/licenses/by/4.0/">Reuse terms</a>
              <a href="https://publisher.example/license">License</a>
              <a href="https://creativecommons.org.evil.example/terms">Reuse terms</a>
              <a href="https://evil.example/?next=creativecommons.org">Reuse terms</a>
              <a href="javascript:alert(1)">License</a>
            </body></html>
            """,
            "text/html; charset=utf-8",
        )
        userinfo_url = "https://user:secret" + chr(64) + "publisher.example/license"
        self.assertIsNone(safe_license_url(userinfo_url))
        self.assertEqual(
            extraction["licenseLinks"],
            [
                {
                    "label": "Reuse terms",
                    "url": "https://creativecommons.org/licenses/by/4.0/",
                },
                {"label": "License", "url": "https://publisher.example/license"},
            ],
        )

    def test_claims_are_located_and_abstract_numbers_are_not_promoted(self) -> None:
        self.assertEqual(len(self.claims["claims"]), 98)
        claim_ids = [claim["claimId"] for claim in self.claims["claims"]]
        self.assertEqual(len(claim_ids), len(set(claim_ids)))
        for claim in self.claims["claims"]:
            self.assertTrue(claim["locator"])
            self.assertIn("sourceUrl", claim["locator"])
            if claim["numericClaim"]:
                self.assertNotIn(
                    claim["evidenceLevel"],
                    {"metadata-and-abstract-only", "abstract-only"},
                )
                self.assertTrue(
                    any(
                        key in claim["locator"]
                        for key in (
                            "page",
                            "section",
                            "table",
                            "figure",
                            "equation",
                            "appendix",
                            "sectionFigureTable",
                        )
                    )
                )

    def test_repository_snapshots_are_pinned_and_license_is_not_inferred(self) -> None:
        repositories = [
            row for row in self.truth["rows"] if row["Item Class"] == "repository"
        ]
        self.assertEqual(len(repositories), 4)
        for row in repositories:
            self.assertRegex(row["Source Snapshot SHA256/Commit"], r"^[0-9a-f]{40}$")
        benchmark = next(
            row
            for row in repositories
            if row["Persistent ID"] == "GitHub:browser-use/benchmark"
        )
        self.assertEqual(benchmark["Source License"], "NOASSERTION")

    def test_qa_is_closed_and_passes(self) -> None:
        self.assertEqual(self.qa["status"], "pass")
        self.assertTrue(all(self.qa["checks"].values()))

    def test_rebuild_is_byte_identical(self) -> None:
        generated = build_literature()
        for name, content in generated.items():
            self.assertEqual(LITERATURE_OUTPUTS[name].read_bytes(), content, name)

    def test_provider_pagination_is_exhausted_or_explicitly_blocked(self) -> None:
        for provider, directions in (
            ("openalex", ("incoming", "outgoing")),
            ("semanticScholar", ("citations", "references")),
        ):
            for record in self.citation_manifest["records"][provider]:
                for direction in directions:
                    state = record["directions"][direction]
                    status = state["status"]
                    if status in {"complete", "complete-zero-provider-count"}:
                        self.assertEqual(
                            int(state.get("retrievedCount") or 0),
                            int(state.get("expectedCount") or 0),
                        )
                        self.assertEqual(state.get("remaining"), 0)
                    else:
                        self.assertTrue(
                            status.startswith("blocked")
                            or status == "count-mismatch",
                            (provider, record["persistentId"], direction, status),
                        )
                        self.assertIn("remaining", state)

    def test_citation_public_manifest_redacts_credential_parameters(self) -> None:
        def inspect(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"requestUrl", "finalUrl"} and isinstance(item, str):
                        lowered = item.lower()
                        self.assertNotIn("api_key=", lowered)
                        self.assertNotIn("apikey=", lowered)
                        self.assertNotIn("mailto=", lowered)
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)

        inspect(self.citation_manifest)

    def test_citation_qa_is_closed_and_passes(self) -> None:
        self.assertEqual(self.citation_qa["status"], "pass")
        self.assertTrue(all(self.citation_qa["checks"].values()))

    def test_private_raw_storage_is_ignored(self) -> None:
        ignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("private_data/", ignore_text)
        for record in self.fulltext["records"]:
            private_path = record.get("rawPrivatePath")
            if private_path:
                self.assertTrue(private_path.startswith("private_data/"))


@pytest.mark.requires_large_artifacts
@unittest.skipUnless(large_artifacts_present(), LARGE_ARTIFACTS_SKIP_REASON)
class CitationGraphArtifactTests(unittest.TestCase):
    """Citation-graph contracts that read the large artifacts in generated/large/."""

    truth: dict[str, Any]
    citation_manifest: dict[str, Any]
    citation_graph: dict[str, Any]
    citation_qa: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        # Present files must match LARGE_ARTIFACTS.json; a corrupt download fails here.
        verify_large_artifacts(require=True)
        cls.truth = load(GENERATED_DIR / "literature_truth_68.json")
        cls.citation_manifest = load(SNAPSHOTS_DIR / "citation_retrieval_manifest.json")
        cls.citation_graph = load(LARGE_DIR / "citation_graph.json")
        cls.citation_qa = load(GENERATED_DIR / "citation_qa.json")

    def test_citation_window_is_honest_about_partial_coverage(self) -> None:
        window = self.citation_manifest["retrievalWindow"]
        self.assertEqual(window["status"], "partial")
        self.assertLess(window["startedAt"], window["completedAt"])
        primary = self.citation_graph["coverage"]["openAlexPrimary"]
        self.assertEqual(primary["resolvedSeeds"], 63)
        self.assertEqual(primary["requestedSeeds"], 64)
        self.assertEqual(primary["incomingCompleteSeeds"], 63)
        self.assertEqual(primary["outgoingCompleteSeeds"], 63)
        self.assertEqual(
            primary["unresolvedSeeds"][0]["persistentId"],
            "IEEE-S&P-2026:WebCloak",
        )
        self.assertIn(
            primary["externalMetadata"]["status"],
            {"partial-provider-not-found", "partial"},
        )

    def test_external_nodes_and_direction_contract(self) -> None:
        counts = self.citation_graph["counts"]
        self.assertEqual(counts["seedNodes"], 68)
        self.assertGreater(counts["externalNodes"], 0)
        self.assertEqual(counts["nodes"], len(self.citation_graph["nodes"]))
        for assertion in self.citation_graph["assertions"]:
            self.assertEqual(assertion["predicate"], "cites")
            self.assertNotEqual(
                assertion["subjectPersistentId"], assertion["objectPersistentId"]
            )

    def test_no_unhandled_rate_limit_and_provider_overlap_is_quantified(self) -> None:
        self.assertEqual(self.citation_qa["metrics"]["unhandled429"], 0)
        overlap = self.citation_graph["providerOverlap"]
        self.assertEqual(len(overlap), 3)
        for row in overlap:
            self.assertGreaterEqual(row["jaccard"], 0.0)
            self.assertLessEqual(row["jaccard"], 1.0)

    def test_frozen_direction_and_internal_edge_counts_are_explicit(self) -> None:
        expected = {
            "openalex": {
                "incoming": {"complete": 63, "partial": 0, "blocked": 1, "total": 64},
                "outgoing": {"complete": 63, "partial": 0, "blocked": 1, "total": 64},
            },
            "semantic-scholar": {
                "citations": {"complete": 61, "partial": 1, "blocked": 2, "total": 64},
                "references": {"complete": 53, "partial": 0, "blocked": 11, "total": 64},
            },
        }
        self.assertEqual(self.citation_qa["directionStatusCounts"], expected)
        self.assertEqual(
            self.citation_graph["coverage"]["directionStatusCounts"], expected
        )
        self.assertEqual(self.citation_qa["metrics"]["completeDirections"], 240)
        self.assertEqual(self.citation_qa["metrics"]["partialDirections"], 1)
        self.assertEqual(self.citation_qa["metrics"]["blockedDirections"], 15)
        self.assertEqual(self.citation_graph["counts"]["assertedEdges"], 22117)
        self.assertEqual(self.citation_graph["counts"]["seedToSeedEdges"], 189)
        self.assertEqual(self.citation_graph["counts"]["externalInvolvedEdges"], 21928)

    def test_xpac_is_only_an_openalex_membership_marker(self) -> None:
        xpac = self.citation_graph["xpacSemantics"]
        self.assertEqual(xpac["relevanceInference"], "none")
        self.assertEqual(
            xpac["labels"], ["openalex-core", "openalex-xpac", "unresolved"]
        )
        self.assertGreaterEqual(xpac["xpacOnlyNodes"], 4)
        for node in self.citation_graph["nodes"]:
            self.assertIn(
                node["openAlexMembership"],
                {"openalex-core", "openalex-xpac", "unresolved"},
            )

    def test_citation_assertions_are_provider_declared_and_auditable(self) -> None:
        assertions = self.citation_graph["assertions"]
        self.assertGreater(len(assertions), 0)
        self.assertEqual(len(assertions), self.citation_graph["counts"]["assertedEdges"])
        for assertion in assertions:
            self.assertEqual(
                assertion["assertionMethod"],
                "provider-declared-exact-identifiers-only",
            )
            self.assertTrue(assertion["providerEvidence"])
            self.assertEqual(
                assertion["confidence"],
                "multi-provider" if len(assertion["providers"]) > 1 else "single-provider",
            )
            for evidence in assertion["providerEvidence"]:
                self.assertTrue(evidence["rawPrivatePath"].startswith("private_data/"))
                for field in (
                    "endpoint",
                    "canonicalQuery",
                    "sourceProviderId",
                    "targetProviderId",
                    "pageNumber",
                    "retrievedAt",
                    "httpStatus",
                    "rateLimitHeaders",
                    "rawSha256",
                    "parser",
                    "parserVersion",
                    "providerDocsUrl",
                    "providerDataLicense",
                ):
                    self.assertIn(field, evidence)

    def test_semantic_scholar_public_nodes_are_minimal(self) -> None:
        for node in self.citation_graph["nodes"]:
            if node["metadataProviders"] == ["semantic-scholar-minimal-id"]:
                self.assertEqual(node["title"], "")
                self.assertIsNone(node["publicationYear"])
        self.assertIn(
            "S2 provider IDs, support/count flags, and attribution",
            self.citation_graph["coverage"]["semanticScholarSensitivity"][
                "publicBoundary"
            ],
        )
        truth_titles = {
            normalize_persistent_id(row["Persistent ID"]): row["Title"]
            for row in self.truth["rows"]
        }
        for record in self.citation_manifest["records"]["semanticScholar"]:
            self.assertEqual(record.get("title"), truth_titles[record["persistentId"]])

    def test_citation_rebuild_is_byte_identical(self) -> None:
        if private_raw_availability() == "absent":
            first = verify_tracked_bundle_without_private_raw()
            second = verify_tracked_bundle_without_private_raw()
            self.assertEqual(first, second)
            return
        first = build_citation()
        second = build_citation()
        self.assertEqual(first, second)
        for name, content in first.items():
            self.assertEqual(CITATION_OUTPUTS[name].read_bytes(), content, name)


if __name__ == "__main__":
    unittest.main()
