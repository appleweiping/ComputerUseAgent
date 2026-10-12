"""Collect and verify full-text evidence for the 64-paper corpus.

Raw HTML/PDF bytes are stored under ignored ``private_data``.  The tracked
manifest contains only provenance, hashes, page/section inventories, and access
outcomes.  A run is fail-closed: any of the 60 expected accessible papers that
cannot be verified leaves an explicit error record and a non-zero exit status.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - exercised by explicit runtime check
    PdfReader = None  # type: ignore[assignment,misc]

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from corpus_common import (  # type: ignore[import-not-found]
        ACCESS_BLOCKED_DOIS,
        SNAPSHOTS_DIR,
        SOURCES_DIR,
        doi_from_persistent_id,
        is_repository,
        load_corpus_rows,
        normalize_persistent_id,
        private_data_root,
        read_json,
        record_private_path,
        slug_for_id,
    )
else:
    from .corpus_common import (
        ACCESS_BLOCKED_DOIS,
        SNAPSHOTS_DIR,
        SOURCES_DIR,
        doi_from_persistent_id,
        is_repository,
        load_corpus_rows,
        normalize_persistent_id,
        private_data_root,
        read_json,
        record_private_path,
        slug_for_id,
    )


OVERRIDES_PATH = SOURCES_DIR / "fulltext_access_overrides.json"
BROWSER_REVIEWS_PATH = SOURCES_DIR / "browser_fulltext_reviews.json"
MANIFEST_PATH = SNAPSHOTS_DIR / "fulltext_evidence_manifest.json"
#: Raw full-text bytes, below the private root (``CUA_PRIVATE_ROOT`` or the
#: git-ignored ``private_data/``); recorded as ``private_data/literature/fulltext_raw``.
RAW_SUBDIR = ("literature", "fulltext_raw")
USER_AGENT = "ComputerUseAgent-literature-review/1.1 (academic provenance audit; +https://github.com/appleweiping/ComputerUseAgent)"

MEANINGFUL_HEADING = re.compile(
    r"\b(method|approach|system|dataset|data|experiment|evaluation|result|"
    r"analysis|discussion|limitation|conclusion|threat|feature|benchmark)\w*\b",
    re.IGNORECASE,
)


def safe_license_url(value: str) -> str | None:
    """Return a normalized absolute HTTP(S) license URL, or reject it."""

    candidate = html.unescape(value).strip()
    if not candidate or any(ord(character) < 32 for character in candidate):
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return candidate


def is_creative_commons_url(value: str) -> bool:
    """Match Creative Commons by parsed hostname, never by URL substring."""

    hostname = (urlparse(value).hostname or "").rstrip(".").lower()
    return hostname == "creativecommons.org" or hostname.endswith(".creativecommons.org")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ReviewHTMLParser(HTMLParser):
    """Small dependency-free extractor for headings, visible text, and license links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self._hidden_depth = 0
        self.headings: list[dict[str, str]] = []
        self.text_parts: list[str] = []
        self.license_links: list[dict[str, str]] = []
        self._link_href: str | None = None
        self._link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self._hidden_depth += 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_tag = tag
            self._heading_parts = []
        if tag == "a":
            self._link_href = attributes.get("href")
            self._link_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._hidden_depth:
            self._hidden_depth -= 1
        if tag == self._heading_tag:
            value = " ".join(" ".join(self._heading_parts).split())
            if value:
                self.headings.append({"level": tag, "text": value})
            self._heading_tag = None
            self._heading_parts = []
        if tag == "a":
            label = " ".join(" ".join(self._link_parts).split())
            href = safe_license_url(self._link_href or "")
            if href is not None and (
                "license" in label.lower() or is_creative_commons_url(href)
            ):
                self.license_links.append({"label": label, "url": href})
            self._link_href = None
            self._link_parts = []

    def handle_data(self, data: str) -> None:
        if self._hidden_depth:
            return
        value = html.unescape(data).strip()
        if not value:
            return
        self.text_parts.append(value)
        if self._heading_tag:
            self._heading_parts.append(value)
        if self._link_href is not None:
            self._link_parts.append(value)


def title_tokens(title: str) -> list[str]:
    stop = {"this", "that", "with", "from", "into", "using", "towards", "based"}
    return [
        token
        for token in re.findall(r"[a-z0-9]+", title.lower())
        if len(token) >= 4 and token not in stop
    ][:8]


