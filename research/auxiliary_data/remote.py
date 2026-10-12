"""Strict public HTTP range access without credentials or signed-URL logging."""

from __future__ import annotations

import io
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests


CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
# Every request URL and every redirect hop must be HTTPS on one of these hosts.
# Entries starting with "." match subdomains of a provider-controlled domain
# (Hugging Face CDN and Xet storage hosts); all other entries match exactly.
# A generic cloud-storage suffix (for example any *.amazonaws.com bucket) is
# deliberately absent: a provider redirect elsewhere fails closed.
ALLOWED_REDIRECT_HOST_SUFFIXES = (
    "huggingface.co",
    ".huggingface.co",
    ".hf.co",
    "userinterfaces.aalto.fi",
)
USER_AGENT = (
    "ComputerUseAgent-research/auxiliary-data (+https://github.com/appleweiping/ComputerUseAgent)"
)


class PublicAccessError(RuntimeError):
    """A sanitized public-source access failure."""


@dataclass(frozen=True)
class RangeAccessStats:
    request_count: int
    transferred_bytes: int


def _allowed_public_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    return any(
        host.endswith(suffix) if suffix.startswith(".") else host == suffix
        for suffix in ALLOWED_REDIRECT_HOST_SUFFIXES
    )


class _AllowlistedSession(requests.Session):
    """Refuse a redirect before it is followed unless its target is allowlisted."""

    def get_redirect_target(self, resp: requests.Response) -> str | None:
        target = super().get_redirect_target(resp)
        if target is not None and not _allowed_public_url(urljoin(resp.url, target)):
            raise PublicAccessError("provider redirected to a non-allowlisted HTTPS host")
        return target


def public_session() -> requests.Session:
    session = _AllowlistedSession()
    # Deliberately no Authorization header: these tracks must remain public-only.
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _validate_redirects(response: requests.Response) -> None:
    for hop in [*response.history, response]:
        if not _allowed_public_url(hop.url):
            raise PublicAccessError("provider redirected to a non-allowlisted HTTPS host")


def get_public_json(url: str, *, attempts: int = 5) -> dict[str, Any]:
    if not _allowed_public_url(url):
        raise PublicAccessError("public JSON URL is outside the provider allowlist")
    session = public_session()
    try:
        for attempt in range(attempts):
            try:
                response = session.get(url, timeout=120)
                _validate_redirects(response)
                if response.status_code != 200:
                    raise PublicAccessError(f"provider returned HTTP {response.status_code}")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise PublicAccessError("provider JSON response is not an object")
                return payload
            except (requests.RequestException, ValueError, PublicAccessError) as exc:
                if attempt + 1 == attempts:
                    if isinstance(exc, PublicAccessError):
                        raise
                    raise PublicAccessError("public JSON request failed after retries") from None
                time.sleep(2**attempt)
    finally:
        session.close()
    raise AssertionError("unreachable")


def get_public_bytes(url: str, *, attempts: int = 5) -> bytes:
    if not _allowed_public_url(url):
        raise PublicAccessError("public byte URL is outside the provider allowlist")
    session = public_session()
    try:
        for attempt in range(attempts):
            try:
                response = session.get(url, timeout=120)
                _validate_redirects(response)
                if response.status_code != 200:
                    raise PublicAccessError(f"provider returned HTTP {response.status_code}")
                return response.content
            except (requests.RequestException, PublicAccessError) as exc:
                if attempt + 1 == attempts:
                    if isinstance(exc, PublicAccessError):
                        raise
                    raise PublicAccessError("public byte request failed after retries") from None
                time.sleep(2**attempt)
    finally:
        session.close()
    raise AssertionError("unreachable")


