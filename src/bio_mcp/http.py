"""The one HTTP client. Every outbound request in bio-mcp goes through here.

Owns retries, disk caching, the global concurrency semaphore, the user
agent, and timeouts, so these are solved once instead of per source
(design.md section 4). Source modules never call `httpx` directly.

Policy, per design.md section 3 and scope.md section 7:

- 429 and 5xx are retried with exponential backoff. 4xx fails immediately —
  retrying a bad request just wastes the upstream's time.
- Responses are cached on disk, keyed by the full request. A broken cache
  must never break a query, so every cache read/write is wrapped in
  try/except.
- Concurrency is capped by a single process-wide semaphore, not one per
  source, so a `gene_evidence` call fanning out to two sources still looks
  polite to each of them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from bio_mcp.errors import SourceError

logger = logging.getLogger("bio_mcp.http")

USER_AGENT = "bio-mcp/0.1 (+https://github.com/Siavashghaffari/Bio-MCP)"
DEFAULT_TIMEOUT = 15.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
CACHE_TTL_SECONDS = 3600
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

CACHE_DIR = Path(os.environ.get("BIO_MCP_CACHE_DIR", Path.home() / ".cache" / "bio-mcp"))

# One process-wide concurrency cap, shared by every source (design.md section 3).
_MAX_CONCURRENCY = int(os.environ.get("BIO_MCP_MAX_CONCURRENCY", "4"))
_semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

# One shared client, created lazily on first use (design.md section 1: "one
# HTTP client"). httpx connection pooling makes this meaningfully cheaper
# than a client per request.
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = httpx.AsyncClient(headers={"User-Agent": USER_AGENT})
    return _client


async def aclose() -> None:
    """Close the shared client. Call on process shutdown; safe to call twice."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _cache_key(method: str, url: str, params: dict[str, Any] | None, body: Any) -> str:
    payload = json.dumps(
        {"method": method, "url": url, "params": params or {}, "body": body},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _cache_read(key: str) -> Any | None:
    try:
        path = _cache_path(key)
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - record["cached_at"] > CACHE_TTL_SECONDS:
            return None
        return record["data"]
    except Exception:
        # A broken cache must never break a query (design.md section 3).
        logger.debug("cache read failed for key %s", key, exc_info=True)
        return None


def _cache_write(key: str, data: Any) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps({"cached_at": time.time(), "data": data}), encoding="utf-8")
        tmp.replace(_cache_path(key))
    except Exception:
        logger.debug("cache write failed for key %s", key, exc_info=True)


async def _request(
    source: str,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    cache: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    key = _cache_key(method, url, params, json_body)
    if cache:
        hit = _cache_read(key)
        if hit is not None:
            return hit

    client = await _get_client()
    last_exc: Exception | None = None

    async with _semaphore:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await client.request(
                    method, url, params=params, json=json_body, timeout=timeout
                )
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt >= MAX_ATTEMPTS:
                    raise SourceError(source, f"network error calling {url}: {exc}") from exc
                await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue

            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue

            if resp.status_code >= 400:
                raise SourceError(
                    source, f"{url} returned HTTP {resp.status_code}: {resp.text[:200]}"
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise SourceError(source, f"{url} returned a non-JSON response") from exc

            if cache:
                _cache_write(key, data)
            return data

    # Unreachable in practice: every branch above returns or raises. Kept as
    # a defensive fallback so a future refactor cannot silently swallow a
    # failure.
    raise SourceError(source, f"{url} failed after {MAX_ATTEMPTS} attempts: {last_exc}")


async def get_json(
    source: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    cache: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """GET `url` and return parsed JSON.

    Retries 429/5xx with exponential backoff, fails immediately on 4xx,
    and serves/saves a disk cache unless `cache=False`.
    """
    return await _request(source, "GET", url, params=params, cache=cache, timeout=timeout)


async def post_json(
    source: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    cache: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """POST `url` and return parsed JSON. Same retry/cache policy as `get_json`.

    Caching a POST is safe here because bio-mcp only ever POSTs read-only
    search bodies (BioGRID ORCS accepts GET or POST for the same lookups) —
    never anything mutating.
    """
    return await _request(
        source, "POST", url, params=params, json_body=json_body, cache=cache, timeout=timeout
    )