def verify_title(title: str, text: str) -> dict[str, Any]:
    tokens = title_tokens(title)
    haystack = text[:30000].lower()
    found = [token for token in tokens if token in haystack]
    required = min(2, len(tokens))
    return {
        "candidateTokens": tokens,
        "matchedTokens": found,
        "requiredMatches": required,
        "passed": len(found) >= required,
    }


def content_extension(content_type: str, body: bytes, url: str) -> str:
    if body.startswith(b"%PDF") or "application/pdf" in content_type.lower():
        return ".pdf"
    if "html" in content_type.lower() or b"<html" in body[:2000].lower():
        return ".html"
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".pdf", ".html", ".htm"} else ".bin"


def extract_pdf(body: bytes) -> tuple[str, dict[str, Any]]:
    if PdfReader is None:
        raise RuntimeError("pypdf is required to verify PDF full text")
    reader = PdfReader(io.BytesIO(body), strict=False)
    pages: list[str] = []
    page_inventory: list[dict[str, Any]] = []
    for index, page in enumerate(reader.pages, start=1):
        value = page.extract_text() or ""
        normalized = " ".join(value.split())
        pages.append(value)
        page_inventory.append(
            {
                "page": index,
                "textCharacters": len(value),
                "textSha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            }
        )
    text = "\n".join(pages)
    locator_candidates: list[dict[str, Any]] = []
    numbered_heading = re.compile(
        r"(?im)^\s*((?:\d+(?:\.\d+)*|[A-Z])\.?\s+"
        r"(?:METHOD|APPROACH|SYSTEM|DATA|DATASET|EXPERIMENT|EVALUATION|RESULT|"
        r"ANALYSIS|DISCUSSION|LIMITATION|CONCLUSION|THREAT|FEATURE|BENCHMARK)"
        r"[^\n]{0,100})"
    )
    for page_index, page_text in enumerate(pages, start=1):
        for match in numbered_heading.finditer(page_text):
            heading = " ".join(match.group(1).split())
            item = {"page": page_index, "text": heading}
            if item not in locator_candidates:
                locator_candidates.append(item)
            if len(locator_candidates) >= 40:
                break
        if len(locator_candidates) >= 40:
            break
    return text, {
        "pageCount": len(reader.pages),
        "pages": page_inventory,
        "locatorCandidates": locator_candidates,
    }


def extract_html(body: bytes, content_type: str) -> tuple[str, dict[str, Any]]:
    charset_match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    charset = charset_match.group(1) if charset_match else "utf-8"
    decoded = body.decode(charset, errors="replace")
    parser = ReviewHTMLParser()
    parser.feed(decoded)
    text = "\n".join(parser.text_parts)
    headings = parser.headings[:200]
    return text, {
        "headings": headings,
        "locatorCandidates": [
            item for item in headings if MEANINGFUL_HEADING.search(item["text"])
        ][:60],
        "licenseLinks": parser.license_links[:20],
    }


def fetch(url: str, retries: int = 3) -> tuple[bytes, str, str, int]:
    error: Exception | None = None
    for attempt in range(retries):
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.1",
            },
        )
        try:
            with urlopen(request, timeout=60) as response:
                body = response.read()
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                status = getattr(response, "status", 200)
                return body, final_url, content_type, status
        except HTTPError as exc:
            error = exc
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else float(2**attempt)
                time.sleep(min(delay, 15.0))
                continue
            if 500 <= exc.code < 600 and attempt + 1 < retries:
                time.sleep(float(2**attempt))
                continue
            break
        except (TimeoutError, URLError) as exc:
            error = exc
            if attempt + 1 < retries:
                time.sleep(float(2**attempt))
                continue
            break
    raise RuntimeError(f"Fetch failed for {url}: {type(error).__name__}: {error}")


def load_overrides() -> dict[str, Any]:
    payload = read_json(OVERRIDES_PATH)
    overrides = payload.get("overrides")
    if not isinstance(overrides, dict):
        raise ValueError("fulltext_access_overrides.json has no overrides object")
    return payload


def load_browser_reviews() -> dict[str, dict[str, Any]]:
    payload = read_json(BROWSER_REVIEWS_PATH)
    reviews = payload.get("reviews")
    if not isinstance(reviews, dict):
        raise ValueError("browser_fulltext_reviews.json has no reviews object")
    return {
        normalize_persistent_id(key): value
        for key, value in reviews.items()
        if isinstance(value, dict)
    }