def head_public(url: str, *, attempts: int = 5) -> dict[str, Any]:
    if not _allowed_public_url(url):
        raise PublicAccessError("public HEAD URL is outside the provider allowlist")
    session = public_session()
    try:
        for attempt in range(attempts):
            try:
                response = session.head(url, allow_redirects=True, timeout=120)
                _validate_redirects(response)
                if response.status_code != 200:
                    raise PublicAccessError(f"provider returned HTTP {response.status_code}")
                return {
                    "status_code": response.status_code,
                    "content_length": int(response.headers["Content-Length"]),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "accept_ranges": response.headers.get("Accept-Ranges"),
                    "content_type": response.headers.get("Content-Type"),
                }
            except (requests.RequestException, KeyError, ValueError, PublicAccessError) as exc:
                if attempt + 1 == attempts:
                    if isinstance(exc, PublicAccessError):
                        raise
                    raise PublicAccessError("public HEAD request failed after retries") from None
                time.sleep(2**attempt)
    finally:
        session.close()
    raise AssertionError("unreachable")


class PublicRangeReader(io.RawIOBase):
    """Seekable HTTP reader with strict 206 checks and bounded forward prefetch."""

    def __init__(
        self,
        url: str,
        size: int,
        *,
        prefetch_bytes: int = 512 * 1024,
        cache_entries: int = 8,
        attempts: int = 5,
    ) -> None:
        super().__init__()
        if not _allowed_public_url(url):
            raise PublicAccessError("range URL is outside the provider allowlist")
        if size <= 0:
            raise ValueError("range source size must be positive")
        self.public_url = url
        self.size = size
        self.position = 0
        self.prefetch_bytes = prefetch_bytes
        self.cache_entries = cache_entries
        self.attempts = attempts
        self.session = public_session()
        self.cache: OrderedDict[int, tuple[int, int, bytes]] = OrderedDict()
        self.request_count = 0
        self.transferred_bytes = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self.position + offset
        elif whence == io.SEEK_END:
            target = self.size + offset
        else:
            raise ValueError(f"unsupported whence: {whence}")
        if target < 0:
            raise ValueError("negative seek position")
        self.position = target
        return target

    def _cached(self, start: int, end: int) -> bytes | None:
        for key, (cached_start, cached_end, data) in list(self.cache.items()):
            if cached_start <= start and end <= cached_end:
                self.cache.move_to_end(key)
                return data[start - cached_start : end - cached_start]
        return None

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self.size - self.position
        if size == 0 or self.position >= self.size:
            return b""
        size = min(size, self.size - self.position)
        start = self.position
        end = start + size
        cached = self._cached(start, end)
        if cached is not None:
            self.position = end
            return cached

        request_end = min(self.size, start + max(size, self.prefetch_bytes))
        range_header = f"bytes={start}-{request_end - 1}"
        for attempt in range(self.attempts):
            try:
                response = self.session.get(
                    self.public_url,
                    headers={"Range": range_header},
                    timeout=120,
                )
                _validate_redirects(response)
                content_range = response.headers.get("Content-Range", "")
                match = CONTENT_RANGE.fullmatch(content_range)
                if response.status_code != 206 or match is None:
                    raise PublicAccessError("provider did not honor an exact byte range")
                returned_start, returned_end, returned_total = map(int, match.groups())
                if (returned_start, returned_end, returned_total) != (
                    start,
                    request_end - 1,
                    self.size,
                ):
                    raise PublicAccessError("provider returned a mismatched Content-Range")
                data = response.content
                if len(data) != request_end - start:
                    raise PublicAccessError("provider returned a mismatched byte count")
                break
            except (requests.RequestException, PublicAccessError) as exc:
                if attempt + 1 == self.attempts:
                    if isinstance(exc, PublicAccessError):
                        raise
                    raise PublicAccessError("range request failed after retries") from None
                time.sleep(2**attempt)
        else:
            raise AssertionError("unreachable")

        self.request_count += 1
        self.transferred_bytes += len(data)
        self.cache[start] = (start, request_end, data)
        while len(self.cache) > self.cache_entries:
            self.cache.popitem(last=False)
        self.position = end
        return data[:size]

    @property
    def access_stats(self) -> RangeAccessStats:
        return RangeAccessStats(self.request_count, self.transferred_bytes)

    def close(self) -> None:
        if not self.closed:
            session = getattr(self, "session", None)
            if session is not None:
                session.close()
            cache = getattr(self, "cache", None)
            if cache is not None:
                cache.clear()
        super().close()
