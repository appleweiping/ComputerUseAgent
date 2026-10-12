"""Build deterministic external citation nodes, directed edges, provenance, and QA.

Only exact provider identifiers and provider-declared citation relations are
used.  Titles never merge nodes and XPAC never implies relevance.  The builder
re-verifies every private raw SHA-256 and is deliberately independent of the
network, so the same frozen raw window rebuilds byte-identical public outputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from corpus_common import (  # type: ignore[import-not-found]
        EXPECTED_PAPERS,
        EXPECTED_REPOSITORIES,
        EXPECTED_TOTAL,
        FETCH_LARGE_COMMAND,
        GENERATED_DIR,
        LARGE_DIR,
        REPO_ROOT,
        SNAPSHOTS_DIR,
        LargeArtifactError,
        display_path,
        doi_from_persistent_id,
        is_repository,
        load_corpus_rows,
        load_large_artifacts,
        normalize_doi,
        normalize_persistent_id,
        read_json,
        recorded_path,
        resolve_private_path,
        resolve_recorded_path,
        sha256_file,
        verify_large_artifact,
    )
else:
    from .corpus_common import (
        EXPECTED_PAPERS,
        EXPECTED_REPOSITORIES,
        EXPECTED_TOTAL,
        FETCH_LARGE_COMMAND,
        GENERATED_DIR,
        LARGE_DIR,
        REPO_ROOT,
        SNAPSHOTS_DIR,
        LargeArtifactError,
        display_path,
        doi_from_persistent_id,
        is_repository,
        load_corpus_rows,
        load_large_artifacts,
        normalize_doi,
        normalize_persistent_id,
        read_json,
        recorded_path,
        resolve_private_path,
        resolve_recorded_path,
        sha256_file,
        verify_large_artifact,
    )


MANIFEST_PATH = SNAPSHOTS_DIR / "citation_retrieval_manifest.json"
OUTPUTS = {
    "graph": LARGE_DIR / "citation_graph.json",
    "nodes_csv": GENERATED_DIR / "citation_nodes.csv",
    "edges_csv": GENERATED_DIR / "citation_edges.csv",
    "assertions_csv": LARGE_DIR / "citation_assertions.csv",
    "markdown": GENERATED_DIR / "CITATION_GRAPH.md",
    "qa": GENERATED_DIR / "citation_qa.json",
}
#: Outputs that are never committed. They are recorded in generated/LARGE_ARTIFACTS.json
#: and fetched into the git-ignored generated/large/ by research.literature.fetch_large.
LARGE_OUTPUT_NAMES = frozenset({"graph", "assertions_csv"})
ALLOWED_XPAC = {"openalex-core", "openalex-xpac", "unresolved"}
REQUIRED_EVIDENCE = {
    "endpoint",
    "canonicalQuery",
    "pageNumber",
    "offset",
    "cursor",
    "retrievedAt",
    "httpStatus",
    "rateLimitHeaders",
    "rawPrivatePath",
    "rawSha256",
    "parser",
    "parserVersion",
    "providerDocsUrl",
    "providerDataLicense",
    "providerLicenseUrl",
    "sourceProviderId",
    "targetProviderId",
}


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def csv_bytes(fields: list[str], rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                field: (
                    json.dumps(row.get(field), ensure_ascii=False, sort_keys=True)
                    if isinstance(row.get(field), (list, dict))
                    else row.get(field)
                )
                for field in fields
            }
        )
    return buffer.getvalue().encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class RawReader:
    def __init__(self) -> None:
        self.cache: dict[tuple[str, str], Any] = {}
        self.verified: set[tuple[str, str]] = set()

    def read(self, evidence: dict[str, Any]) -> Any:
        key = (str(evidence["rawPrivatePath"]), str(evidence["rawSha256"]))
        if key in self.cache:
            return self.cache[key]
        path = resolve_private_path(key[0])
        raw = path.read_bytes()
        actual = sha256_bytes(raw)
        if actual != key[1]:
            raise ValueError(f"Raw hash mismatch for {path}: {actual} != {key[1]}")
        payload = json.loads(raw)
        self.cache[key] = payload
        self.verified.add(key)
        return payload


def iter_raw_evidence(value: Any):
    """Yield closed raw-response descriptors without exposing response bytes."""

    if isinstance(value, dict):
        raw_path = value.get("rawPrivatePath")
        if isinstance(raw_path, str) and raw_path:
            raw_sha256 = value.get("rawSha256")
            if not isinstance(raw_sha256, str):
                raise ValueError(f"Raw evidence has no SHA-256: {raw_path}")
            yield raw_path, raw_sha256
        for child in value.values():
            yield from iter_raw_evidence(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_raw_evidence(child)


def private_raw_availability(manifest: dict[str, Any] | None = None) -> str:
    """Return ``complete`` or ``absent``; reject a partial private raw cache."""

    manifest = manifest or read_json(MANIFEST_PATH)
    evidence = sorted(set(iter_raw_evidence(manifest)))
    if not evidence:
        raise ValueError("Citation manifest has no raw-response provenance")
    present = 0
    for relative, expected_sha256 in evidence:
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not relative.startswith("private_data/")
        ):
            raise ValueError(f"Raw evidence path is not private and relative: {relative}")
        if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise ValueError(f"Raw evidence SHA-256 is invalid: {relative}")
        present += int(resolve_private_path(relative).is_file())
    if present == len(evidence):
        return "complete"
    if present == 0:
        return "absent"
    raise FileNotFoundError(
        f"Private citation raw cache is incomplete: {present}/{len(evidence)} files present"
    )


def large_artifact_specs() -> dict[str, dict[str, Any]]:
    """Return the LARGE_ARTIFACTS.json spec for each large output, keyed by output name."""

    specs = load_large_artifacts()
    by_output: dict[str, dict[str, Any]] = {}
    for name in sorted(LARGE_OUTPUT_NAMES):
        file_name = OUTPUTS[name].name
        spec = specs.get(file_name)
        if spec is None:
            raise ValueError(f"LARGE_ARTIFACTS.json does not record {file_name}")
        if spec.get("builderOutput") != name:
            raise ValueError(f"LARGE_ARTIFACTS.json maps {file_name} to the wrong output")
        by_output[name] = spec
    if set(specs) != {OUTPUTS[name].name for name in LARGE_OUTPUT_NAMES}:
        raise ValueError("LARGE_ARTIFACTS.json records files that the builder does not write")
    return by_output


def verify_large_artifacts(
    directory: Path = LARGE_DIR, *, require: bool = False
) -> dict[str, str]:
    """Verify every large artifact present in ``directory`` against its recorded hash.

    Returns ``{file name: "verified" | "absent"}``. A present file with the wrong size
    or SHA-256 always raises :class:`LargeArtifactError`; an absent file raises only
    when ``require`` is true.
    """

    states: dict[str, str] = {}
    for spec in large_artifact_specs().values():
        path = directory / spec["name"]
        if not path.exists():
            if require:
                raise LargeArtifactError(
                    f"Large artifact {spec['name']} is missing from "
                    f"{display_path(directory)}; run `{FETCH_LARGE_COMMAND}`"
                )
            states[spec["name"]] = "absent"
            continue
        verify_large_artifact(path, spec)
        states[spec["name"]] = "verified"
    return states


def _verify_small_tracked_outputs(
    manifest: dict[str, Any], outputs: dict[str, bytes]
) -> dict[str, Any]:
    """Cross-check the committed QA, node/edge CSVs and narrative; return the QA."""

    qa: dict[str, Any] = json.loads(outputs["qa"])
    if qa.get("status") != "pass" or not all(qa.get("checks", {}).values()):
        raise ValueError("Tracked citation QA is not closed and passing")
    if qa.get("builtFromRetrievalWindowCompletedAt") != manifest.get(
        "retrievalWindow", {}
    ).get("completedAt"):
        raise ValueError("Tracked citation QA is not bound to the retrieval window")
    metrics = qa.get("metrics", {})
    csv_expectations = {
        "nodes_csv": metrics.get("nodes"),
        "edges_csv": metrics.get("assertions"),
    }
    for name, expected in csv_expectations.items():
        rows = sum(
            1
            for _ in csv.DictReader(
                io.StringIO(outputs[name].decode("utf-8"), newline="")
            )
        )
        if rows != expected:
            raise ValueError(f"Tracked {name} row count does not reconcile with QA")
    if not outputs["markdown"].strip():
        raise ValueError("Tracked citation narrative is empty")
    return qa


def verify_tracked_bundle_without_large_artifacts() -> dict[str, bytes]:
    """Verify the committed citation outputs when the large artifacts are not fetched.

    This is the weakest mode: it checks the closed QA, its binding to the retrieval
    window, and that the committed node and edge CSVs have the row counts the QA
    records. The graph and the assertion CSV are not inspected.
    """

    manifest = read_json(MANIFEST_PATH)
    outputs: dict[str, bytes] = {}
    for name, path in OUTPUTS.items():
        if name in LARGE_OUTPUT_NAMES:
            continue
        if not path.is_file():
            raise FileNotFoundError(f"Tracked citation output is missing: {path}")
        outputs[name] = path.read_bytes()
    _verify_small_tracked_outputs(manifest, outputs)
    return outputs


def verify_tracked_bundle_without_private_raw() -> dict[str, bytes]:
    """Verify the public frozen bundle when licensed/private raw is unavailable.

    Needs the two large artifacts in ``generated/large/``; their size and SHA-256 are
    checked against ``generated/LARGE_ARTIFACTS.json`` before anything is parsed.
    """

    manifest = read_json(MANIFEST_PATH)
    if private_raw_availability(manifest) != "absent":
        raise ValueError("Clone-safe verification is only for an absent private raw cache")
    verify_large_artifacts(require=True)
    outputs: dict[str, bytes] = {}
    for name, path in OUTPUTS.items():
        if not path.is_file():
            raise FileNotFoundError(f"Tracked citation output is missing: {path}")
        outputs[name] = path.read_bytes()

    graph = json.loads(outputs["graph"])
    qa = _verify_small_tracked_outputs(manifest, outputs)
    counts = graph.get("counts", {})
    nodes = graph.get("nodes", [])
    assertions = graph.get("assertions", [])
    if counts.get("nodes") != len(nodes):
        raise ValueError("Tracked citation node count does not reconcile")
    if counts.get("assertedEdges") != len(assertions):
        raise ValueError("Tracked citation edge count does not reconcile")

    provenance = graph.get("provenance", {})
    if sha256_file(resolve_recorded_path(str(provenance.get("citationManifestPath")))) != (
        provenance.get("citationManifestSha256")
    ):
        raise ValueError("Tracked citation graph is not bound to the retrieval manifest")
    for item in provenance.get("corpusInputs", []):
        if sha256_file(resolve_recorded_path(str(item.get("path")))) != item.get("sha256"):
            raise ValueError(f"Tracked citation graph corpus input changed: {item.get('path')}")

    provider_evidence_count = 0
    for assertion in assertions:
        provider_evidence = assertion.get("providerEvidence")
        if not isinstance(provider_evidence, list) or not provider_evidence:
            raise ValueError("Tracked citation assertion has no provider evidence")
        provider_evidence_count += len(provider_evidence)
        for evidence in provider_evidence:
            missing = REQUIRED_EVIDENCE - set(evidence)
            if missing:
                raise ValueError(
                    "Tracked provider evidence is incomplete: " + ", ".join(sorted(missing))
                )
            relative = str(evidence["rawPrivatePath"])
            if not relative.startswith("private_data/") or ".." in PurePosixPath(
                relative
            ).parts:
                raise ValueError("Tracked provider evidence escapes private_data")
            if re.fullmatch(r"[0-9a-f]{64}", str(evidence["rawSha256"])) is None:
                raise ValueError("Tracked provider evidence has an invalid SHA-256")
    if provider_evidence_count != qa.get("metrics", {}).get("providerEvidenceEntries"):
        raise ValueError("Tracked provider-evidence count does not reconcile")

    csv_expectations = {
        "nodes_csv": len(nodes),
        "edges_csv": len(assertions),
        "assertions_csv": len(assertions),
    }
    for name, expected in csv_expectations.items():
        rows = sum(
            1
            for _ in csv.DictReader(
                io.StringIO(outputs[name].decode("utf-8"), newline="")
            )
        )
        if rows != expected:
            raise ValueError(f"Tracked {name} row count does not reconcile")
    return outputs


def provider_records(manifest: dict[str, Any], key: str) -> list[dict[str, Any]]:
    records = manifest.get("records", {}).get(key, [])
    if not isinstance(records, list):
        raise ValueError(f"Provider record ledger is not a list: {key}")
    return records


def seed_work(record: dict[str, Any], raw: RawReader) -> dict[str, Any]:
    evidence = record["seedEvidence"]
    payload = raw.read(evidence)
    pointer = str(evidence.get("providerRecordJsonPointer", ""))
    if pointer:
        match = re.fullmatch(r"\$\.results\[(\d+)\]", pointer)
        if not match:
            raise ValueError(f"Unsupported seed JSON pointer: {pointer}")
        work = payload["results"][int(match.group(1))]
    else:
        work = payload
    if not isinstance(work, dict) or str(work.get("id")) != str(record["openAlexId"]):
        raise ValueError(f"Seed raw record mismatch: {record['persistentId']}")
    return work


def short_oa(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1]


def oa_id(value: str) -> str:
    return f"https://openalex.org/{short_oa(value)}"


def doi_alias(value: Any) -> str | None:
    if not value:
        return None
    normalized = normalize_doi(str(value))
    return f"doi:{normalized}" if normalized else None


def arxiv_alias(value: Any) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().lower().removeprefix("arxiv:")
    return f"arxiv:{normalized}" if normalized else None


def work_aliases(work: dict[str, Any]) -> list[str]:
    aliases = [f"oa:{oa_id(str(work['id']))}"]
    doi = doi_alias(work.get("doi") or (work.get("ids") or {}).get("doi"))
    if doi:
        aliases.append(doi)
    ids = work.get("ids") or {}
    arxiv = arxiv_alias(ids.get("arxiv"))
    if arxiv:
        aliases.append(arxiv)
    return aliases


def s2_aliases(item: dict[str, Any]) -> list[str]:
    paper_id = item.get("paperId")
    aliases = [f"s2:{paper_id}"] if paper_id else []
    external = item.get("externalIds") or {}
    doi = doi_alias(external.get("DOI"))
    arxiv = arxiv_alias(external.get("ArXiv"))
    if doi:
        aliases.append(doi)
    if arxiv:
        aliases.append(arxiv)
    return aliases


def compact_evidence(
    provider: str,
    base: dict[str, Any],
    source_provider_id: str,
    target_provider_id: str,
    *,
    provider_field: str,
    json_pointer: str,
    retrieval_direction: str,
    support_boundary: str = "provider-declared-citation",
) -> dict[str, Any]:
    evidence = {
        "provider": provider,
        "sourceProviderId": source_provider_id,
        "targetProviderId": target_provider_id,
        "providerField": provider_field,
        "rawJsonPointer": json_pointer,
        "retrievalDirection": retrieval_direction,
        "supportBoundary": support_boundary,
    }
    for field in (
        "endpoint",
        "canonicalQuery",
        "requestUrl",
        "pageNumber",
        "offset",
        "cursor",
        "retrievedAt",
        "httpStatus",
        "rateLimitHeaders",
        "rawPrivatePath",
        "rawSha256",
        "parser",
        "parserVersion",
        "providerDocsUrl",
        "providerDataLicense",
        "providerLicenseUrl",
    ):
        evidence[field] = base.get(field)
    missing = REQUIRED_EVIDENCE - set(evidence)
    if missing or any(evidence.get(field) is None for field in REQUIRED_EVIDENCE - {"offset", "cursor"}):
        raise ValueError(f"Incomplete {provider} evidence: {sorted(missing)}")
    return evidence


class Registry:
    def __init__(self, seed_rows: dict[str, dict[str, Any]], corpus_index: dict[str, int]) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.alias_to_key: dict[str, str] = {}
        self.redirects: dict[str, str] = {}
        self.conflicts: list[dict[str, Any]] = []
        self.corpus_index = corpus_index
        for pid, row in seed_rows.items():
            item_class = "repository" if is_repository(row) else "paper-or-preprint"
            self.nodes[pid] = {
                "persistentId": pid,
                "corpusIndex": corpus_index[pid],
                "isSeed": True,
                "title": row["Title"],
                "publicationYear": row["Publication Year"],
                "itemClass": item_class,
                "openAlexId": None,
                "openAlexMembership": "unresolved",
                "xpacOnly": False,
                "providerIds": {"openalex": [], "semanticScholar": [], "crossref": []},
                "exactIdentifiers": [pid],
                "metadataProviders": ["corpus-truth"],
            }
            self.register_alias(f"pid:{pid.lower()}", pid, "corpus")
            doi = doi_from_persistent_id(pid)
            if doi:
                self.register_alias(f"doi:{normalize_doi(doi)}", pid, "corpus")
            if pid.startswith("arXiv:"):
                self.register_alias(f"arxiv:{pid.split(':', 1)[1].lower()}", pid, "corpus")

    def canonical(self, key: str) -> str:
        while key in self.redirects:
            key = self.redirects[key]
        return key

    def merge_exact_identifier_nodes(
        self, existing: str, candidate: str, alias: str, provider: str
    ) -> str:
        existing = self.canonical(existing)
        candidate = self.canonical(candidate)
        if existing == candidate:
            return existing
        existing_seed = bool(self.nodes.get(existing, {}).get("isSeed"))
        candidate_seed = bool(self.nodes.get(candidate, {}).get("isSeed"))
        if existing_seed and candidate_seed:
            self.conflicts.append(
                {
                    "kind": "seed-identifier-conflict",
                    "identifier": alias,
                    "existingNode": existing,
                    "candidateNode": candidate,
                    "provider": provider,
                    "resolution": "kept-separate-frozen-seeds; requires manual adjudication",
                }
            )
            return existing
        if candidate_seed:
            winner, loser = candidate, existing
        elif existing_seed:
            winner, loser = existing, candidate
        else:
            winner, loser = sorted((existing, candidate))
        winner_node = self.nodes[winner]
        loser_node = self.nodes[loser]
        for provider_key, values in loser_node["providerIds"].items():
            winner_node["providerIds"][provider_key].extend(values)
        winner_node["exactIdentifiers"].extend(loser_node["exactIdentifiers"])
        winner_node["metadataProviders"].extend(loser_node["metadataProviders"])
        if not winner_node.get("title"):
            winner_node["title"] = loser_node.get("title", "")
        if not winner_node.get("publicationYear"):
            winner_node["publicationYear"] = loser_node.get("publicationYear")
        if not winner_node.get("openAlexId") and loser_node.get("openAlexId"):
            winner_node["openAlexId"] = loser_node["openAlexId"]
            winner_node["openAlexMembership"] = loser_node["openAlexMembership"]
            winner_node["xpacOnly"] = loser_node["xpacOnly"]
        self.redirects[loser] = winner
        del self.nodes[loser]
        for known_alias, owner in list(self.alias_to_key.items()):
            if self.canonical(owner) == winner or owner == loser:
                self.alias_to_key[known_alias] = winner
        self.conflicts.append(
            {
                "kind": "exact-identifier-node-merge",
                "identifier": alias,
                "mergedNode": loser,
                "canonicalNode": winner,
                "provider": provider,
                "resolution": "merged by exact provider/DOI/arXiv bridge; title not used",
            }
        )
        return winner

    def register_alias(self, alias: str, key: str, provider: str) -> str:
        key = self.canonical(key)
        existing = self.alias_to_key.get(alias)
        if existing and self.canonical(existing) != key:
            key = self.merge_exact_identifier_nodes(existing, key, alias, provider)
        self.alias_to_key[alias] = key
        return self.canonical(key)

    def key_for_aliases(self, aliases: list[str], preferred: str) -> str:
        existing = sorted(
            {
                self.canonical(self.alias_to_key[value])
                for value in aliases
                if value in self.alias_to_key
            }
        )
        if len(existing) > 1:
            self.conflicts.append(
                {
                    "kind": "cross-provider-alias-conflict",
                    "aliases": aliases,
                    "candidateNodes": existing,
                    "resolution": "used-first-stable-node; no title merge",
                }
            )
        return existing[0] if existing else preferred

    def ensure_node(self, key: str, defaults: dict[str, Any]) -> dict[str, Any]:
        if key not in self.nodes:
            self.nodes[key] = {
                "persistentId": key,
                "corpusIndex": None,
                "isSeed": False,
                "title": "",
                "publicationYear": None,
                "itemClass": "external-work",
                "openAlexId": None,
                "openAlexMembership": "unresolved",
                "xpacOnly": False,
                "providerIds": {"openalex": [], "semanticScholar": [], "crossref": []},
                "exactIdentifiers": [],
                "metadataProviders": [],
                **defaults,
            }
        return self.nodes[key]

    def add_oa(self, work: dict[str, Any], force_key: str | None = None) -> str:
        aliases = work_aliases(work)
        preferred = f"OpenAlex:{short_oa(str(work['id']))}"
        key = force_key or self.key_for_aliases(aliases, preferred)
        node = self.ensure_node(key, {})
        provider_id = oa_id(str(work["id"]))
        if provider_id not in node["providerIds"]["openalex"]:
            node["providerIds"]["openalex"].append(provider_id)
        node["openAlexId"] = provider_id
        is_xpac = work.get("is_xpac")
        if isinstance(is_xpac, bool):
            node["openAlexMembership"] = "openalex-xpac" if is_xpac else "openalex-core"
            node["xpacOnly"] = is_xpac
        if not node.get("title"):
            node["title"] = str(work.get("title") or "")
        if not node.get("publicationYear"):
            node["publicationYear"] = work.get("publication_year")
        if "openalex" not in node["metadataProviders"]:
            node["metadataProviders"].append("openalex")
        for alias in aliases:
            self.register_alias(alias, key, "openalex")
            if alias not in node["exactIdentifiers"]:
                node["exactIdentifiers"].append(alias)
        return self.canonical(key)

    def add_dangling_oa(self, provider_id: str) -> str:
        normalized = oa_id(provider_id)
        alias = f"oa:{normalized}"
        key = self.alias_to_key.get(alias, f"OpenAlex:{short_oa(normalized)}")
        node = self.ensure_node(key, {})
        if normalized not in node["providerIds"]["openalex"]:
            node["providerIds"]["openalex"].append(normalized)
        node["openAlexId"] = normalized
        if alias not in node["exactIdentifiers"]:
            node["exactIdentifiers"].append(alias)
        self.register_alias(alias, key, "openalex-dangling-reference")
        return self.canonical(key)

    def add_s2(self, item: dict[str, Any], force_key: str | None = None) -> str:
        aliases = s2_aliases(item)
        paper_id = str(item.get("paperId") or "")
        preferred = f"S2:{paper_id}"
        key = force_key or self.key_for_aliases(aliases, preferred)
        node = self.ensure_node(key, {})
        if paper_id and paper_id not in node["providerIds"]["semanticScholar"]:
            node["providerIds"]["semanticScholar"].append(paper_id)
        if "semantic-scholar-minimal-id" not in node["metadataProviders"]:
            node["metadataProviders"].append("semantic-scholar-minimal-id")
        for alias in aliases:
            self.register_alias(alias, key, "semantic-scholar")
            if alias not in node["exactIdentifiers"]:
                node["exactIdentifiers"].append(alias)
        return self.canonical(key)

    def add_crossref_doi(self, doi: str) -> str:
        normalized = normalize_doi(doi)
        alias = f"doi:{normalized}"
        key = self.alias_to_key.get(alias, f"DOI:{normalized}")
        node = self.ensure_node(key, {})
        if normalized not in node["providerIds"]["crossref"]:
            node["providerIds"]["crossref"].append(normalized)
        if alias not in node["exactIdentifiers"]:
            node["exactIdentifiers"].append(alias)
        self.register_alias(alias, key, "crossref")
        return self.canonical(key)


def add_edge(
    edge_evidence: dict[tuple[str, str], list[dict[str, Any]]],
    conflicts: list[dict[str, Any]],
    source: str,
    target: str,
    evidence: dict[str, Any],
) -> None:
    if source == target:
        conflicts.append(
            {
                "kind": "self-loop-provider-record",
                "node": source,
                "provider": evidence["provider"],
                "resolution": "excluded from graph; retained as conflict",
                "rawPrivatePath": evidence["rawPrivatePath"],
            }
        )
        return
    key = (source, target)
    fingerprint = (
        evidence["provider"],
        evidence["rawSha256"],
        evidence["rawJsonPointer"],
        evidence["sourceProviderId"],
        evidence["targetProviderId"],
    )
    existing = {
        (
            value["provider"],
            value["rawSha256"],
            value["rawJsonPointer"],
            value["sourceProviderId"],
            value["targetProviderId"],
        )
        for value in edge_evidence.get(key, [])
    }
    if fingerprint not in existing:
        edge_evidence.setdefault(key, []).append(evidence)


def build() -> dict[str, bytes]:
    _, corpus_rows, corpus_provenance = load_corpus_rows()
    manifest = read_json(MANIFEST_PATH)
    if manifest.get("schemaVersion") != "cui-citation-retrieval-manifest-v2":
        raise ValueError("Citation artifact builder requires retrieval manifest v2")
    completed_at = str(manifest["retrievalWindow"]["completedAt"])
    if not completed_at or completed_at == "None":
        raise ValueError("Citation retrieval window has not been finalized")

    corpus_index = {
        normalize_persistent_id(str(row["Persistent ID"])): index
        for index, row in enumerate(corpus_rows, start=1)
    }
    row_by_pid = {
        normalize_persistent_id(str(row["Persistent ID"])): row for row in corpus_rows
    }
    paper_pids = [pid for pid, row in row_by_pid.items() if not is_repository(row)]
    if len(row_by_pid) != EXPECTED_TOTAL or len(paper_pids) != EXPECTED_PAPERS:
        raise ValueError("Citation build requires frozen 64-paper + 4-repository truth")

    raw = RawReader()
    registry = Registry(row_by_pid, corpus_index)
    conflicts = registry.conflicts
    oa_records = provider_records(manifest, "openalex")
    s2_records = provider_records(manifest, "semanticScholar")
    crossref_records = provider_records(manifest, "crossref")
    if len(oa_records) != EXPECTED_PAPERS or len(s2_records) != EXPECTED_PAPERS:
        raise ValueError("OpenAlex and S2 ledgers must each contain all 64 seed rows")

    oa_seed_work: dict[str, dict[str, Any]] = {}
    oa_seed_pid_by_id: dict[str, str] = {}
    all_oa_works: dict[str, dict[str, Any]] = {}
    for record in oa_records:
        if record.get("status") != "resolved":
            continue
        pid = normalize_persistent_id(str(record["persistentId"]))
        work = seed_work(record, raw)
        provider_id = oa_id(str(work["id"]))
        oa_seed_work[pid] = work
        oa_seed_pid_by_id[provider_id] = pid
        all_oa_works[provider_id] = work
        registry.add_oa(work, force_key=pid)

    for record in oa_records:
        for page in record.get("directions", {}).get("incoming", {}).get("pages", []):
            payload = raw.read(page)
            for work in payload.get("results", []):
                if isinstance(work, dict) and work.get("id"):
                    all_oa_works[oa_id(str(work["id"]))] = work
    metadata_state = manifest.get("records", {}).get("openalexExternalMetadata", {})
    for page in metadata_state.get("pages", []):
        if page.get("failure") or not page.get("rawPrivatePath"):
            continue
        payload = raw.read(page)
        for work in payload.get("results", []):
            if isinstance(work, dict) and work.get("id"):
                all_oa_works[oa_id(str(work["id"]))] = work
    for followup in metadata_state.get("singletonFollowups", []):
        evidence = followup.get("evidence")
        if followup.get("status") != "resolved" or not isinstance(evidence, dict):
            continue
        work = raw.read(evidence)
        if isinstance(work, dict) and work.get("id"):
            all_oa_works[oa_id(str(work["id"]))] = work
    for provider_id in sorted(all_oa_works):
        work = all_oa_works[provider_id]
        registry.add_oa(work, force_key=oa_seed_pid_by_id.get(provider_id))

    referenced_ids: set[str] = set()
    for work in oa_seed_work.values():
        referenced_ids.update(oa_id(str(value)) for value in work.get("referenced_works") or [])
    for provider_id in sorted(referenced_ids - set(all_oa_works)):
        registry.add_dangling_oa(provider_id)

    s2_seed_pid_by_id: dict[str, str] = {}
    for record in s2_records:
        if record.get("status") != "resolved" or not record.get("semanticScholarPaperId"):
            continue
        pid = normalize_persistent_id(str(record["persistentId"]))
        paper_id = str(record["semanticScholarPaperId"])
        s2_seed_pid_by_id[paper_id] = pid
        registry.add_s2({"paperId": paper_id, "externalIds": {}}, force_key=pid)

    edge_evidence: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in oa_records:
        if record.get("status") != "resolved":
            continue
        pid = normalize_persistent_id(str(record["persistentId"]))
        work = oa_seed_work[pid]
        seed_base = record["seedEvidence"]
        pointer_prefix = str(seed_base.get("providerRecordJsonPointer") or "$")
        for index, target_id in enumerate(work.get("referenced_works") or []):
            target_provider_id = oa_id(str(target_id))
            target_key = registry.alias_to_key[f"oa:{target_provider_id}"]
            evidence = compact_evidence(
                "openalex",
                seed_base,
                oa_id(str(work["id"])),
                target_provider_id,
                provider_field="referenced_works",
                json_pointer=f"{pointer_prefix}.referenced_works[{index}]",
                retrieval_direction="outgoing",
            )
            add_edge(edge_evidence, conflicts, pid, target_key, evidence)

        for page in record.get("directions", {}).get("incoming", {}).get("pages", []):
            payload = raw.read(page)
            for index, citing_work in enumerate(payload.get("results", [])):
                if not isinstance(citing_work, dict) or not citing_work.get("id"):
                    continue
                citing_id = oa_id(str(citing_work["id"]))
                source_key = registry.alias_to_key[f"oa:{citing_id}"]
                evidence = compact_evidence(
                    "openalex",
                    page,
                    citing_id,
                    oa_id(str(work["id"])),
                    provider_field="filter=cites",
                    json_pointer=f"$.results[{index}].id",
                    retrieval_direction="incoming",
                )
                add_edge(edge_evidence, conflicts, source_key, pid, evidence)

    for record in s2_records:
        if record.get("status") != "resolved":
            continue
        seed_pid = normalize_persistent_id(str(record["persistentId"]))
        seed_s2_id = str(record["semanticScholarPaperId"])
        for direction, nested_key in (("citations", "citingPaper"), ("references", "citedPaper")):
            for page in record.get("directions", {}).get(direction, {}).get("pages", []):
                payload = raw.read(page)
                values = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(values, list):
                    continue
                for index, relation in enumerate(values):
                    item = relation.get(nested_key) if isinstance(relation, dict) else None
                    if not isinstance(item, dict) or not item.get("paperId"):
                        continue
                    paper_id = str(item["paperId"])
                    other_key = registry.add_s2(item, force_key=s2_seed_pid_by_id.get(paper_id))
                    if direction == "citations":
                        source_key, target_key = other_key, seed_pid
                        source_provider_id, target_provider_id = paper_id, seed_s2_id
                    else:
                        source_key, target_key = seed_pid, other_key
                        source_provider_id, target_provider_id = seed_s2_id, paper_id
                    evidence = compact_evidence(
                        "semantic-scholar",
                        page,
                        source_provider_id,
                        target_provider_id,
                        provider_field=nested_key,
                        json_pointer=f"$.data[{index}].{nested_key}.paperId",
                        retrieval_direction=direction,
                        support_boundary="auxiliary-coverage-sensitivity; public output retains minimal IDs only",
                    )
                    add_edge(edge_evidence, conflicts, source_key, target_key, evidence)

    for record in crossref_records:
        if record.get("status") != "resolved":
            continue
        seed_pid = normalize_persistent_id(str(record["persistentId"]))
        evidence_base = record["evidence"]
        payload = raw.read(evidence_base)
        message = payload.get("message") or {}
        source_doi = normalize_doi(str(message.get("DOI") or record.get("doi") or ""))
        for index, reference in enumerate(message.get("reference") or []):
            if not isinstance(reference, dict) or not reference.get("DOI"):
                continue
            target_doi = normalize_doi(str(reference["DOI"]))
            target_key = registry.add_crossref_doi(target_doi)
            evidence = compact_evidence(
                "crossref",
                evidence_base,
                source_doi,
                target_doi,
                provider_field="message.reference[].DOI",
                json_pointer=f"$.message.reference[{index}].DOI",
                retrieval_direction="outgoing",
                support_boundary="exact DOI deposited-reference candidate; Crossref bibliography completeness is not assumed",
            )
            add_edge(edge_evidence, conflicts, seed_pid, target_key, evidence)

    canonical_edge_evidence: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (source, target), evidence_values in edge_evidence.items():
        canonical_source = registry.canonical(source)
        canonical_target = registry.canonical(target)
        for evidence in evidence_values:
            add_edge(
                canonical_edge_evidence,
                conflicts,
                canonical_source,
                canonical_target,
                evidence,
            )
    edge_evidence = canonical_edge_evidence

    ordered_keys = sorted(
        registry.nodes,
        key=lambda key: (
            0 if registry.nodes[key]["isSeed"] else 1,
            registry.nodes[key]["corpusIndex"] or 10**9,
            key.lower(),
        ),
    )
    nodes: list[dict[str, Any]] = []
    node_by_key: dict[str, dict[str, Any]] = {}
    for index, key in enumerate(ordered_keys, start=1):
        node = registry.nodes[key]
        node["nodeId"] = f"CUI-CITE-NODE-{index:06d}"
        node["providerIds"] = {
            provider: sorted(set(values)) for provider, values in node["providerIds"].items()
        }
        node["exactIdentifiers"] = sorted(set(node["exactIdentifiers"]))
        node["metadataProviders"] = sorted(set(node["metadataProviders"]))
        node["citationCountsAreSnapshotAt"] = completed_at
        if node["openAlexMembership"] not in ALLOWED_XPAC:
            raise ValueError(f"Invalid XPAC membership: {node['openAlexMembership']}")
        nodes.append(node)
        node_by_key[key] = node

    assertions: list[dict[str, Any]] = []
    provider_edge_sets: dict[str, set[tuple[str, str]]] = {
        "openalex": set(),
        "semantic-scholar": set(),
        "crossref": set(),
    }
    for edge_index, (source, target) in enumerate(sorted(edge_evidence), start=1):
        evidence = sorted(
            edge_evidence[(source, target)],
            key=lambda item: (
                item["provider"],
                item["rawPrivatePath"],
                item["rawJsonPointer"],
            ),
        )
        providers = sorted({str(item["provider"]) for item in evidence})
        for provider in providers:
            provider_edge_sets[provider].add((source, target))
        source_node = node_by_key[source]
        target_node = node_by_key[target]
        assertions.append(
            {
                "assertionId": f"CUI-CITE-{edge_index:06d}",
                "predicate": "cites",
                "subjectPersistentId": source,
                "subjectTitle": source_node["title"],
                "objectPersistentId": target,
                "objectTitle": target_node["title"],
                "providers": providers,
                "confidence": "multi-provider" if len(providers) > 1 else "single-provider",
                "assertionMethod": "provider-declared-exact-identifiers-only",
                "providerEvidence": evidence,
                "sourceOpenAlexMembership": source_node["openAlexMembership"],
                "targetOpenAlexMembership": target_node["openAlexMembership"],
                "xpacOnlyInvolved": bool(source_node["xpacOnly"] or target_node["xpacOnly"]),
                "sourceIsSeed": source_node["isSeed"],
                "targetIsSeed": target_node["isSeed"],
                "retrievalWindowCompletedAt": completed_at,
            }
        )

    overlaps: list[dict[str, Any]] = []
    for left, right in itertools.combinations(sorted(provider_edge_sets), 2):
        intersection = provider_edge_sets[left] & provider_edge_sets[right]
        union = provider_edge_sets[left] | provider_edge_sets[right]
        overlaps.append(
            {
                "providerA": left,
                "providerB": right,
                "edgesA": len(provider_edge_sets[left]),
                "edgesB": len(provider_edge_sets[right]),
                "intersection": len(intersection),
                "union": len(union),
                "jaccard": round(len(intersection) / len(union), 8) if union else 1.0,
            }
        )

    reverse_pairs = sorted(
        {
            tuple(sorted((source, target)))
            for source, target in edge_evidence
            if (target, source) in edge_evidence and source != target
        }
    )
    for left, right in reverse_pairs:
        conflicts.append(
            {
                "kind": "reciprocal-provider-declared-citations",
                "nodeA": left,
                "nodeB": right,
                "resolution": "retained as two directed edges; not collapsed",
            }
        )

    oa_resolved = sum(record.get("status") == "resolved" for record in oa_records)
    oa_incoming_complete = sum(
        record.get("directions", {}).get("incoming", {}).get("status")
        in {"complete", "complete-zero-provider-count"}
        for record in oa_records
        if record.get("status") == "resolved"
    )
    oa_outgoing_complete = sum(
        record.get("directions", {}).get("outgoing", {}).get("status") == "complete"
        for record in oa_records
        if record.get("status") == "resolved"
    )
    s2_resolved = sum(record.get("status") == "resolved" for record in s2_records)
    s2_complete = {
        direction: sum(
            record.get("directions", {}).get(direction, {}).get("status") == "complete"
            for record in s2_records
            if record.get("status") == "resolved"
        )
        for direction in ("citations", "references")
    }
    complete_directions = [
        direction
        for record in oa_records
        if record.get("status") == "resolved"
        for direction in (record["directions"]["incoming"], record["directions"]["outgoing"])
    ] + [
        direction
        for record in s2_records
        if record.get("status") == "resolved"
        for direction in (record["directions"]["citations"], record["directions"]["references"])
    ]
    direction_status_counts: dict[str, dict[str, dict[str, int]]] = {}
    partial_directions: list[dict[str, Any]] = []
    blocked_directions: list[dict[str, Any]] = []
    for provider, records, directions in (
        ("openalex", oa_records, ("incoming", "outgoing")),
        ("semantic-scholar", s2_records, ("citations", "references")),
    ):
        direction_status_counts[provider] = {}
        for direction in directions:
            counts = {"complete": 0, "partial": 0, "blocked": 0, "total": len(records)}
            for record in records:
                state = record.get("directions", {}).get(direction, {})
                status = str(state.get("status") or "")
                if status in {"complete", "complete-zero-provider-count"}:
                    bucket = "complete"
                elif status == "count-mismatch" or status.startswith("partial"):
                    bucket = "partial"
                else:
                    bucket = "blocked"
                counts[bucket] += 1
                if bucket == "complete":
                    continue
                item = {
                    "provider": provider,
                    "persistentId": normalize_persistent_id(str(record["persistentId"])),
                    "direction": direction,
                    "state": state,
                }
                (partial_directions if bucket == "partial" else blocked_directions).append(item)
            direction_status_counts[provider][direction] = counts
    incomplete_directions = partial_directions + blocked_directions
    unhandled_429 = [
        item
        for item in incomplete_directions
        if item["state"].get("failure", {}).get("httpStatus") == 429
        and not str(item["state"].get("status", "")).startswith("blocked")
    ]
    raw_evidence = [item for assertion in assertions for item in assertion["providerEvidence"]]
    evidence_fields_ok = all(REQUIRED_EVIDENCE.issubset(item) for item in raw_evidence)
    count_reconciled = all(
        int(direction.get("retrievedCount") or 0) == int(direction.get("expectedCount") or 0)
        for direction in complete_directions
        if direction.get("status") == "complete"
    )
    external_nodes = sum(not node["isSeed"] for node in nodes)
    xpac_nodes = sum(node["openAlexMembership"] == "openalex-xpac" for node in nodes)
    internal_edges = sum(assertion["sourceIsSeed"] and assertion["targetIsSeed"] for assertion in assertions)
    external_involved_edges = len(assertions) - internal_edges
    multi_provider = sum(assertion["confidence"] == "multi-provider" for assertion in assertions)
    checks = {
        "seedNodesExactly68": sum(node["isSeed"] for node in nodes) == EXPECTED_TOTAL,
        "paperSeedsExactly64": sum(node["isSeed"] and node["itemClass"] == "paper-or-preprint" for node in nodes) == EXPECTED_PAPERS,
        "repositorySeedsExactly4": sum(node["isSeed"] and node["itemClass"] == "repository" for node in nodes) == EXPECTED_REPOSITORIES,
        "externalWorksMaterialized": sum(not node["isSeed"] for node in nodes) > 0,
        "openAlexResolvedDirectionsExhaustedOrExplicit": all(
            sum(direction_status_counts["openalex"][direction][bucket] for bucket in ("complete", "partial", "blocked"))
            == len(oa_records)
            for direction in ("incoming", "outgoing")
        ),
        "semanticScholarDirectionsExhaustedOrExplicit": all(
            sum(direction_status_counts["semantic-scholar"][direction][bucket] for bucket in ("complete", "partial", "blocked"))
            == len(s2_records)
            for direction in ("citations", "references")
        ),
        "directionStatusCountsReconcile": all(
            sum(counts[bucket] for bucket in ("complete", "partial", "blocked")) == counts["total"] == EXPECTED_PAPERS
            for provider_counts in direction_status_counts.values()
            for counts in provider_counts.values()
        ),
        "completeDirectionCountsReconcile": count_reconciled,
        "noUnhandled429": not unhandled_429,
        "noSelfLoops": all(assertion["subjectPersistentId"] != assertion["objectPersistentId"] for assertion in assertions),
        "edgeDirectionFixedCitingToCited": all(assertion["predicate"] == "cites" for assertion in assertions),
        "noTitleOnlyMerge": manifest["providerPolicy"]["mergePolicy"].startswith("Exact provider IDs"),
        "everyAssertionHasCompleteProviderEvidence": evidence_fields_ok and all(assertion["providerEvidence"] for assertion in assertions),
        "everyRawHashReverified": all((item["rawPrivatePath"], item["rawSha256"]) in raw.verified for item in raw_evidence),
        "xpacMembershipClosed": all(node["openAlexMembership"] in ALLOWED_XPAC for node in nodes),
        "xpacIsNotRelevance": manifest["providerPolicy"]["xpacPolicy"].endswith("XPAC is not a relevance taxonomy."),
        "providerOverlapAndJaccardPresent": len(overlaps) == 3 and all("jaccard" in item for item in overlaps),
        "partialOrCompleteWindowExplicit": manifest["retrievalWindow"]["status"] in {"partial", "complete"},
        "rawFrozenRebuildBasis": bool(raw.verified),
        "assertedEdgePartitionReconciles": internal_edges + external_involved_edges == len(assertions),
    }
    if not all(checks.values()):
        raise ValueError(f"Citation artifact QA failed: {[key for key, value in checks.items() if not value]}")

    graph = {
        "schemaVersion": "cui-citation-graph-v2",
        "builtFromRetrievalWindow": manifest["retrievalWindow"],
        "coverage": {
            "overallStatus": manifest["retrievalWindow"]["status"],
            "openAlexPrimary": {
                "resolvedSeeds": oa_resolved,
                "requestedSeeds": EXPECTED_PAPERS,
                "incomingCompleteSeeds": oa_incoming_complete,
                "outgoingCompleteSeeds": oa_outgoing_complete,
                "externalMetadata": metadata_state,
                "unresolvedSeeds": [
                    {"persistentId": record["persistentId"], "status": record["status"]}
                    for record in oa_records
                    if record.get("status") != "resolved"
                ],
            },
            "semanticScholarSensitivity": {
                "resolvedSeeds": s2_resolved,
                "requestedSeeds": EXPECTED_PAPERS,
                "completeCitationLists": s2_complete["citations"],
                "completeReferenceLists": s2_complete["references"],
                "publicBoundary": manifest["providers"]["semanticScholar"].get("publicBoundary"),
            },
            "crossrefLimited": manifest["providers"]["crossref"],
            "directionStatusCounts": direction_status_counts,
            "partialDirections": partial_directions,
            "blockedDirections": blocked_directions,
        },
        "xpacSemantics": {
            "meaning": "OpenAlex expanded-record membership, not a relevance taxonomy.",
            "labels": ["openalex-core", "openalex-xpac", "unresolved"],
            "xpacOnlyNodes": xpac_nodes,
            "classificationRule": manifest["providerPolicy"]["xpacPolicy"],
            "relevanceInference": "none",
        },
        "edgePolicy": {
            "direction": "citing -> cited",
            "included": "Exact provider-declared OpenAlex, Semantic Scholar, and exact-DOI Crossref relations; external works are retained.",
            "excluded": "Title-only similarity, related_works, topics, authors, XPAC membership, and Crossref references without an exact DOI.",
            "crossrefBoundary": manifest["providers"]["crossref"].get("candidateBoundary"),
        },
        "counts": {
            "nodes": len(nodes),
            "seedNodes": EXPECTED_TOTAL,
            "externalNodes": external_nodes,
            "assertedEdges": len(assertions),
            "seedToSeedEdges": internal_edges,
            "externalInvolvedEdges": external_involved_edges,
            "multiProviderEdges": multi_provider,
            "xpacOnlyNodes": xpac_nodes,
            "conflicts": len(conflicts),
        },
        "providerOverlap": overlaps,
        "conflicts": sorted(conflicts, key=lambda item: json.dumps(item, sort_keys=True)),
        "provenance": {
            "citationManifestPath": recorded_path(MANIFEST_PATH),
            "citationManifestSha256": sha256_file(MANIFEST_PATH),
            "corpusInputs": corpus_provenance["inputs"],
            "rawResponsesReverified": len(raw.verified),
            "rebuildPolicy": "Network-free deterministic parse of exact frozen raw bytes; build twice must be byte-identical.",
        },
        "nodes": nodes,
        "assertions": assertions,
    }
    qa = {
        "schemaVersion": "cui-citation-qa-v2",
        "builtFromRetrievalWindowCompletedAt": completed_at,
        "status": "pass",
        "checks": checks,
        "metrics": {
            "nodes": len(nodes),
            "seedNodes": EXPECTED_TOTAL,
            "externalNodes": external_nodes,
            "assertions": len(assertions),
            "seedToSeedEdges": internal_edges,
            "externalInvolvedEdges": external_involved_edges,
            "providerEvidenceEntries": len(raw_evidence),
            "verifiedRawResponses": len(raw.verified),
            "openAlexResolvedSeeds": oa_resolved,
            "openAlexIncomingCompleteSeeds": oa_incoming_complete,
            "openAlexOutgoingCompleteSeeds": oa_outgoing_complete,
            "semanticScholarResolvedSeeds": s2_resolved,
            "semanticScholarCompleteCitationLists": s2_complete["citations"],
            "semanticScholarCompleteReferenceLists": s2_complete["references"],
            "completeDirections": sum(
                counts["complete"]
                for provider_counts in direction_status_counts.values()
                for counts in provider_counts.values()
            ),
            "partialDirections": len(partial_directions),
            "blockedDirections": len(blocked_directions),
            "unhandled429": len(unhandled_429),
            "xpacOnlyNodes": xpac_nodes,
            "conflicts": len(conflicts),
        },
        "providerOverlap": overlaps,
        "directionStatusCounts": direction_status_counts,
        "partialDirections": partial_directions,
        "blockedDirections": blocked_directions,
    }

    node_fields = [
        "nodeId", "persistentId", "corpusIndex", "isSeed", "title", "publicationYear",
        "itemClass", "openAlexId", "openAlexMembership", "xpacOnly", "providerIds",
        "exactIdentifiers", "metadataProviders", "citationCountsAreSnapshotAt",
    ]
    edge_fields = [
        "assertionId", "subjectPersistentId", "objectPersistentId", "predicate",
        "providers", "confidence", "sourceIsSeed", "targetIsSeed",
        "sourceOpenAlexMembership", "targetOpenAlexMembership", "xpacOnlyInvolved",
        "retrievalWindowCompletedAt",
    ]
    assertion_fields = edge_fields + [
        "subjectTitle", "objectTitle", "assertionMethod", "providerEvidence",
    ]
    unresolved_oa = graph["coverage"]["openAlexPrimary"]["unresolvedSeeds"]
    # The "Rebuild" section below names the source repository's script path. It is part
    # of the hash-bound generated bytes, so it stays as recorded; the current command is
    # ``python -m research.literature.scripts.build_citation_artifacts --check``.
    unresolved_lines = [
        f"- `{item['persistentId']}` — `{item['status']}`" for item in unresolved_oa
    ] or ["- None."]
    markdown = "\n".join(
        [
            "# Frozen External Citation Graph",
            "",
            f"Retrieval window: `{manifest['retrievalWindow']['startedAt']}` to `{completed_at}` UTC. Overall status: **{manifest['retrievalWindow']['status']}**.",
            "",
            "## Coverage",
            "",
            f"- Nodes: {len(nodes)} = {EXPECTED_TOTAL} frozen seeds + {external_nodes} exact provider-linked external works.",
            f"- Directed citing→cited edges: {len(assertions)} total assertions; seed-to-seed: {internal_edges}; external-involved: {external_involved_edges}; multi-provider: {multi_provider}.",
            f"- OpenAlex: {oa_resolved}/{EXPECTED_PAPERS} seeds resolved; {oa_incoming_complete}/{oa_resolved} resolved inbound cursors exhausted; {oa_outgoing_complete}/{oa_resolved} complete `referenced_works` arrays.",
            f"- Semantic Scholar sensitivity: {s2_resolved}/{EXPECTED_PAPERS} seeds resolved; citations {s2_complete['citations']}/{s2_resolved}, references {s2_complete['references']}/{s2_resolved} fully paged.",
            "- Direction status (complete / partial / blocked, denominator 64 each): "
            f"OpenAlex incoming {direction_status_counts['openalex']['incoming']['complete']} / {direction_status_counts['openalex']['incoming']['partial']} / {direction_status_counts['openalex']['incoming']['blocked']}; "
            f"OpenAlex outgoing {direction_status_counts['openalex']['outgoing']['complete']} / {direction_status_counts['openalex']['outgoing']['partial']} / {direction_status_counts['openalex']['outgoing']['blocked']}; "
            f"Semantic Scholar citations {direction_status_counts['semantic-scholar']['citations']['complete']} / {direction_status_counts['semantic-scholar']['citations']['partial']} / {direction_status_counts['semantic-scholar']['citations']['blocked']}; "
            f"Semantic Scholar references {direction_status_counts['semantic-scholar']['references']['complete']} / {direction_status_counts['semantic-scholar']['references']['partial']} / {direction_status_counts['semantic-scholar']['references']['blocked']}.",
            f"- Crossref: {manifest['providers']['crossref'].get('resolvedDoiSeeds', 0)}/{manifest['providers']['crossref'].get('requestedDoiSeeds', 0)} DOI seeds in its intentionally limited scope.",
            f"- XPAC-only nodes: {xpac_nodes}. XPAC is an OpenAlex collection marker, never a relevance tier.",
            "",
            "## OpenAlex unresolved seed ledger",
            "",
            *unresolved_lines,
            "",
            "## Audit boundaries",
            "",
            f"- Explicit partial seed-directions: {len(partial_directions)}; blocked seed-directions: {len(blocked_directions)}; unhandled HTTP 429 responses: {len(unhandled_429)}.",
            f"- OpenAlex dangling referenced-work metadata IDs: {len(metadata_state.get('unresolvedMetadataIds', []))}; their exact W identifiers remain nodes with unresolved membership.",
            "- Semantic Scholar tracked outputs expose minimal IDs/support only; exact payloads remain ignored under `private_data/`.",
            "- Crossref exact DOI references are deposited-reference candidates/support, not a claim that Crossref bibliographies are exhaustive.",
            "- Node merging uses exact provider IDs, DOI, or arXiv only. Titles, topics, authors, and XPAC membership never merge nodes or create edges.",
            "",
            "## Rebuild",
            "",
            "With the ignored private raw window available, `python literature/scripts/build_citation_artifacts.py` rebuilds without network access and `--check` verifies byte identity. In a clean clone without licensed/private raw, `--check` instead verifies the tracked graph/CSV/QA cross-contract and reports that clone-safe mode explicitly.",
            "",
        ]
    )
    return {
        "graph": json_bytes(graph),
        "nodes_csv": csv_bytes(node_fields, nodes),
        "edges_csv": csv_bytes(edge_fields, assertions),
        "assertions_csv": csv_bytes(assertion_fields, assertions),
        "markdown": markdown.encode("utf-8"),
        "qa": json_bytes(qa),
    }


def _output_summary(outputs: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    return {
        OUTPUTS[name].relative_to(REPO_ROOT).as_posix(): {
            "bytes": len(content),
            "sha256": sha256_bytes(content),
        }
        for name, content in outputs.items()
    }


def write_or_check(
    outputs: dict[str, bytes], check: bool, *, require_large: bool = False
) -> int:
    """Write rebuilt outputs, or compare them byte for byte with the files on disk.

    In check mode a large output that has not been fetched is compared with its
    recorded size and SHA-256 in LARGE_ARTIFACTS.json instead (or reported as a
    mismatch when ``require_large`` is set).
    """

    mismatches: list[str] = []
    large_states: dict[str, str] = {}
    specs = large_artifact_specs()
    for name, content in outputs.items():
        path = OUTPUTS[name]
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            continue
        if name in LARGE_OUTPUT_NAMES:
            spec = specs[name]
            if len(content) != spec["bytes"] or sha256_bytes(content) != spec["sha256"]:
                mismatches.append(f"{path.name} (rebuild differs from LARGE_ARTIFACTS.json)")
                continue
            if not path.exists():
                large_states[path.name] = "absent-rebuild-matches-recorded-hash"
                if require_large:
                    mismatches.append(path.relative_to(REPO_ROOT).as_posix())
                continue
            large_states[path.name] = "verified"
        if not path.exists() or path.read_bytes() != content:
            mismatches.append(path.relative_to(REPO_ROOT).as_posix())
    if mismatches:
        print(json.dumps({"status": "drift", "mismatches": mismatches}, sort_keys=True))
        return 2
    summary: dict[str, Any] = {
        "status": "pass" if check else "built",
        "outputs": _output_summary(outputs),
    }
    if check:
        summary["mode"] = "byte-identical-rebuild-from-private-raw"
        summary["largeArtifacts"] = large_states
    print(json.dumps(summary, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify instead of writing (see the README for the three check modes)",
    )
    parser.add_argument(
        "--require-large",
        action="store_true",
        help=(
            "With --check, fail when the large artifacts have not been fetched "
            f"into generated/large/ (`{FETCH_LARGE_COMMAND}`)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.require_large and not args.check:
        build_parser().error("--require-large is only valid with --check")
    if args.check:
        try:
            large_states = verify_large_artifacts(require=args.require_large)
        except LargeArtifactError as exc:
            print(json.dumps({"status": "fail", "error": str(exc)}, sort_keys=True))
            return 2
        if private_raw_availability() == "absent":
            if all(state == "verified" for state in large_states.values()):
                outputs = verify_tracked_bundle_without_private_raw()
                mode = "tracked-bundle-with-private-raw-absent"
            else:
                outputs = verify_tracked_bundle_without_large_artifacts()
                mode = "tracked-small-files-only-large-artifacts-absent"
            summary: dict[str, Any] = {
                "status": "pass",
                "mode": mode,
                "private_raw_reverified": False,
                "largeArtifacts": large_states,
                "outputs": _output_summary(outputs),
            }
            if mode != "tracked-bundle-with-private-raw-absent":
                summary["note"] = (
                    "The citation graph and assertion CSV were not checked; "
                    f"run `{FETCH_LARGE_COMMAND}` for the full clone-safe check"
                )
            print(json.dumps(summary, sort_keys=True))
            return 0
    return write_or_check(build(), args.check, require_large=args.require_large)


if __name__ == "__main__":
    raise SystemExit(main())