def browser_review_record(
    row: dict[str, Any], review: dict[str, Any]
) -> dict[str, Any]:
    if not review.get("titleVerified"):
        raise ValueError(f"Browser review did not verify title for {row['Persistent ID']}")
    locators = review.get("locatorInventory")
    if not isinstance(locators, list) or len(locators) < 2:
        raise ValueError(f"Browser review has insufficient locators for {row['Persistent ID']}")
    return {
        "persistentId": normalize_persistent_id(str(row["Persistent ID"])),
        "title": row["Title"],
        "status": "verified-full-text-browser-review",
        "requestedUrl": review["sourceUrl"],
        "finalUrl": review["sourceUrl"],
        "accessedAt": review["reviewedAt"],
        "mediaKind": "embedded-browser-render",
        "byteHashAvailable": False,
        "accessOutcome": review["accessOutcome"],
        "fullTextExtent": review["fullTextExtent"],
        "titleVerification": {"passed": True, "method": "curator visual review"},
        "extraction": {"locatorCandidates": locators},
        "license": review["license"],
        "licenseEvidenceUrl": review["licenseEvidenceUrl"],
        "reuseBoundary": review["reuseBoundary"],
    }


def source_candidates(row: dict[str, Any], override: dict[str, Any] | None) -> list[str]:
    persistent_id = normalize_persistent_id(str(row["Persistent ID"]))
    if override:
        return [str(override["fullTextUrl"])]
    if persistent_id.startswith("arXiv:"):
        arxiv_id = persistent_id.split(":", 1)[1]
        return [f"https://arxiv.org/html/{arxiv_id}", f"https://arxiv.org/pdf/{arxiv_id}"]
    return [str(row["Official Paper URL"] or row["Primary Source URL"])]


