"""Retrieve an auditable, resumable citation expansion around the 64 paper seeds.

OpenAlex is the CC0 primary graph.  For every resolved seed this collector reads
the complete ``referenced_works`` array and exhausts the ``cites:W...`` cursor.
Semantic Scholar is a coverage-sensitivity layer collected from the dedicated
``/citations`` and ``/references`` endpoints.  Crossref remains a deliberately
limited DOI/deposited-reference source.  Exact response bytes are private and
ignored; the tracked manifest contains hashes, real UTC timestamps, pagination
state, response status/rate-limit headers, parser versions, documentation, and
licence boundaries.  A failed or capped page is never interpreted as empty.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from corpus_common import (  # type: ignore[import-not-found]
        EXPECTED_PAPERS,
        SNAPSHOTS_DIR,
        doi_from_persistent_id,
        is_repository,
        load_corpus_rows,
        normalize_persistent_id,
        private_data_root,
        read_json,
        record_private_path,
        resolve_private_path,
        slug_for_id,
    )
else:
    from .corpus_common import (
        EXPECTED_PAPERS,
        SNAPSHOTS_DIR,
        doi_from_persistent_id,
        is_repository,
        load_corpus_rows,
        normalize_persistent_id,
        private_data_root,
        read_json,
        record_private_path,
        resolve_private_path,
        slug_for_id,
    )


MANIFEST_PATH = SNAPSHOTS_DIR / "citation_retrieval_manifest.json"
#: Raw provider responses, below the private root (``CUA_PRIVATE_ROOT`` or the
#: git-ignored ``private_data/``); recorded as ``private_data/literature/citation_raw``.
RAW_SUBDIR = ("literature", "citation_raw")
USER_AGENT = "ComputerUseAgent-citation-audit/2.1 (academic reproducibility; +https://github.com/appleweiping/ComputerUseAgent)"
SCHEMA_VERSION = "cui-citation-retrieval-manifest-v2"

OA_SEED_FIELDS = (
    "id,doi,title,publication_year,publication_date,type,cited_by_count,"
    "referenced_works,is_xpac,open_access,primary_location,ids"
)
OA_NODE_FIELDS = "id,doi,title,publication_year,type,cited_by_count,is_xpac,ids"
S2_SEED_FIELDS = "paperId,externalIds,title,year,citationCount,referenceCount"
S2_EDGE_FIELDS = "paperId,externalIds"

PROVIDER_META: dict[str, dict[str, str]] = {
    "openalex": {
        "parser": "cui-openalex-citation-parser",
        "parserVersion": "2.0.0",
        "providerDocsUrl": "https://developers.openalex.org/",
        "providerDataLicense": "CC0-1.0",
        "providerLicenseUrl": "https://developers.openalex.org/",
    },
    "semantic-scholar": {
        "parser": "cui-semantic-scholar-citation-parser",
        "parserVersion": "2.0.0",
        "providerDocsUrl": "https://api.semanticscholar.org/api-docs",
        "providerDataLicense": "Semantic Scholar Dataset License Agreement",
        "providerLicenseUrl": "https://api.semanticscholar.org/license/",
    },
    "crossref": {
        "parser": "cui-crossref-citation-parser",
        "parserVersion": "2.0.0",
        "providerDocsUrl": "https://api.crossref.org/swagger-ui/index.html",
        "providerDataLicense": "Crossref REST metadata; deposited content rights vary",
        "providerLicenseUrl": "https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-metadata-license-information/",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_id_now() -> str:
    return datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")


def normalized_title(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def sanitized_url(url: str) -> str:
    parsed = urlparse(url)
    filtered = sorted(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"api_key", "apikey", "key", "mailto"}
    )
    return urlunparse(parsed._replace(query=urlencode(filtered, doseq=True)))


def canonical_query(url: str) -> str:
    return urlparse(sanitized_url(url)).query


def endpoint_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_bytes(path: Path, value: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return {
        "rawPrivatePath": record_private_path(path),
        "rawSha256": sha256_bytes(value),
        "rawBytes": len(value),
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def selected_limit_headers(headers: Any) -> dict[str, str]:
    selected: dict[str, str] = {}
    for key, value in headers.items():
        lowered = str(key).lower()
        if (
            lowered.startswith("x-ratelimit")
            or lowered.startswith("ratelimit")
            or lowered in {"retry-after", "x-request-id", "x-openalex-api-key-status"}
        ):
            selected[str(key)] = str(value)
    return dict(sorted(selected.items(), key=lambda item: item[0].lower()))


@dataclass(frozen=True)
class Response:
    body: bytes
    data: Any
    final_url: str
    status: int
    retrieved_at: str
    limit_headers: dict[str, str]


class ProviderRequestError(RuntimeError):
    def __init__(
        self,
        provider: str,
        status: str,
        reason: str,
        *,
        http_status: int | None = None,
        body: bytes = b"",
        limit_headers: dict[str, str] | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(f"{provider}: {status}: {reason}")
        self.provider = provider
        self.status = status
        self.reason = reason
        self.http_status = http_status
        self.body = body
        self.limit_headers = limit_headers or {}
        self.attempts = attempts


def request_json(
    provider: str,
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 5,
) -> Response:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        **(headers or {}),
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    last_error: ProviderRequestError | None = None
    for attempt in range(1, retries + 1):
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=90) as response:
                raw = response.read()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ProviderRequestError(
                        provider,
                        "invalid-json",
                        f"{len(raw)} response bytes",
                        http_status=getattr(response, "status", 200),
                        body=raw,
                        limit_headers=selected_limit_headers(response.headers),
                        attempts=attempt,
                    ) from exc
                return Response(
                    body=raw,
                    data=data,
                    final_url=response.geturl(),
                    status=getattr(response, "status", 200),
                    retrieved_at=utc_now(),
                    limit_headers=selected_limit_headers(response.headers),
                )
        except HTTPError as exc:
            raw = exc.read()
            status = f"http-{exc.code}"
            last_error = ProviderRequestError(
                provider,
                status,
                str(exc.reason or "HTTP error"),
                http_status=exc.code,
                body=raw,
                limit_headers=selected_limit_headers(exc.headers),
                attempts=attempt,
            )
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if retryable and attempt < retries:
                retry_after = str(exc.headers.get("Retry-After", ""))
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = float(2 ** (attempt - 1))
                time.sleep(min(max(delay, 0.5), 30.0))
                continue
            break
        except (TimeoutError, URLError) as exc:
            last_error = ProviderRequestError(
                provider,
                "network-error",
                type(exc).__name__,
                attempts=attempt,
            )
            if attempt < retries:
                time.sleep(min(float(2 ** (attempt - 1)), 20.0))
                continue
            break
    if last_error is None:
        last_error = ProviderRequestError(
            provider, "unknown-error", "request did not run", attempts=retries
        )
    raise last_error


def evidence_for_response(
    provider: str,
    url: str,
    response: Response,
    raw_path: Path,
    **pagination: Any,
) -> dict[str, Any]:
    return {
        "endpoint": endpoint_url(url),
        "canonicalQuery": canonical_query(url),
        "requestUrl": sanitized_url(response.final_url),
        "retrievedAt": response.retrieved_at,
        "httpStatus": response.status,
        "rateLimitHeaders": response.limit_headers,
        **PROVIDER_META[provider],
        **pagination,
        **write_bytes(raw_path, response.body),
    }


def failure_evidence(
    provider: str,
    url: str,
    error: ProviderRequestError,
    raw_path: Path,
    **pagination: Any,
) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if error.body:
        raw = write_bytes(raw_path, error.body)
    return {
        "endpoint": endpoint_url(url),
        "canonicalQuery": canonical_query(url),
        "requestUrl": sanitized_url(url),
        "attemptedAt": utc_now(),
        "status": error.status,
        "reason": error.reason,
        "httpStatus": error.http_status,
        "rateLimitHeaders": error.limit_headers,
        "retryAttempts": error.attempts,
        **PROVIDER_META[provider],
        **pagination,
        **raw,
    }


def openalex_url(path: str, query: dict[str, str], api_key: str | None) -> str:
    values = dict(query)
    if api_key:
        values["api_key"] = api_key
    return f"https://api.openalex.org/{path}?{urlencode(values)}"


def short_openalex_id(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1]


def chunks(values: Iterable[str], size: int) -> Iterable[list[str]]:
    batch: list[str] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def doi_or_alias(row: dict[str, Any]) -> str | None:
    persistent_id = normalize_persistent_id(str(row["Persistent ID"]))
    doi = doi_from_persistent_id(persistent_id)
    if doi:
        return doi
    if persistent_id.startswith("arXiv:"):
        return f"10.48550/arXiv.{persistent_id.split(':', 1)[1]}"
    if persistent_id == "ACL:P19-3019":
        return "10.18653/v1/P19-3019"
    return None


def resolve_openalex_seed(
    row: dict[str, Any],
    api_key: str | None,
    raw_dir: Path,
    hint_openalex_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    persistent_id = normalize_persistent_id(str(row["Persistent ID"]))
    slug = slug_for_id(persistent_id)
    attempts: list[dict[str, Any]] = []
    work: dict[str, Any] | None = None
    seed_evidence: dict[str, Any] | None = None
    resolution_method = ""

    candidates: list[tuple[str, str]] = []
    if hint_openalex_id:
        candidates.append(("previous-provider-id-singleton", short_openalex_id(hint_openalex_id)))
    doi = doi_or_alias(row)
    if doi:
        candidates.append(("doi-singleton", f"https://doi.org/{doi}"))
    seen_identifiers: set[str] = set()
    for method, identifier in candidates:
        if identifier in seen_identifiers:
            continue
        seen_identifiers.add(identifier)
        url = openalex_url(
            f"works/{quote(identifier, safe='')}",
            {"include_xpac": "true", "select": OA_SEED_FIELDS},
            api_key,
        )
        try:
            response = request_json("openalex", url)
            evidence = evidence_for_response(
                "openalex", url, response, raw_dir / f"{slug}-seed.json", pageNumber=1, offset=None, cursor=None
            )
            if isinstance(response.data, dict) and response.data.get("id"):
                work = response.data
                seed_evidence = evidence
                resolution_method = method
                attempts.append({"method": method, "status": "resolved", **evidence})
                break
            attempts.append({"method": method, "status": "invalid-provider-record", **evidence})
        except ProviderRequestError as exc:
            evidence = failure_evidence(
                "openalex", url, exc, raw_dir / f"{slug}-{method}-failure.json", pageNumber=1, offset=None, cursor=None
            )
            attempts.append({"method": method, **evidence})

    if work is None:
        resolution_method = "exact-normalized-title-search-first-page"
        url = openalex_url(
            "works",
            {
                "search": str(row["Title"]),
                "include_xpac": "true",
                "per_page": "10",
                "select": OA_SEED_FIELDS,
            },
            api_key,
        )
        try:
            response = request_json("openalex", url)
            evidence = evidence_for_response(
                "openalex", url, response, raw_dir / f"{slug}-seed-search.json", pageNumber=1, offset=0, cursor=None
            )
            values = response.data.get("results", []) if isinstance(response.data, dict) else []
            exact = [
                (index, item)
                for index, item in enumerate(values)
                if isinstance(item, dict)
                and normalized_title(str(item.get("title", "")))
                == normalized_title(str(row["Title"]))
            ]
            year = str(row["Publication Year"]).strip()
            year_exact = [
                pair for pair in exact if str(pair[1].get("publication_year", "")) == year
            ]
            if len(year_exact) == 1:
                exact = year_exact
            if len(exact) == 1:
                selected_index, work = exact[0]
                seed_evidence = {
                    **evidence,
                    "providerRecordJsonPointer": f"$.results[{selected_index}]",
                }
                attempts.append(
                    {
                        "method": resolution_method,
                        "status": "resolved",
                        "selectedIndex": selected_index,
                        **evidence,
                    }
                )
            else:
                attempts.append(
                    {
                        "method": resolution_method,
                        "status": "ambiguous-or-not-found",
                        "exactTitleCandidates": len(exact),
                        **evidence,
                    }
                )
        except ProviderRequestError as exc:
            evidence = failure_evidence(
                "openalex", url, exc, raw_dir / f"{slug}-seed-search-failure.json", pageNumber=1, offset=0, cursor=None
            )
            attempts.append({"method": resolution_method, **evidence})

    if work is None or seed_evidence is None:
        return (
            {
                "persistentId": persistent_id,
                "title": row["Title"],
                "status": "unresolved",
                "attempts": attempts,
                "directions": {
                    "outgoing": {"status": "blocked-seed-unresolved", "remaining": None},
                    "incoming": {"status": "blocked-seed-unresolved", "remaining": None},
                },
            },
            None,
        )

    is_xpac = work.get("is_xpac")
    xpac_method = "explicit-is_xpac-field"
    if not isinstance(is_xpac, bool):
        xpac_method = "include_xpac-set-difference"
        core_url = openalex_url(
            f"works/{quote(short_openalex_id(str(work['id'])), safe='')}",
            {"include_xpac": "false", "select": "id"},
            api_key,
        )
        try:
            core_response = request_json("openalex", core_url)
            is_xpac = not bool(core_response.data.get("id"))
            core_evidence = evidence_for_response(
                "openalex", core_url, core_response, raw_dir / f"{slug}-core-membership.json", pageNumber=1, offset=None, cursor=None
            )
            attempts.append({"method": xpac_method, "status": "core-record-found", **core_evidence})
        except ProviderRequestError as exc:
            if exc.http_status == 404:
                is_xpac = True
                attempts.append(
                    {
                        "method": xpac_method,
                        "status": "xpac-only-by-set-difference",
                        **failure_evidence(
                            "openalex", core_url, exc, raw_dir / f"{slug}-core-membership-404.json", pageNumber=1, offset=None, cursor=None
                        ),
                    }
                )
            else:
                return (
                    {
                        "persistentId": persistent_id,
                        "title": row["Title"],
                        "status": "xpac-membership-unresolved",
                        "openAlexId": work.get("id"),
                        "seedEvidence": seed_evidence,
                        "attempts": attempts,
                        "directions": {
                            "outgoing": {"status": "blocked-xpac-unresolved", "remaining": len(work.get("referenced_works") or [])},
                            "incoming": {"status": "blocked-xpac-unresolved", "remaining": work.get("cited_by_count")},
                        },
                    },
                    None,
                )

    references = work.get("referenced_works") or []
    record = {
        "persistentId": persistent_id,
        "title": row["Title"],
        "status": "resolved",
        "openAlexId": str(work["id"]),
        "doi": work.get("doi"),
        "publicationYear": work.get("publication_year"),
        "citedByCount": work.get("cited_by_count"),
        "referencedWorksCount": len(references),
        "xpac": {
            "membership": "openalex-xpac" if is_xpac else "openalex-core",
            "xpacOnly": bool(is_xpac),
            "method": xpac_method,
            "isXpacRaw": bool(is_xpac),
        },
        "resolutionMethod": resolution_method,
        "seedEvidence": seed_evidence,
        "attempts": attempts,
        "directions": {
            "outgoing": {
                "status": "complete",
                "expectedCount": len(references),
                "retrievedCount": len(references),
                "uniqueCount": len(set(map(str, references))),
                "remaining": 0,
                "exhaustionEvidence": "complete referenced_works array in the resolved seed singleton/search record",
            },
            "incoming": {"status": "pending"},
        },
    }
    return record, work


Checkpoint = Callable[[], None]


def fetch_openalex_incoming(
    record: dict[str, Any],
    api_key: str | None,
    raw_dir: Path,
    checkpoint: Checkpoint,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    work_id = str(record["openAlexId"])
    expected_singleton = int(record.get("citedByCount") or 0)
    if expected_singleton == 0:
        direction = {
            "status": "complete-zero-provider-count",
            "expectedCount": 0,
            "retrievedCount": 0,
            "uniqueCount": 0,
            "pages": [],
            "remaining": 0,
            "exhaustionEvidence": "seed singleton cited_by_count=0; no cursor page exists to consume",
        }
        record["directions"]["incoming"] = direction
        checkpoint()
        return direction, {}

    slug = slug_for_id(str(record["persistentId"]))
    cursor = "*"
    page_number = 0
    page_evidence: list[dict[str, Any]] = []
    works: dict[str, dict[str, Any]] = {}
    retrieved_ids: list[str] = []
    expected_from_query: int | None = None
    while cursor:
        page_number += 1
        url = openalex_url(
            "works",
            {
                "filter": f"cites:{short_openalex_id(work_id)}",
                "include_xpac": "true",
                "per_page": "100",
                "cursor": cursor,
                "select": OA_NODE_FIELDS,
            },
            api_key,
        )
        try:
            response = request_json("openalex", url)
            evidence = evidence_for_response(
                "openalex",
                url,
                response,
                raw_dir / slug / "incoming" / f"page-{page_number:04d}.json",
                pageNumber=page_number,
                offset=(page_number - 1) * 100,
                cursor=cursor,
            )
        except ProviderRequestError as exc:
            failure = failure_evidence(
                "openalex",
                url,
                exc,
                raw_dir / slug / "incoming" / f"page-{page_number:04d}-failure.json",
                pageNumber=page_number,
                offset=(page_number - 1) * 100,
                cursor=cursor,
            )
            direction = {
                "status": "blocked-page-failure",
                "expectedCount": expected_from_query or expected_singleton,
                "retrievedCount": len(retrieved_ids),
                "uniqueCount": len(set(retrieved_ids)),
                "pages": page_evidence,
                "nextCursor": cursor,
                "remaining": max((expected_from_query or expected_singleton) - len(set(retrieved_ids)), 0),
                "failure": failure,
            }
            record["directions"]["incoming"] = direction
            checkpoint()
            return direction, works

        payload = response.data
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            direction = {
                "status": "blocked-invalid-page-shape",
                "expectedCount": expected_from_query or expected_singleton,
                "retrievedCount": len(retrieved_ids),
                "uniqueCount": len(set(retrieved_ids)),
                "pages": page_evidence + [evidence],
                "nextCursor": cursor,
                "remaining": max((expected_from_query or expected_singleton) - len(set(retrieved_ids)), 0),
            }
            record["directions"]["incoming"] = direction
            checkpoint()
            return direction, works
        meta = payload.get("meta") or {}
        if expected_from_query is None and isinstance(meta.get("count"), int):
            expected_from_query = int(meta["count"])
        results = payload["results"]
        for work in results:
            if isinstance(work, dict) and work.get("id"):
                provider_id = str(work["id"])
                retrieved_ids.append(provider_id)
                works[provider_id] = work
        evidence["resultCount"] = len(results)
        evidence["providerCount"] = meta.get("count")
        evidence["nextCursorPresent"] = bool(meta.get("next_cursor"))
        page_evidence.append(evidence)
        next_cursor = meta.get("next_cursor")
        cursor = str(next_cursor) if next_cursor else ""
        record["directions"]["incoming"] = {
            "status": "in-progress" if cursor else "reconciling",
            "expectedCount": expected_from_query or expected_singleton,
            "retrievedCount": len(retrieved_ids),
            "uniqueCount": len(set(retrieved_ids)),
            "pages": page_evidence,
            "nextCursor": cursor or None,
            "remaining": max((expected_from_query or expected_singleton) - len(set(retrieved_ids)), 0),
        }
        checkpoint()

    unique_count = len(set(retrieved_ids))
    expected = expected_from_query if expected_from_query is not None else expected_singleton
    duplicates = len(retrieved_ids) - unique_count
    status = "complete" if unique_count == expected and duplicates == 0 else "count-mismatch"
    direction = {
        "status": status,
        "expectedCount": expected,
        "singletonCitedByCount": expected_singleton,
        "retrievedCount": len(retrieved_ids),
        "uniqueCount": unique_count,
        "duplicateCount": duplicates,
        "pages": page_evidence,
        "nextCursor": None,
        "remaining": max(expected - unique_count, 0),
        "exhaustionEvidence": "OpenAlex meta.next_cursor is null after the final stored page",
    }
    record["directions"]["incoming"] = direction
    checkpoint()
    return direction, works


def fetch_openalex_external_metadata(
    required_ids: set[str],
    known_ids: set[str],
    api_key: str | None,
    raw_dir: Path,
    manifest: dict[str, Any],
    checkpoint: Checkpoint,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    missing_initial = sorted(required_ids - known_ids)
    pages: list[dict[str, Any]] = []
    works: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    for batch_number, batch in enumerate(chunks(missing_initial, 100), start=1):
        short_ids = [short_openalex_id(value) for value in batch]
        url = openalex_url(
            "works",
            {
                "filter": f"openalex_id:{'|'.join(short_ids)}",
                "include_xpac": "true",
                "per_page": "100",
                "select": OA_NODE_FIELDS,
            },
            api_key,
        )
        try:
            response = request_json("openalex", url)
            evidence = evidence_for_response(
                "openalex",
                url,
                response,
                raw_dir / "external-metadata" / f"batch-{batch_number:04d}.json",
                pageNumber=batch_number,
                offset=(batch_number - 1) * 100,
                cursor=None,
            )
            results = response.data.get("results", []) if isinstance(response.data, dict) else []
            returned: set[str] = set()
            for work in results:
                if isinstance(work, dict) and work.get("id"):
                    provider_id = str(work["id"])
                    returned.add(provider_id)
                    works[provider_id] = work
            requested_full = {f"https://openalex.org/{value}" for value in short_ids}
            batch_missing = sorted(requested_full - returned)
            unresolved.extend(batch_missing)
            evidence.update(
                {
                    "requestedIdCount": len(batch),
                    "resultCount": len(results),
                    "missingIdCount": len(batch_missing),
                }
            )
            pages.append(evidence)
        except ProviderRequestError as exc:
            unresolved.extend(batch)
            pages.append(
                {
                    "requestedIdCount": len(batch),
                    "resultCount": 0,
                    "missingIdCount": len(batch),
                    "failure": failure_evidence(
                        "openalex",
                        url,
                        exc,
                        raw_dir / "external-metadata" / f"batch-{batch_number:04d}-failure.json",
                        pageNumber=batch_number,
                        offset=(batch_number - 1) * 100,
                        cursor=None,
                    ),
                }
            )
        state = {
            "status": "in-progress",
            "requiredUniqueIds": len(required_ids),
            "alreadyKnownIds": len(required_ids & known_ids),
            "requestedMetadataIds": len(missing_initial),
            "resolvedMetadataIds": len(works),
            "unresolvedMetadataIds": sorted(set(unresolved)),
            "pages": pages,
        }
        manifest["records"]["openalexExternalMetadata"] = state
        checkpoint()
    followups: list[dict[str, Any]] = []
    unresolved_set = set(unresolved)
    for provider_id in sorted(unresolved_set):
        url = openalex_url(
            f"works/{quote(short_openalex_id(provider_id), safe='')}",
            {"include_xpac": "true", "select": OA_NODE_FIELDS},
            api_key,
        )
        slug = short_openalex_id(provider_id).lower()
        try:
            response = request_json("openalex", url)
            evidence = evidence_for_response(
                "openalex",
                url,
                response,
                raw_dir / "external-metadata" / "singleton-followup" / f"{slug}.json",
                pageNumber=1,
                offset=None,
                cursor=None,
            )
            work = response.data
            if isinstance(work, dict) and work.get("id"):
                works[str(work["id"])] = work
                unresolved_set.discard(provider_id)
                followups.append(
                    {"providerId": provider_id, "status": "resolved", "evidence": evidence}
                )
            else:
                followups.append(
                    {
                        "providerId": provider_id,
                        "status": "invalid-provider-record",
                        "evidence": evidence,
                    }
                )
        except ProviderRequestError as exc:
            followups.append(
                {
                    "providerId": provider_id,
                    "status": "provider-not-found" if exc.http_status == 404 else "provider-blocked",
                    "failure": failure_evidence(
                        "openalex",
                        url,
                        exc,
                        raw_dir / "external-metadata" / "singleton-followup" / f"{slug}-failure.json",
                        pageNumber=1,
                        offset=None,
                        cursor=None,
                    ),
                }
            )
        manifest["records"]["openalexExternalMetadata"] = {
            "status": "in-progress-singleton-verification",
            "requiredUniqueIds": len(required_ids),
            "alreadyKnownIds": len(required_ids & known_ids),
            "requestedMetadataIds": len(missing_initial),
            "resolvedMetadataIds": len(works),
            "unresolvedMetadataIds": sorted(unresolved_set),
            "pages": pages,
            "singletonFollowups": followups,
        }
        checkpoint()
    resolved_total = len(required_ids & known_ids) + len(works)
    all_not_found = bool(unresolved_set) and all(
        item["status"] == "provider-not-found"
        for item in followups
        if item["providerId"] in unresolved_set
    )
    state = {
        "status": (
            "complete"
            if not unresolved_set and resolved_total == len(required_ids)
            else "partial-provider-not-found"
            if all_not_found
            else "partial"
        ),
        "requiredUniqueIds": len(required_ids),
        "alreadyKnownIds": len(required_ids & known_ids),
        "requestedMetadataIds": len(missing_initial),
        "resolvedMetadataIds": len(works),
        "unresolvedMetadataIds": sorted(unresolved_set),
        "pages": pages,
        "singletonFollowups": followups,
    }
    manifest["records"]["openalexExternalMetadata"] = state
    checkpoint()
    return state, works


def previous_openalex_hints(previous: dict[str, Any] | None) -> dict[str, str]:
    if not previous:
        return {}
    values = previous.get("records", {}).get("openalex", [])
    return {
        normalize_persistent_id(str(record["persistentId"])): str(record["openAlexId"])
        for record in values
        if isinstance(record, dict) and record.get("openAlexId")
    }


def collect_openalex(
    rows: list[dict[str, Any]],
    api_key: str | None,
    run_raw: Path,
    manifest: dict[str, Any],
    checkpoint: Checkpoint,
    hints: dict[str, str],
) -> None:
    provider = {
        "role": "primary",
        "authentication": "api-key-present" if api_key else "anonymous",
        "status": "in-progress",
        "coverage": "partial",
        "requestedSeeds": len(rows),
        "resolvedSeeds": 0,
        "unresolvedSeeds": len(rows),
        "listRequestBudgetBoundary": (
            "OpenAlex documentation reports $0.01/day anonymous usage versus $1/day with a free key; no key was present for this run."
            if not api_key
            else "Free-key or paid account budget applies; the key value is never persisted."
        ),
        "xpac": {
            "labels": ["openalex-core", "openalex-xpac"],
            "classification": "explicit is_xpac when returned; otherwise include_xpac set difference",
            "relevanceInference": "none",
        },
    }
    manifest["providers"]["openalex"] = provider
    manifest["records"]["openalex"] = []
    checkpoint()
    records: list[dict[str, Any]] = manifest["records"]["openalex"]
    seed_works: dict[str, dict[str, Any]] = {}
    referenced_ids: set[str] = set()
    raw_dir = run_raw / "openalex"

    for index, row in enumerate(rows, start=1):
        pid = normalize_persistent_id(str(row["Persistent ID"]))
        record, work = resolve_openalex_seed(row, api_key, raw_dir / "seeds", hints.get(pid))
        records.append(record)
        if work is not None:
            seed_works[str(work["id"])] = work
            referenced_ids.update(map(str, work.get("referenced_works") or []))
        provider["resolvedSeeds"] = sum(item["status"] == "resolved" for item in records)
        provider["unresolvedSeeds"] = index - provider["resolvedSeeds"] + len(rows) - index
        checkpoint()
        print(
            json.dumps(
                {"provider": "openalex", "phase": "seed-resolution", "processed": index, "total": len(rows), "resolved": provider["resolvedSeeds"]},
                sort_keys=True,
            ),
            flush=True,
        )

    incoming_works: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records, start=1):
        if record["status"] != "resolved":
            continue
        _, works = fetch_openalex_incoming(record, api_key, raw_dir, checkpoint)
        incoming_works.update(works)
        print(
            json.dumps(
                {
                    "provider": "openalex",
                    "phase": "incoming-cursor",
                    "processedSeed": index,
                    "totalSeeds": len(records),
                    "seed": record["persistentId"],
                    "status": record["directions"]["incoming"]["status"],
                    "pages": len(record["directions"]["incoming"].get("pages", [])),
                },
                sort_keys=True,
            ),
            flush=True,
        )

    known_ids = set(seed_works) | set(incoming_works)
    metadata_state, external_works = fetch_openalex_external_metadata(
        referenced_ids, known_ids, api_key, raw_dir, manifest, checkpoint
    )
    all_known = {**seed_works, **incoming_works, **external_works}
    resolved = sum(record["status"] == "resolved" for record in records)
    incoming_complete = sum(
        record.get("directions", {}).get("incoming", {}).get("status")
        in {"complete", "complete-zero-provider-count"}
        for record in records
        if record["status"] == "resolved"
    )
    outgoing_complete = sum(
        record.get("directions", {}).get("outgoing", {}).get("status") == "complete"
        for record in records
        if record["status"] == "resolved"
    )
    xpac_values = [
        work.get("is_xpac")
        for work in all_known.values()
        if isinstance(work.get("is_xpac"), bool)
    ]
    provider.update(
        {
            "status": "finished",
            "resolvedSeeds": resolved,
            "unresolvedSeeds": len(rows) - resolved,
            "outgoing": {
                "completeSeeds": outgoing_complete,
                "blockedSeeds": resolved - outgoing_complete,
                "declaredEdges": sum(
                    int(record.get("directions", {}).get("outgoing", {}).get("uniqueCount") or 0)
                    for record in records
                ),
            },
            "incoming": {
                "completeSeeds": incoming_complete,
                "blockedSeeds": resolved - incoming_complete,
                "declaredEdges": sum(
                    int(record.get("directions", {}).get("incoming", {}).get("uniqueCount") or 0)
                    for record in records
                ),
                "storedPages": sum(
                    len(record.get("directions", {}).get("incoming", {}).get("pages", []))
                    for record in records
                ),
            },
            "externalMetadata": metadata_state,
            "xpac": {
                **provider["xpac"],
                "classifiedWorks": len(xpac_values),
                "xpacOnlyWorks": sum(value is True for value in xpac_values),
                "coreWorks": sum(value is False for value in xpac_values),
            },
        }
    )
    complete = (
        resolved == len(rows)
        and incoming_complete == resolved
        and outgoing_complete == resolved
        and metadata_state["status"] == "complete"
    )
    provider["coverage"] = "complete" if complete else "partial"
    checkpoint()


def s2_lookup_id(row: dict[str, Any]) -> str | None:
    persistent_id = normalize_persistent_id(str(row["Persistent ID"]))
    doi = doi_from_persistent_id(persistent_id)
    if doi:
        return f"DOI:{doi}"
    if persistent_id.startswith("arXiv:"):
        return f"ARXIV:{persistent_id.split(':', 1)[1]}"
    if persistent_id == "ACL:P19-3019":
        return "DOI:10.18653/v1/P19-3019"
    return None


def fetch_s2_direction(
    record: dict[str, Any],
    direction: str,
    headers: dict[str, str],
    raw_dir: Path,
    checkpoint: Checkpoint,
    interval: float,
    circuit: dict[str, Any],
) -> dict[str, Any]:
    if circuit.get("open"):
        return {
            "status": "blocked-provider-circuit-open",
            "retrievedCount": 0,
            "uniqueCount": 0,
            "pages": [],
            "nextOffset": 0,
            "remaining": None,
            "failure": {
                "status": "circuit-open",
                "reason": "three consecutive retried 429/5xx/network failures",
                **PROVIDER_META["semantic-scholar"],
            },
        }
    paper_id = str(record["semanticScholarPaperId"])
    slug = slug_for_id(str(record["persistentId"]))
    offset = 0
    page_number = 0
    pages: list[dict[str, Any]] = []
    retrieved_ids: list[str] = []
    retrieved_rows = 0
    declared_key = "citationCount" if direction == "citations" else "referenceCount"
    declared_value = record.get(declared_key)
    expected_total: int | None = (
        int(declared_value) if isinstance(declared_value, int) else None
    )
    while True:
        page_number += 1
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/{quote(paper_id, safe='')}/{direction}?"
            + urlencode({"fields": S2_EDGE_FIELDS, "offset": str(offset), "limit": "1000"})
        )
        try:
            response = request_json("semantic-scholar", url, headers=headers)
            circuit["consecutiveFailures"] = 0
            evidence = evidence_for_response(
                "semantic-scholar",
                url,
                response,
                raw_dir / slug / direction / f"page-{page_number:04d}.json",
                pageNumber=page_number,
                offset=offset,
                cursor=None,
            )
        except ProviderRequestError as exc:
            if exc.status == "http-429" or exc.status.startswith("http-5") or exc.status == "network-error":
                circuit["consecutiveFailures"] = int(circuit.get("consecutiveFailures", 0)) + 1
                if circuit["consecutiveFailures"] >= 3:
                    circuit["open"] = True
                    circuit["openedAt"] = utc_now()
            failure = failure_evidence(
                "semantic-scholar",
                url,
                exc,
                raw_dir / slug / direction / f"page-{page_number:04d}-failure.json",
                pageNumber=page_number,
                offset=offset,
                cursor=None,
            )
            return {
                "status": "blocked-page-failure",
                "expectedCount": expected_total,
                "retrievedCount": retrieved_rows,
                "identifiedProviderIdCount": len(retrieved_ids),
                "uniqueCount": len(set(retrieved_ids)),
                "pages": pages,
                "nextOffset": offset,
                "remaining": max(expected_total - retrieved_rows, 0) if expected_total is not None else None,
                "failure": failure,
            }
        payload = response.data
        if isinstance(payload, dict) and payload.get("data") is None and payload.get("citingPaperInfo"):
            return {
                "status": "blocked-provider-data-elided",
                "expectedCount": expected_total,
                "retrievedCount": retrieved_rows,
                "identifiedProviderIdCount": len(retrieved_ids),
                "uniqueCount": len(set(retrieved_ids)),
                "pages": pages + [evidence],
                "nextOffset": offset,
                "remaining": expected_total,
                "failure": {
                    "status": "provider-data-elided",
                    "reason": "Semantic Scholar returned data=null and citingPaperInfo for a provider-elided references field",
                    "httpStatus": response.status,
                    "rateLimitHeaders": response.limit_headers,
                    **PROVIDER_META["semantic-scholar"],
                },
            }
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return {
                "status": "blocked-invalid-page-shape",
                "expectedCount": expected_total,
                "retrievedCount": retrieved_rows,
                "identifiedProviderIdCount": len(retrieved_ids),
                "uniqueCount": len(set(retrieved_ids)),
                "pages": pages + [evidence],
                "nextOffset": offset,
                "remaining": None,
            }
        values = payload["data"]
        retrieved_rows += len(values)
        if expected_total is None and isinstance(payload.get("total"), int):
            expected_total = int(payload["total"])
        nested_key = "citingPaper" if direction == "citations" else "citedPaper"
        for item in values:
            nested = item.get(nested_key) if isinstance(item, dict) else None
            if isinstance(nested, dict) and nested.get("paperId"):
                retrieved_ids.append(str(nested["paperId"]))
        next_offset = payload.get("next")
        evidence.update(
            {
                "resultCount": len(values),
                "providerTotal": payload.get("total"),
                "providerNextOffset": next_offset,
            }
        )
        pages.append(evidence)
        record["directions"][direction] = {
            "status": "in-progress",
            "expectedCount": expected_total,
            "retrievedCount": retrieved_rows,
            "identifiedProviderIdCount": len(retrieved_ids),
            "uniqueCount": len(set(retrieved_ids)),
            "pages": pages,
            "nextOffset": next_offset,
            "remaining": max(expected_total - retrieved_rows, 0) if expected_total is not None else None,
        }
        checkpoint()
        time.sleep(max(interval, 0.0))
        if next_offset is None or (not values and int(next_offset or 0) <= offset):
            break
        next_int = int(next_offset)
        if next_int <= offset:
            break
        if next_int > 9999:
            return {
                "status": "blocked-provider-9999-cap",
                "expectedCount": expected_total,
                "retrievedCount": retrieved_rows,
                "identifiedProviderIdCount": len(retrieved_ids),
                "uniqueCount": len(set(retrieved_ids)),
                "pages": pages,
                "nextOffset": next_int,
                "remaining": max(expected_total - retrieved_rows, 0) if expected_total is not None else None,
                "failure": {
                    "status": "provider-cap",
                    "reason": "Semantic Scholar documents a maximum of 9,999 citations from this endpoint",
                    **PROVIDER_META["semantic-scholar"],
                },
            }
        offset = next_int

    unique_count = len(set(retrieved_ids))
    duplicates = len(retrieved_ids) - unique_count
    if expected_total is None:
        expected_total = retrieved_rows
    status = "complete" if retrieved_rows == expected_total else "count-mismatch"
    return {
        "status": status,
        "expectedCount": expected_total,
        "retrievedCount": retrieved_rows,
        "identifiedProviderIdCount": len(retrieved_ids),
        "uniqueCount": unique_count,
        "unidentifiedRelationRows": retrieved_rows - len(retrieved_ids),
        "duplicateCount": duplicates,
        "pages": pages,
        "nextOffset": None,
        "remaining": max(expected_total - retrieved_rows, 0),
        "exhaustionEvidence": "Semantic Scholar next offset is absent after the final stored page",
    }


def collect_semantic_scholar(
    rows: list[dict[str, Any]],
    api_key: str | None,
    run_raw: Path,
    manifest: dict[str, Any],
    checkpoint: Checkpoint,
    interval: float,
) -> None:
    headers = {"x-api-key": api_key} if api_key else {}
    provider: dict[str, Any] = {
        "role": "auxiliary-coverage-sensitivity",
        "authentication": "api-key-present" if api_key else "anonymous",
        "status": "in-progress",
        "coverage": "partial",
        "requestedSeeds": len(rows),
        "publicBoundary": (
            "Tracked outputs expose only S2 provider IDs, support/count flags, and attribution. "
            "Any seed title in this retrieval ledger is copied from frozen corpus truth, not S2; "
            "S2-only title/year text and full response payloads remain private/ignored."
        ),
    }
    manifest["providers"]["semanticScholar"] = provider
    manifest["records"]["semanticScholar"] = []
    checkpoint()
    raw_dir = run_raw / "semantic-scholar"
    direct = [(row, s2_lookup_id(row)) for row in rows]
    direct_values = [(row, identifier) for row, identifier in direct if identifier]
    batch_url = "https://api.semanticscholar.org/graph/v1/paper/batch?" + urlencode({"fields": S2_SEED_FIELDS})
    batch_items: list[Any] = []
    batch_evidence: dict[str, Any] | None = None
    batch_failure: dict[str, Any] | None = None
    try:
        response = request_json(
            "semantic-scholar",
            batch_url,
            method="POST",
            payload={"ids": [identifier for _, identifier in direct_values]},
            headers=headers,
        )
        batch_evidence = evidence_for_response(
            "semantic-scholar", batch_url, response, raw_dir / "seed-batch.json", pageNumber=1, offset=0, cursor=None
        )
        if isinstance(response.data, list):
            batch_items = response.data
    except ProviderRequestError as exc:
        batch_failure = failure_evidence(
            "semantic-scholar", batch_url, exc, raw_dir / "seed-batch-failure.json", pageNumber=1, offset=0, cursor=None
        )

    direct_map = {
        normalize_persistent_id(str(row["Persistent ID"])): (identifier, item)
        for (row, identifier), item in zip(direct_values, batch_items)
    }
    records: list[dict[str, Any]] = manifest["records"]["semanticScholar"]
    for row, identifier in direct:
        pid = normalize_persistent_id(str(row["Persistent ID"]))
        if identifier:
            item = direct_map.get(pid, (identifier, None))[1]
            if isinstance(item, dict) and item.get("paperId"):
                records.append(
                    {
                        "persistentId": pid,
                        "title": row["Title"],
                        "status": "resolved",
                        "lookupId": identifier,
                        "semanticScholarPaperId": str(item["paperId"]),
                        "citationCount": item.get("citationCount"),
                        "referenceCount": item.get("referenceCount"),
                        "seedEvidence": batch_evidence,
                        "directions": {"citations": {"status": "pending"}, "references": {"status": "pending"}},
                    }
                )
            else:
                records.append(
                    {
                        "persistentId": pid,
                        "title": row["Title"],
                        "status": "provider-blocked" if batch_failure else "unresolved",
                        "lookupId": identifier,
                        "failure": batch_failure,
                        "directions": {
                            "citations": {"status": "blocked-seed-unresolved", "remaining": None},
                            "references": {"status": "blocked-seed-unresolved", "remaining": None},
                        },
                    }
                )
            continue

        search_url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urlencode(
            {"query": str(row["Title"]), "limit": "5", "fields": S2_SEED_FIELDS}
        )
        try:
            response = request_json("semantic-scholar", search_url, headers=headers)
            evidence = evidence_for_response(
                "semantic-scholar", search_url, response, raw_dir / "seed-search" / f"{slug_for_id(pid)}.json", pageNumber=1, offset=0, cursor=None
            )
            candidates = [
                item
                for item in response.data.get("data", [])
                if isinstance(item, dict)
                and normalized_title(str(item.get("title", ""))) == normalized_title(str(row["Title"]))
            ]
            if len(candidates) == 1:
                item = candidates[0]
                records.append(
                    {
                        "persistentId": pid,
                        "title": row["Title"],
                        "status": "resolved",
                        "lookupId": "exact-normalized-title-search-first-page",
                        "semanticScholarPaperId": str(item["paperId"]),
                        "citationCount": item.get("citationCount"),
                        "referenceCount": item.get("referenceCount"),
                        "seedEvidence": evidence,
                        "directions": {"citations": {"status": "pending"}, "references": {"status": "pending"}},
                    }
                )
            else:
                records.append(
                    {
                        "persistentId": pid,
                        "title": row["Title"],
                        "status": "unresolved-title-search",
                        "exactTitleCandidates": len(candidates),
                        "seedEvidence": evidence,
                        "directions": {
                            "citations": {"status": "blocked-seed-unresolved", "remaining": None},
                            "references": {"status": "blocked-seed-unresolved", "remaining": None},
                        },
                    }
                )
        except ProviderRequestError as exc:
            failure = failure_evidence(
                "semantic-scholar", search_url, exc, raw_dir / "seed-search" / f"{slug_for_id(pid)}-failure.json", pageNumber=1, offset=0, cursor=None
            )
            records.append(
                {
                    "persistentId": pid,
                    "title": row["Title"],
                    "status": "provider-blocked",
                    "failure": failure,
                    "directions": {
                        "citations": {"status": "blocked-seed-unresolved", "remaining": None},
                        "references": {"status": "blocked-seed-unresolved", "remaining": None},
                    },
                }
            )
        time.sleep(max(interval, 0.0))
    checkpoint()

    circuit: dict[str, Any] = {"consecutiveFailures": 0, "open": False}
    for index, record in enumerate(records, start=1):
        if record["status"] != "resolved":
            continue
        for direction in ("citations", "references"):
            result = fetch_s2_direction(
                record, direction, headers, raw_dir, checkpoint, interval, circuit
            )
            record["directions"][direction] = result
            checkpoint()
        print(
            json.dumps(
                {
                    "provider": "semantic-scholar",
                    "processedSeed": index,
                    "totalSeeds": len(records),
                    "seed": record["persistentId"],
                    "citations": record["directions"]["citations"]["status"],
                    "references": record["directions"]["references"]["status"],
                    "circuitOpen": bool(circuit.get("open")),
                },
                sort_keys=True,
            ),
            flush=True,
        )

    resolved = sum(record["status"] == "resolved" for record in records)
    direction_complete = {
        direction: sum(
            record.get("directions", {}).get(direction, {}).get("status") == "complete"
            for record in records
            if record["status"] == "resolved"
        )
        for direction in ("citations", "references")
    }
    provider.update(
        {
            "status": "finished",
            "resolvedSeeds": resolved,
            "unresolvedSeeds": len(rows) - resolved,
            "citations": {
                "completeSeeds": direction_complete["citations"],
                "blockedSeeds": resolved - direction_complete["citations"],
                "declaredEdges": sum(
                    int(record.get("directions", {}).get("citations", {}).get("uniqueCount") or 0)
                    for record in records
                ),
            },
            "references": {
                "completeSeeds": direction_complete["references"],
                "blockedSeeds": resolved - direction_complete["references"],
                "declaredEdges": sum(
                    int(record.get("directions", {}).get("references", {}).get("uniqueCount") or 0)
                    for record in records
                ),
            },
            "circuitBreaker": circuit,
        }
    )
    complete = resolved == len(rows) and all(value == resolved for value in direction_complete.values())
    provider["coverage"] = "complete" if complete else "partial"
    checkpoint()


def collect_crossref(
    rows: list[dict[str, Any]],
    mailto: str | None,
    run_raw: Path,
    manifest: dict[str, Any],
    checkpoint: Checkpoint,
) -> None:
    doi_rows = [row for row in rows if doi_from_persistent_id(str(row["Persistent ID"]))]
    records: list[dict[str, Any]] = []
    manifest["records"]["crossref"] = records
    provider = {
        "role": "limited-auxiliary-doi-metadata-and-deposited-reference-candidates",
        "authentication": "polite-mailto-present" if mailto else "anonymous",
        "scope": "Persistent-ID DOI seeds only; arXiv aliases are intentionally excluded.",
        "candidateBoundary": "A deposited DOI reference is an exact-ID candidate/support assertion; title-only references are not merged and deposited counts are not treated as complete bibliographies.",
        "requestedDoiSeeds": len(doi_rows),
        "status": "in-progress",
        "coverage": "partial",
    }
    manifest["providers"]["crossref"] = provider
    checkpoint()
    raw_dir = run_raw / "crossref"
    for index, row in enumerate(doi_rows, start=1):
        pid = normalize_persistent_id(str(row["Persistent ID"]))
        doi = str(doi_from_persistent_id(pid))
        query = {"mailto": mailto} if mailto else {}
        suffix = f"?{urlencode(query)}" if query else ""
        url = f"https://api.crossref.org/works/{quote(doi, safe='')}{suffix}"
        try:
            response = request_json("crossref", url)
            evidence = evidence_for_response(
                "crossref", url, response, raw_dir / f"{slug_for_id(pid)}.json", pageNumber=1, offset=None, cursor=None
            )
            message = response.data.get("message", {})
            references = message.get("reference") or [] if isinstance(message, dict) else []
            records.append(
                {
                    "persistentId": pid,
                    "title": row["Title"],
                    "status": "resolved",
                    "doi": message.get("DOI") if isinstance(message, dict) else None,
                    "isReferencedByCount": message.get("is-referenced-by-count") if isinstance(message, dict) else None,
                    "depositedReferenceCount": len(references),
                    "exactDoiReferenceCandidates": sum(isinstance(item, dict) and bool(item.get("DOI")) for item in references),
                    "evidence": evidence,
                }
            )
        except ProviderRequestError as exc:
            records.append(
                {
                    "persistentId": pid,
                    "title": row["Title"],
                    "status": "provider-blocked",
                    "doi": doi,
                    "failure": failure_evidence(
                        "crossref", url, exc, raw_dir / f"{slug_for_id(pid)}-failure.json", pageNumber=1, offset=None, cursor=None
                    ),
                }
            )
        provider["resolvedDoiSeeds"] = sum(record["status"] == "resolved" for record in records)
        checkpoint()
        if index < len(doi_rows):
            time.sleep(0.08)
    resolved = sum(record["status"] == "resolved" for record in records)
    provider.update(
        {
            "status": "finished",
            "resolvedDoiSeeds": resolved,
            "coverage": "complete-limited-scope" if resolved == len(doi_rows) else "partial",
        }
    )
    checkpoint()


def new_manifest(
    run_id: str, run_raw: Path, corpus_provenance: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "retrievalWindow": {
            "runId": run_id,
            "startedAt": utc_now(),
            "completedAt": None,
            "status": "in-progress",
            "clockPolicy": "Actual UTC timestamps are recorded at each provider response; no project-date substitution is used.",
        },
        "rawRunPrivatePath": record_private_path(run_raw),
        "corpusProvenance": corpus_provenance,
        "providerPolicy": {
            "primary": "OpenAlex CC0",
            "auxiliary": ["Semantic Scholar coverage sensitivity", "Crossref limited DOI/deposited references"],
            "edgeDirection": "citing -> cited",
            "externalNodePolicy": "All exact provider-declared citing/cited works become nodes; the graph is not restricted to the 68 seeds.",
            "mergePolicy": "Exact provider IDs and exact DOI/arXiv identifiers only; title-only merging is forbidden.",
            "rawStorage": "Exact response bytes are stored under ignored private_data and referenced by SHA-256.",
            "failurePolicy": "Every unresolved seed, failed page, remaining cursor/offset, count mismatch, provider cap, or circuit-open direction stays explicit; no failure is converted to an empty page.",
            "secretPolicy": "Public URLs remove api_key/key/mailto and public metadata records only credential presence.",
            "xpacPolicy": "Use official OpenAlex is_xpac when returned; otherwise derive membership via include_xpac=true versus include_xpac=false. XPAC is not a relevance taxonomy.",
        },
        "providers": {
            "openalex": {"status": "not-run", "coverage": "partial"},
            "semanticScholar": {"status": "not-run", "coverage": "partial"},
            "crossref": {"status": "not-run", "coverage": "partial"},
        },
        "records": {},
    }


def finalize_window(manifest: dict[str, Any]) -> int:
    providers = manifest["providers"]
    complete = (
        providers.get("openalex", {}).get("coverage") == "complete"
        and providers.get("semanticScholar", {}).get("coverage") == "complete"
        and providers.get("crossref", {}).get("coverage") == "complete-limited-scope"
    )
    manifest["retrievalWindow"]["completedAt"] = utc_now()
    manifest["retrievalWindow"]["status"] = "complete" if complete else "partial"
    return 0 if complete else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        action="append",
        choices=("openalex", "s2", "crossref"),
        default=[],
        help="Provider(s) to run; default is all three",
    )
    parser.add_argument(
        "--new-window",
        action="store_true",
        help="Start a new timestamped raw run; required when replacing a v1 manifest",
    )
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=None,
        help=(
            "Private raw response directory (default: <private root>/literature/citation_raw; "
            "the private root is CUA_PRIVATE_ROOT or the git-ignored private_data/)"
        ),
    )
    parser.add_argument(
        "--s2-interval",
        type=float,
        default=float(
            os.environ.get("CUA_S2_REQUEST_INTERVAL")
            or os.environ.get("CUI_S2_REQUEST_INTERVAL", "0.8")
        ),
        help="Seconds between successful S2 pagination requests",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    selected = set(args.provider or {"openalex", "s2", "crossref"})
    _, all_rows, corpus_provenance = load_corpus_rows()
    paper_rows = [row for row in all_rows if not is_repository(row)]
    if len(paper_rows) != EXPECTED_PAPERS:
        raise ValueError("Citation retrieval requires the frozen 64-paper corpus")

    if args.raw_root is None:
        args.raw_root = private_data_root().joinpath(*RAW_SUBDIR)
    previous = read_json(args.manifest) if args.manifest.exists() else None
    hints = previous_openalex_hints(previous)
    if args.new_window:
        run_id = run_id_now()
        run_raw = args.raw_root / run_id
        manifest = new_manifest(run_id, run_raw, corpus_provenance)
    else:
        if not previous or previous.get("schemaVersion") != SCHEMA_VERSION:
            raise ValueError("Use --new-window to initialize the v2 citation manifest")
        manifest = previous
        run_raw = resolve_private_path(str(manifest["rawRunPrivatePath"]))
        manifest["retrievalWindow"]["completedAt"] = None
        manifest["retrievalWindow"]["status"] = "in-progress"

    def checkpoint() -> None:
        write_json_atomic(args.manifest, manifest)

    checkpoint()
    if "openalex" in selected:
        collect_openalex(
            paper_rows,
            os.environ.get("OPENALEX_API_KEY") or None,
            run_raw,
            manifest,
            checkpoint,
            hints,
        )
    if "s2" in selected:
        collect_semantic_scholar(
            paper_rows,
            os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or None,
            run_raw,
            manifest,
            checkpoint,
            args.s2_interval,
        )
    if "crossref" in selected:
        collect_crossref(
            paper_rows,
            os.environ.get("CROSSREF_MAILTO") or None,
            run_raw,
            manifest,
            checkpoint,
        )

    exit_code = finalize_window(manifest)
    checkpoint()
    print(
        json.dumps(
            {
                "manifest": args.manifest.as_posix(),
                "runId": manifest["retrievalWindow"]["runId"],
                "status": manifest["retrievalWindow"]["status"],
                "providers": manifest["providers"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