def collect_one(
    row: dict[str, Any],
    override: dict[str, Any] | None,
    raw_dir: Path,
) -> dict[str, Any]:
    persistent_id = normalize_persistent_id(str(row["Persistent ID"]))
    failures: list[str] = []
    for url in source_candidates(row, override):
        try:
            body, final_url, content_type, http_status = fetch(url)
            extension = content_extension(content_type, body, final_url)
            if extension == ".pdf":
                text, extraction = extract_pdf(body)
                media_kind = "pdf"
            elif extension in {".html", ".htm"}:
                text, extraction = extract_html(body, content_type)
                media_kind = "html"
            else:
                raise RuntimeError(
                    f"Unsupported media type {content_type!r}; magic={body[:16]!r}"
                )
            title_check = verify_title(str(row["Title"]), text)
            if len(text) < 2500:
                raise RuntimeError(f"Extracted text too short ({len(text)} characters)")
            if not title_check["passed"]:
                raise RuntimeError(
                    "Title verification failed: "
                    f"matched={title_check['matchedTokens']}, "
                    f"candidates={title_check['candidateTokens']}"
                )

            raw_path = raw_dir / f"{slug_for_id(persistent_id)}{extension}"
            raw_path.write_bytes(body)
            accessed_at = utc_now()
            record: dict[str, Any] = {
                "persistentId": persistent_id,
                "title": row["Title"],
                "status": "verified-full-text",
                "requestedUrl": url,
                "finalUrl": final_url,
                "httpStatus": http_status,
                "accessedAt": accessed_at,
                "mediaKind": media_kind,
                "contentType": content_type,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "rawPrivatePath": record_private_path(raw_path),
                "extractedTextCharacters": len(text),
                "extractedTextSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "titleVerification": title_check,
                "extraction": extraction,
                "accessCopyKind": (
                    override.get("accessCopyKind") if override else "arXiv full text"
                ),
                "license": (
                    override.get("license")
                    if override
                    else "machine-read-from-arxiv-page-when-present"
                ),
                "licenseEvidenceUrl": (
                    override.get("licenseEvidenceUrl")
                    if override
                    else str(row["Provenance URL"])
                ),
                "reuseBoundary": (
                    override.get("reuseBoundary")
                    if override
                    else "Use the license link recorded in extraction.licenseLinks; "
                    "if absent, cite/quote only and do not redistribute figures by default."
                ),
            }
            if failures:
                record["candidateFailuresBeforeSuccess"] = failures
            return record
        except Exception as exc:  # keep provider failures explicit in the manifest
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
    return {
        "persistentId": persistent_id,
        "title": row["Title"],
        "status": "fetch-or-verification-failed",
        "attemptedAt": utc_now(),
        "candidateFailures": failures,
        "license": override.get("license") if override else "not-resolved",
        "reuseBoundary": override.get("reuseBoundary") if override else "not-resolved",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=MANIFEST_PATH,
        help="Tracked sanitized manifest output path",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=None,
        help=(
            "Private raw full-text directory (default: <private root>/literature/fulltext_raw; "
            "the private root is CUA_PRIVATE_ROOT or the git-ignored private_data/)"
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Collect only the named Persistent ID (repeatable; diagnostic use)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refetch records that are already verified in the existing manifest",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _, rows, provenance = load_corpus_rows()
    access_payload = load_overrides()
    browser_reviews = load_browser_reviews()
    overrides = {
        normalize_persistent_id(key): value
        for key, value in access_payload["overrides"].items()
    }
    only = {normalize_persistent_id(value) for value in args.only}
    if args.raw_dir is None:
        args.raw_dir = private_data_root().joinpath(*RAW_SUBDIR)
    args.raw_dir.mkdir(parents=True, exist_ok=True)

    previous_records: dict[str, dict[str, Any]] = {}
    if args.manifest.exists() and not args.refresh:
        previous = read_json(args.manifest)
        previous_records = {
            normalize_persistent_id(str(record["persistentId"])): record
            for record in previous.get("records", [])
            if isinstance(record, dict) and "persistentId" in record
        }

    records: list[dict[str, Any]] = []
    for row in rows:
        persistent_id = normalize_persistent_id(str(row["Persistent ID"]))
        if only and persistent_id not in only:
            previous_record = previous_records.get(persistent_id)
            if previous_record:
                records.append(previous_record)
            continue
        if is_repository(row):
            records.append(
                {
                    "persistentId": persistent_id,
                    "title": row["Title"],
                    "status": "repository-not-paper",
                    "reviewEvidence": row["Evidence Locator"],
                }
            )
            continue
        doi = doi_from_persistent_id(persistent_id)
        if doi in ACCESS_BLOCKED_DOIS:
            records.append(
                {
                    "persistentId": persistent_id,
                    "title": row["Title"],
                    "status": "access-blocked",
                    "recordUrl": row["Provenance URL"],
                    "reviewBoundary": access_payload["policy"]["blockedMeaning"],
                }
            )
            continue
        if persistent_id in browser_reviews:
            records.append(browser_review_record(row, browser_reviews[persistent_id]))
            continue
        previous_record = previous_records.get(persistent_id)
        if (
            previous_record
            and str(previous_record.get("status", "")).startswith("verified-full-text")
        ):
            records.append(previous_record)
            continue
        records.append(collect_one(row, overrides.get(persistent_id), args.raw_dir))

    counts: dict[str, int] = {}
    for record in records:
        status = str(record["status"])
        counts[status] = counts.get(status, 0) + 1
    payload = {
        "schemaVersion": "cui-literature-fulltext-evidence-manifest-v1",
        "retrievalRun": {
            "startedOrCompletedAt": utc_now(),
            "userAgent": USER_AGENT,
            "rawStoragePolicy": "Raw HTML/PDF bytes are ignored under private_data; only hashes and structural inventories are tracked.",
            "failurePolicy": "fail-closed: any expected accessible paper without verified full text returns non-zero",
        },
        "corpusProvenance": provenance,
        "accessPolicy": access_payload["policy"],
        "counts": counts,
        "records": sorted(records, key=lambda item: str(item["persistentId"]).lower()),
    }
    write_json_atomic(args.manifest, payload)

    preserving_full_manifest = bool(only and len(previous_records) == len(rows))
    expected_full_text = (
        60
        if not only or preserving_full_manifest
        else sum(
            1
            for row in rows
            if normalize_persistent_id(str(row["Persistent ID"])) in only
            and not is_repository(row)
            and doi_from_persistent_id(str(row["Persistent ID"])) not in ACCESS_BLOCKED_DOIS
        )
    )
    verified = sum(
        count for status, count in counts.items() if status.startswith("verified-full-text")
    )
    failures = counts.get("fetch-or-verification-failed", 0)
    print(
        json.dumps(
            {
                "manifest": str(args.manifest),
                "expectedVerifiedFullText": expected_full_text,
                "verifiedFullText": verified,
                "failures": failures,
                "counts": counts,
            },
            sort_keys=True,
        )
    )
    return 0 if verified == expected_full_text and failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
