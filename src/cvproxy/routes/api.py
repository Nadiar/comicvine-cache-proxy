"""ComicVine-compatible API routes.

The upstream CV API returns JSON with this envelope::

    {
      "status_code": 1,
      "error": "OK",
      "number_of_total_results": 42,
      "number_of_page_results": 10,
      "limit": 10,
      "offset": 0,
      "results": [ ... ]
    }

We replicate that envelope so consuming tools (ComicTagger, Mylar, etc.)
see exactly the same response shape they expect from comicvine.gamespot.com.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any
from urllib.parse import urlencode, parse_qs, urlparse

from fastapi import APIRouter, Query, Request, Response

from cvproxy.config import get_settings
from cvproxy.cv_client import CVClient, RATE_LIMITED
from cvproxy.db import Database, is_past_cutoff
from cvproxy.image_cache import ImageCache
from cvproxy.stats import StatsTracker

logger = logging.getLogger(__name__)


async def _refresh_volumes_if_stale(
    client: CVClient,
    db: Database,
    filter_str: str,
    local_total: int,
) -> bool:
    """If upstream has more volumes than local cache, fetch all pages and upsert them.

    Returns True if a refresh was performed, False otherwise.
    """
    try:
        probe = await client.get("volumes/", {"filter": filter_str, "limit": 1, "offset": 0})
        if probe is RATE_LIMITED or not probe:
            return False
        upstream_total: int = probe.get("number_of_total_results", 0)
        if upstream_total <= local_total:
            return False
        logger.info(
            "Stale cache detected: local=%d upstream=%d filter=%r — refreshing before response",
            local_total, upstream_total, filter_str,
        )
        page_limit = 100
        fetched = 0
        while fetched < upstream_total:
            page_data = await client.get(
                "volumes/", {"filter": filter_str, "limit": page_limit, "offset": fetched}
            )
            if page_data is RATE_LIMITED or not page_data:
                break
            page_results: list[dict[str, Any]] = page_data.get("results") or []
            if not page_results:
                break
            for vol in page_results:
                try:
                    db.upsert_volume(vol)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Refresh upsert failed for volume %s: %s", vol.get("id"), exc)
            fetched += len(page_results)
            if len(page_results) < page_limit:
                break
        logger.info(
            "Cache refresh complete: upserted up to %d volumes for filter=%r",
            fetched, filter_str,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Volume cache refresh failed for filter=%r: %s", filter_str, exc)
        return False

router = APIRouter(prefix="/api")

# ---------------------------------------------------------------------------
# Rate limit helpers
# ---------------------------------------------------------------------------

#: When total upstream calls in the past hour reach this value we stop
#: forwarding to the upstream and return a rate-limit busy response instead,
#: reserving headroom for manual/diagnostic queries.
_RATE_LIMIT_UPSTREAM_THRESHOLD = 190


def _cv_rate_limit_error() -> dict[str, Any]:
    """Return a CV-compatible rate limit error envelope (status_code 107)."""
    return {
        "status_code": 107,
        "error": "Rate Limit Exceeded",
        "number_of_total_results": 0,
        "number_of_page_results": 0,
        "limit": 0,
        "offset": 0,
        "results": [],
    }


def _is_rate_limit_busy(client: CVClient) -> bool:
    """True when the total hourly upstream call count is at or above the threshold."""
    return client.total_hourly_calls() >= _RATE_LIMIT_UPSTREAM_THRESHOLD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cv_envelope(
    results: Any,
    *,
    total: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Wrap *results* in a CV-compatible JSON envelope."""
    is_list = isinstance(results, list)
    return {
        "status_code": 1,
        "error": "OK",
        "number_of_total_results": total if total is not None else (len(results) if is_list else 1),
        "number_of_page_results": len(results) if is_list else 1,
        "limit": limit,
        "offset": offset,
        "results": results,
    }


def _cv_error(message: str, status_code: int = 100) -> dict[str, Any]:
    return {
        "status_code": status_code,
        "error": message,
        "number_of_total_results": 0,
        "number_of_page_results": 0,
        "limit": 0,
        "offset": 0,
        "results": [],
    }


def _apply_field_list(data: dict[str, Any], field_list: str) -> dict[str, Any]:
    """Filter a result dict to only the fields named in field_list.

    Args:
        data: A single CV result dict (NOT the envelope).
        field_list: Comma-separated field names, e.g. "id,name,publisher".

    Returns:
        Filtered dict if field_list is non-empty, else the original dict unchanged.
    """
    if not field_list:
        return data
    fields = {f.strip() for f in field_list.split(",") if f.strip()}
    return {k: v for k, v in data.items() if k in fields}


def _apply_sort(results: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Sort results by CV sort spec 'field:asc' or 'field:desc'.

    Numeric values (e.g. issue_number) are sorted numerically; everything else
    is sorted as lowercase strings. None values sort last regardless of direction.
    """
    if not sort or not results:
        return results
    parts = sort.split(":")
    field = parts[0].strip()
    reverse = len(parts) > 1 and parts[1].strip().lower() == "desc"

    def _key(r: dict[str, Any]) -> tuple[int, float | str]:
        val = r.get(field)
        try:
            return (0, float(val))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return (1, str(val).lower())

    try:
        nones = [r for r in results if r.get(field) is None]
        non_nones = [r for r in results if r.get(field) is not None]
        return sorted(non_nones, key=_key, reverse=reverse) + nones
    except Exception:
        return results


def _parse_cv_id(raw: str) -> int:
    """Extract numeric ID from CV-style identifiers like ``4050-160294``.

    Returns 0 if the ID cannot be parsed (routes treat 0 as not-found).
    """
    try:
        if "-" in raw:
            return int(raw.rsplit("-", 1)[1])
        return int(raw)
    except (ValueError, IndexError):
        return 0


def _get_db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


def _get_client(request: Request) -> CVClient:
    return request.app.state.cv_client  # type: ignore[no-any-return]


def _get_cache(request: Request) -> ImageCache:
    return request.app.state.image_cache  # type: ignore[no-any-return]


def _get_cache_mode(request: Request) -> str:
    return request.headers.get("X-CV-Cache", "").lower()


def _get_stats(request: Request) -> StatsTracker | None:
    return getattr(request.app.state, "stats", None)


def _client_ip(request: Request) -> tuple[str, str | None]:
    """Return (client_ip, x_forwarded_for) from the request.

    When running behind a reverse proxy (e.g. Traefik), ``request.client.host``
    is the proxy's internal IP, not the real client IP.  We prefer — in order:

      1. First IP in ``X-Forwarded-For`` (proxy inserts real client IP here)
      2. ``X-Real-IP`` (set by some proxies instead of / in addition to XFF)
      3. ``request.client.host`` (direct TCP connection — proxy IP when behind one)

    The raw ``X-Forwarded-For`` header is returned unchanged as the second item
    for audit purposes.
    """
    forwarded = request.headers.get("x-forwarded-for")
    real_ip_header = request.headers.get("x-real-ip")

    if forwarded:
        # X-Forwarded-For may be "client, proxy1, proxy2" — leftmost is the
        # original client; rightmost is the most recently added proxy.
        ip = forwarded.split(",")[0].strip()
    elif real_ip_header:
        ip = real_ip_header.strip()
    else:
        ip = request.client.host if request.client else "unknown"

    return ip, forwarded or None


def _sanitised_url(request: Request) -> str:
    """Return the request path + query string with api_key stripped."""
    url = request.url
    path = url.path
    qs = parse_qs(urlparse(str(url)).query, keep_blank_values=True)
    qs.pop("api_key", None)
    if qs:
        # flatten single-value lists for cleaner output
        flat = {k: v[0] if len(v) == 1 else v for k, v in qs.items()}
        return f"{path}?{urlencode(flat, doseq=True)}"
    return path


def _record(
    request: Request,
    *,
    endpoint: str,
    source: str,
    start: float,
) -> None:
    """Record a request in the stats tracker (no-op if tracker not available)."""
    stats = _get_stats(request)
    if stats is None:
        return
    ip, forwarded = _client_ip(request)
    latency_ms = (time.monotonic() - start) * 1000
    stats.record(
        client_ip=ip,
        forwarded=forwarded,
        endpoint=endpoint,
        source=source,
        latency_ms=latency_ms,
        query_url=_sanitised_url(request),
    )


def _effective_offset(offset: int, page: int, limit: int) -> int:
    return (page - 1) * limit if page >= 1 else offset


def _touch_if_evicting(db: Database, table: str, entity_id: int) -> None:
    if get_settings().evict_older_than_years > 0:
        db.touch_last_accessed(table, entity_id)


async def _guarded_get(client: CVClient, coro: Any) -> Any:
    """Pre-flight busy-check before awaiting a CVClient coroutine.

    Closes the coroutine without running it when the hourly threshold is
    exceeded, returning RATE_LIMITED so callers have a single branch to handle.
    """
    if _is_rate_limit_busy(client):
        coro.close()
        return RATE_LIMITED
    return await coro


_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def _normalize_search_query(query: str) -> str:
    """Normalize a search query for cache-key generation.

    Strips leading articles and lowercases so that 'Amazing Spider-Man' and
    'The Amazing Spider-Man' share the same cache entry rather than hitting
    upstream twice and storing near-duplicate result sets.
    """
    return _LEADING_ARTICLE_RE.sub("", query.strip()).lower()


def _params_hash(params: dict[str, str]) -> str:
    """Compute a stable 16-char hex hash of a params dict for list cache keying.

    Excludes api_key, format, limit, offset, field_list — these don't affect
    which results are returned from the upstream API, only how they're presented.

    Args:
        params: Dict of query params (values should be strings).

    Returns:
        16-character lowercase hex string.
    """
    canonical = sorted(
        (k, v)
        for k, v in params.items()
        if k not in ("api_key", "format", "limit", "offset", "field_list") and v
    )
    return hashlib.md5(str(canonical).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Default field sets — what CV returns per endpoint when no field_list is given
# (verified against live CV API March 2026)
# ---------------------------------------------------------------------------

_VOLUME_LIST_FIELDS: frozenset[str] = frozenset({
    "aliases", "api_detail_url", "count_of_issues", "date_added", "date_last_updated",
    "deck", "description", "first_issue", "id", "image", "last_issue", "name",
    "publisher", "site_detail_url", "start_year",
})

_ISSUE_LIST_FIELDS: frozenset[str] = frozenset({
    "aliases", "api_detail_url", "associated_images", "cover_date", "date_added",
    "date_last_updated", "deck", "description", "has_staff_review", "id", "image",
    "issue_number", "name", "site_detail_url", "store_date", "volume",
})

_PUBLISHER_LIST_FIELDS: frozenset[str] = frozenset({
    "aliases", "api_detail_url", "date_added", "date_last_updated", "deck",
    "description", "id", "image", "location_address", "location_city",
    "location_state", "name", "site_detail_url",
})


def _apply_list_defaults(
    data: dict[str, Any],
    default_fields: frozenset[str],
    field_list: str,
) -> dict[str, Any]:
    """Apply field_list if given, else trim result to CV list endpoint defaults."""
    if field_list:
        return _apply_field_list(data, field_list)
    return {k: v for k, v in data.items() if k in default_fields}


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------


def _search_relevance_key(
    name: str | None, query: str, date: str | None = None
) -> tuple[int, int, int, str]:
    """Return a sort key (lower = more relevant) for a search result.

    Tier 0 — exact match (case-insensitive)
    Tier 1 — all query terms present; shorter names rank higher (closer to exact)
    Tier 2 — partial match; fewer missing terms and shorter names rank higher
    Tier 3 — no name

    Within a tier, more recent date_last_updated sorts first (negated ISO string).
    """
    # Invert ISO date so newer dates sort lower (ascending sort = descending date).
    # Each digit d → (9-d), non-digit chars kept as-is.  Missing date → "~"
    # which sorts after all digit strings, putting undated results last.
    neg_date = (
        "".join(str(9 - int(c)) if c.isdigit() else c for c in date)
        if date
        else "~"
    )

    if not name:
        return (3, 0, 0, neg_date)
    name_lower = name.lower()
    query_lower = query.lower()
    query_terms = query_lower.split()

    if name_lower == query_lower:
        return (0, 0, 0, neg_date)

    matched = sum(1 for t in query_terms if t in name_lower)
    if matched == len(query_terms):
        return (1, len(name), 0, neg_date)

    missing = len(query_terms) - matched
    return (2, missing, len(name), neg_date)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.get("/search/")
async def search(
    request: Request,
    query: str = Query(default="", alias="query"),
    resources: str = Query(default="volume", alias="resources"),
    limit: int = Query(default=100, alias="limit"),
    offset: int = Query(default=0, alias="offset"),
    page: int = Query(default=0, alias="page"),
    api_key: str = Query(default="", alias="api_key"),
    format: str = Query(default="json", alias="format"),
    field_list: str = Query(default="", alias="field_list"),
) -> dict[str, Any]:
    """CV-compatible search endpoint.

    Cache strategy:
    1. Check cv_response_cache for (query, resources) hash. The cache stores the
       FULL upstream result set (all pages combined) so any limit/offset can be
       served without a new upstream call.
    2. Stale check: if local FTS count > len(cached results), the local DB has
       grown (backfill jobs added new volumes) — invalidate and re-fetch.
    3. On miss/stale: auto-paginate upstream up to search_max_pages, combine all
       results, upsert each volume into cv_volume, store in cache.
    4. Build response: for each cached result ID, prefer fresh cv_volume data;
       fall back to cached upstream data for IDs not yet in local DB.
    5. Apply relevance sort across the full list, slice by limit/offset, apply
       field_list trimming.

    field_list is never forwarded upstream so the cached copy satisfies any
    future field_list variant without a new upstream call.
    """
    t0 = time.monotonic()
    effective_offset = _effective_offset(offset, page, limit)

    if not query:
        return _cv_error("Query is required")

    db = _get_db(request)
    cache_mode = _get_cache_mode(request)
    settings = get_settings()

    # Cache key: normalized query + resources (not limit/offset — we cache the full result
    # set and slice at serve time).  Leading articles are stripped so "amazing spider-man"
    # and "the amazing spider-man" share the same cache entry.
    norm_query = _normalize_search_query(query)
    params_hash = hashlib.md5(
        f"q:{norm_query}|r:{resources}".encode()
    ).hexdigest()

    cached_data: dict[str, Any] | None = None

    # --- 1. Check cache ---
    if cache_mode != "no-cache":
        cached_data = db.cache_get_search(params_hash)
        if cached_data is not None:
            # Stale check: if local FTS has more matching volumes than cache, re-fetch
            if "volume" in resources:
                local_count = db.count_volumes_fts(query)
                if local_count > len(cached_data.get("results", [])):
                    cached_data = None  # invalidate

        # Superset check: if cache miss, look for a broader query whose complete
        # result set is a superset of this query.  E.g. if "batman" is fully
        # cached (all upstream pages), serve "absolute batman" from it.
        if cached_data is None and cache_mode != "no-cache":
            superset = db.cache_find_superset_search(norm_query, resources)
            if superset is not None:
                # Store this derived entry so subsequent requests hit directly
                db.cache_put_search(
                    params_hash,
                    superset["results"],
                    superset["total"],
                    query=norm_query,
                    resources=resources,
                )
                cached_data = superset

    # --- 2. only-if-cached: local FTS fallback, no upstream ---
    if cache_mode == "only-if-cached" and cached_data is None:
        _FTS_RESOURCE_MAP = [
            ("volume", db.search_volumes, _VOLUME_LIST_FIELDS),
            ("issue", db.search_issues_by_name, _ISSUE_LIST_FIELDS),
            ("publisher", db.search_publishers_by_name, _PUBLISHER_LIST_FIELDS),
        ]
        for resource_type, search_fn, list_fields in _FTS_RESOURCE_MAP:
            if resource_type not in resources:
                continue
            raw, total = search_fn(query, limit=limit, offset=effective_offset)
            if not raw:
                continue
            raw.sort(key=lambda r: _search_relevance_key(
                r.get("name"), query, r.get("date_last_updated")
            ))
            search_fields = list_fields | {"resource_type"}
            for r in raw:
                r["resource_type"] = resource_type
            shaped = [_apply_list_defaults(r, search_fields, field_list) for r in raw]
            _record(request, endpoint="search", source="cache", start=t0)
            return _cv_envelope(shaped, total=total, limit=limit, offset=effective_offset)
        _record(request, endpoint="search", source="miss", start=t0)
        return _cv_envelope([], total=0, limit=limit, offset=effective_offset)

    # --- 3. Fetch from upstream if still no cached data ---
    if cached_data is None:
        if cache_mode == "only-if-cached":
            _record(request, endpoint="search", source="miss", start=t0)
            return _cv_envelope([], total=0, limit=limit, offset=effective_offset)

        client = _get_client(request)
        all_results: list[dict[str, Any]] = []
        cv_total = 0
        fetch_offset = 0
        max_pages = settings.search_max_pages

        for _ in range(max_pages):
            page_data = await _guarded_get(client, client.search(query, resources, limit=100, offset=fetch_offset))
            if page_data is RATE_LIMITED:
                _record(request, endpoint="search", source="miss", start=t0)
                return _cv_rate_limit_error()
            if not page_data:
                break
            page_results = page_data.get("results", [])
            if not page_results:
                break
            if cv_total == 0:
                cv_total = page_data.get("number_of_total_results", 0)
            all_results.extend(page_results)
            if len(all_results) >= cv_total or len(page_results) < 100:
                break
            fetch_offset += 100

        if not all_results:
            _record(request, endpoint="search", source="miss", start=t0)
            return _cv_envelope([], total=0, limit=limit, offset=effective_offset)

        # Upsert into local DB so backfill jobs can keep these fresh
        for item in all_results:
            if "volume" in resources and item.get("id"):
                try:
                    db.upsert_volume(item)
                except Exception:  # noqa: BLE001
                    pass

        db.cache_put_search(
            params_hash, all_results, cv_total, query=norm_query, resources=resources
        )
        cached_data = {"results": all_results, "total": cv_total}
        _record(request, endpoint="search", source="upstream", start=t0)
    else:
        _record(request, endpoint="search", source="cache", start=t0)

    # --- 4. Build response from cached results + fresh local DB data ---
    raw_results: list[dict[str, Any]] = cached_data.get("results", [])
    cv_total = cached_data.get("total", len(raw_results))

    # For each cached result, prefer fresh cv_volume data (batch fetch to avoid N+1)
    enriched: list[dict[str, Any]] = []
    if "volume" in resources:
        ids = [item["id"] for item in raw_results if item.get("id")]
        local_map = {v["id"]: v for v in db.get_volumes_by_ids(ids)[0]} if ids else {}
        for item in raw_results:
            item_id = item.get("id")
            enriched.append(local_map.get(item_id, item) if item_id else item)
    else:
        enriched = list(raw_results)

    # Add resource_type
    resource_type = resources.split(",")[0].strip()  # primary resource type
    for r in enriched:
        r["resource_type"] = resource_type

    # Relevance sort across full result set
    enriched.sort(key=lambda r: _search_relevance_key(
        r.get("name"), query, r.get("date_last_updated")
    ))

    # Slice by requested offset/limit
    page_slice = enriched[effective_offset: effective_offset + limit]

    # Apply field_list (never forwarded upstream, always applied here)
    list_fields = _VOLUME_LIST_FIELDS | {"resource_type"}
    shaped = [_apply_list_defaults(r, list_fields, field_list) for r in page_slice]

    return _cv_envelope(shaped, total=cv_total, limit=limit, offset=effective_offset)


# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------


@router.get("/volume/{volume_id}/")
async def get_volume(
    request: Request,
    volume_id: str,
    api_key: str = Query(default="", alias="api_key"),
    format: str = Query(default="json", alias="format"),
    field_list: str = Query(default="", alias="field_list"),
) -> dict[str, Any]:
    """Get volume detail by ID (accepts ``4050-123`` or ``123``)."""
    t0 = time.monotonic()
    vid = _parse_cv_id(volume_id)
    db = _get_db(request)
    cache_mode = request.headers.get("X-CV-Cache", "").lower()
    vol = None if cache_mode == "no-cache" else db.get_volume(vid)

    if vol:
        if not field_list or "issues" in field_list:
            issues = db.get_volume_issues(vid)
            vol["issues"] = [
                {"id": i["id"], "issue_number": i["issue_number"], "name": i["name"]}
                for i in issues
            ]
        _touch_if_evicting(db, "cv_volume", vid)
        vol = _apply_field_list(vol, field_list)
        _record(request, endpoint="volume", source="cache", start=t0)
        return _cv_envelope(vol)

    if cache_mode == "only-if-cached":
        _record(request, endpoint="volume", source="miss", start=t0)
        return _cv_error("Volume not in cache", status_code=101)

    client = _get_client(request)
    data = await _guarded_get(client, client.get_volume(vid))
    if data is RATE_LIMITED:
        _record(request, endpoint="volume", source="miss", start=t0)
        return _cv_rate_limit_error()
    if data and data.get("results"):
        try:
            db.upsert_volume(data["results"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to upsert volume %s: %s", data["results"].get("id"), exc)
        if field_list:
            data = {**data, "results": _apply_field_list(data["results"], field_list)}
        _record(request, endpoint="volume", source="upstream", start=t0)
        return data

    _record(request, endpoint="volume", source="miss", start=t0)
    return _cv_error("Volume not found", status_code=101)


@router.get("/volumes/")
async def list_volumes(
    request: Request,
    filter: str = Query(default="", alias="filter"),
    limit: int = Query(default=100, alias="limit"),
    offset: int = Query(default=0, alias="offset"),
    page: int = Query(default=0, alias="page"),
    api_key: str = Query(default="", alias="api_key"),
    format: str = Query(default="json", alias="format"),
    field_list: str = Query(default="", alias="field_list"),
    sort: str = Query(default="", alias="sort"),
) -> dict[str, Any]:
    """CV-compatible volumes list with filter support."""
    t0 = time.monotonic()
    effective_offset = _effective_offset(offset, page, limit)
    db = _get_db(request)
    cache_mode = _get_cache_mode(request)

    filters = _parse_cv_filter(filter)
    name = filters.get("name", "")
    id_filter = filters.get("id", "")

    # Handle id:ID1|ID2|... filter (bulk volume lookup from cache)
    if id_filter and cache_mode != "no-cache":
        ids = _parse_id_list(id_filter)
        if ids:
            results, total = db.get_volumes_by_ids(ids)
            if results:
                if sort:
                    results = _apply_sort(results, sort)
                results = [_apply_list_defaults(r, _VOLUME_LIST_FIELDS, field_list) for r in results]
                _record(request, endpoint="volumes", source="cache", start=t0)
                return _cv_envelope(results, total=total, limit=limit, offset=effective_offset)

    if name and cache_mode != "no-cache":
        results, total = db.search_volumes(name, limit=limit, offset=effective_offset)
        if results:
            client = _get_client(request)
            refreshed = await _refresh_volumes_if_stale(client, db, filter, total)
            if refreshed:
                results, total = db.search_volumes(name, limit=limit, offset=effective_offset)
            if sort:
                results = _apply_sort(results, sort)
            results = [_apply_list_defaults(r, _VOLUME_LIST_FIELDS, field_list) for r in results]
            _record(request, endpoint="volumes", source="cache", start=t0)
            return _cv_envelope(results, total=total, limit=limit, offset=effective_offset)

    if cache_mode == "only-if-cached":
        _record(request, endpoint="volumes", source="miss", start=t0)
        return _cv_envelope([], total=0, limit=limit, offset=effective_offset)

    upstream_params: dict[str, Any] = {"filter": filter, "limit": limit, "offset": effective_offset}
    if sort:
        upstream_params["sort"] = sort
    client = _get_client(request)
    data = await _guarded_get(client, client.get("volumes/", upstream_params))
    if data is RATE_LIMITED:
        _record(request, endpoint="volumes", source="miss", start=t0)
        return _cv_rate_limit_error()
    if data:
        if isinstance(data.get("results"), list):
            for vol in data["results"]:
                try:
                    db.upsert_volume(vol)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to upsert volume %s: %s", vol.get("id"), exc)
            if field_list:
                data = {**data, "results": [_apply_field_list(r, field_list) for r in data["results"]]}
        _record(request, endpoint="volumes", source="upstream", start=t0)
        return data

    _record(request, endpoint="volumes", source="miss", start=t0)
    return _cv_envelope([], total=0, limit=limit, offset=effective_offset)


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


@router.get("/issue/{issue_id}/")
async def get_issue(
    request: Request,
    issue_id: str,
    api_key: str = Query(default="", alias="api_key"),
    format: str = Query(default="json", alias="format"),
    field_list: str = Query(default="", alias="field_list"),
) -> dict[str, Any]:
    """Get issue detail by ID (accepts ``4000-123`` or ``123``)."""
    t0 = time.monotonic()
    iid = _parse_cv_id(issue_id)
    db = _get_db(request)
    cache_mode = request.headers.get("X-CV-Cache", "").lower()
    issue = None if cache_mode == "no-cache" else db.get_issue(iid)

    if issue:
        _date = issue.get("store_date") or issue.get("cover_date")
        if is_past_cutoff(_date, get_settings().evict_older_than_years):
            _touch_if_evicting(db, "cv_issue", iid)
        issue = _apply_field_list(issue, field_list)
        _record(request, endpoint="issue", source="cache", start=t0)
        return _cv_envelope(issue)

    if cache_mode == "only-if-cached":
        _record(request, endpoint="issue", source="miss", start=t0)
        return _cv_error("Issue not in cache", status_code=101)

    client = _get_client(request)
    data = await _guarded_get(client, client.get_issue(iid))
    if data is RATE_LIMITED:
        _record(request, endpoint="issue", source="miss", start=t0)
        return _cv_rate_limit_error()
    if data and data.get("results"):
        try:
            db.upsert_issue(data["results"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to upsert issue %s: %s", data["results"].get("id"), exc)
        if field_list:
            data = {**data, "results": _apply_field_list(data["results"], field_list)}
        _record(request, endpoint="issue", source="upstream", start=t0)
        return data

    _record(request, endpoint="issue", source="miss", start=t0)
    return _cv_error("Issue not found", status_code=101)


@router.get("/issues/")
async def list_issues(
    request: Request,
    filter: str = Query(default="", alias="filter"),
    limit: int = Query(default=100, alias="limit"),
    offset: int = Query(default=0, alias="offset"),
    page: int = Query(default=0, alias="page"),
    api_key: str = Query(default="", alias="api_key"),
    format: str = Query(default="json", alias="format"),
    field_list: str = Query(default="", alias="field_list"),
    sort: str = Query(default="", alias="sort"),
) -> dict[str, Any]:
    """CV-compatible issues list with filter support."""
    t0 = time.monotonic()
    effective_offset = _effective_offset(offset, page, limit)
    db = _get_db(request)
    cache_mode = _get_cache_mode(request)

    filters = _parse_cv_filter(filter)
    volume_filter = filters.get("volume", "")
    issue_number = filters.get("issue_number")
    store_date_range = _parse_date_range(filters.get("store_date", ""))

    vol_ids = _parse_id_list(volume_filter) if volume_filter else None
    store_date_start = store_date_range[0] if store_date_range else None
    store_date_end = store_date_range[1] if store_date_range else None

    if cache_mode != "no-cache":
        results, total = db.search_issues(
            volume_ids=vol_ids,
            issue_number=issue_number,
            store_date_start=store_date_start,
            store_date_end=store_date_end,
            limit=limit,
            offset=effective_offset,
        )

        if results:
            if sort:
                results = _apply_sort(results, sort)
            results = [_apply_list_defaults(r, _ISSUE_LIST_FIELDS, field_list) for r in results]
            _record(request, endpoint="issues", source="cache", start=t0)
            return _cv_envelope(results, total=total, limit=limit, offset=effective_offset)

    if cache_mode == "only-if-cached":
        _record(request, endpoint="issues", source="miss", start=t0)
        return _cv_envelope([], total=0, limit=limit, offset=effective_offset)

    upstream_params2: dict[str, Any] = {"filter": filter, "limit": limit, "offset": effective_offset}
    if sort:
        upstream_params2["sort"] = sort
    client = _get_client(request)
    data = await _guarded_get(client, client.get("issues/", upstream_params2))
    if data is RATE_LIMITED:
        _record(request, endpoint="issues", source="miss", start=t0)
        return _cv_rate_limit_error()
    if data:
        if isinstance(data.get("results"), list):
            for issue in data["results"]:
                try:
                    db.upsert_issue(issue)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to upsert issue %s: %s", issue.get("id"), exc)
            if field_list:
                data = {**data, "results": [_apply_field_list(r, field_list) for r in data["results"]]}
        _record(request, endpoint="issues", source="upstream", start=t0)
        return data

    _record(request, endpoint="issues", source="miss", start=t0)
    return _cv_envelope([], total=0, limit=limit, offset=effective_offset)


# ---------------------------------------------------------------------------
# Publishers
# ---------------------------------------------------------------------------


@router.get("/publisher/{publisher_id}/")
async def get_publisher(
    request: Request,
    publisher_id: str,
    api_key: str = Query(default="", alias="api_key"),
    format: str = Query(default="json", alias="format"),
    field_list: str = Query(default="", alias="field_list"),
) -> dict[str, Any]:
    """Get publisher by ID (accepts ``4010-1`` or ``1``)."""
    t0 = time.monotonic()
    pid = _parse_cv_id(publisher_id)
    db = _get_db(request)
    cache_mode = request.headers.get("X-CV-Cache", "").lower()
    pub = None if cache_mode == "no-cache" else db.get_publisher(pid)

    if pub:
        pub = _apply_field_list(pub, field_list)
        _record(request, endpoint="publisher", source="cache", start=t0)
        return _cv_envelope(pub)

    if cache_mode == "only-if-cached":
        _record(request, endpoint="publisher", source="miss", start=t0)
        return _cv_error("Publisher not in cache", status_code=101)

    client = _get_client(request)
    data = await _guarded_get(client, client.get_publisher(pid))
    if data is RATE_LIMITED:
        _record(request, endpoint="publisher", source="miss", start=t0)
        return _cv_rate_limit_error()
    if data and data.get("results"):
        try:
            db.upsert_publisher(data["results"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to upsert publisher %s: %s", data["results"].get("id"), exc)
        if field_list:
            data = {**data, "results": _apply_field_list(data["results"], field_list)}
        _record(request, endpoint="publisher", source="upstream", start=t0)
        return data

    _record(request, endpoint="publisher", source="miss", start=t0)
    return _cv_error("Publisher not found", status_code=101)


@router.get("/publishers/")
async def list_publishers(
    request: Request,
    filter: str = Query(default="", alias="filter"),
    limit: int = Query(default=100, alias="limit"),
    offset: int = Query(default=0, alias="offset"),
    page: int = Query(default=0, alias="page"),
    api_key: str = Query(default="", alias="api_key"),
    format: str = Query(default="json", alias="format"),
    field_list: str = Query(default="", alias="field_list"),
    sort: str = Query(default="", alias="sort"),
) -> dict[str, Any]:
    """CV-compatible publishers list."""
    t0 = time.monotonic()
    effective_offset = _effective_offset(offset, page, limit)
    db = _get_db(request)
    cache_mode = _get_cache_mode(request)

    filters = _parse_cv_filter(filter)
    name = filters.get("name", "")

    if cache_mode != "no-cache":
        results, total = db.list_publishers(name=name, limit=limit, offset=effective_offset)
        if results:
            results = _apply_sort(results, sort)
            results = [_apply_list_defaults(r, _PUBLISHER_LIST_FIELDS, field_list) for r in results]
            _record(request, endpoint="publishers", source="cache", start=t0)
            return _cv_envelope(results, total=total, limit=limit, offset=effective_offset)

    if cache_mode == "only-if-cached":
        _record(request, endpoint="publishers", source="miss", start=t0)
        return _cv_envelope([], total=0, limit=limit, offset=effective_offset)

    upstream_params: dict[str, Any] = {"filter": filter, "limit": limit, "offset": effective_offset}
    if sort:
        upstream_params["sort"] = sort
    client = _get_client(request)
    data = await _guarded_get(client, client.get("publishers/", upstream_params))
    if data is RATE_LIMITED:
        _record(request, endpoint="publishers", source="miss", start=t0)
        return _cv_rate_limit_error()
    if data:
        if isinstance(data.get("results"), list):
            for pub in data["results"]:
                try:
                    db.upsert_publisher(pub)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to upsert publisher %s: %s", pub.get("id"), exc)
            if field_list:
                data = {**data, "results": [_apply_field_list(r, field_list) for r in data["results"]]}
        _record(request, endpoint="publishers", source="upstream", start=t0)
        return data

    _record(request, endpoint="publishers", source="miss", start=t0)
    return _cv_envelope([], total=0, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Image proxy
# ---------------------------------------------------------------------------


@router.get("/image/{path:path}")
async def proxy_image(
    request: Request,
    path: str,
) -> Response:
    """Serve cached images or fetch on-demand from CV CDN.

    The original CV image URL is reconstructed from the path.
    """
    t0 = time.monotonic()
    # Reconstruct the original CV image URL
    original_url = f"https://comicvine.gamespot.com/a/uploads/{path}"

    cache = _get_cache(request)
    client = _get_client(request)
    result = await cache.get_or_fetch(original_url, client)

    if result is None:
        _record(request, endpoint="image", source="miss", start=t0)
        return Response(status_code=404, content=b"Image not found")

    # ImageCache.get_or_fetch doesn't tell us if it was a cache hit or fetch,
    # but we can infer: if latency is very low it was cached. For simplicity
    # we record all image proxy calls as "cache" since they go through the
    # image cache layer regardless.
    _record(request, endpoint="image", source="cache", start=t0)
    return Response(
        content=result.data,
        media_type=result.content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------------
# CV filter parser
# ---------------------------------------------------------------------------


def _parse_cv_filter(filter_str: str) -> dict[str, str]:
    """Parse CV filter format ``key:value,key2:value2`` into a dict."""
    if not filter_str:
        return {}
    result: dict[str, str] = {}
    for part in re.split(r",(?=[a-z_]+:)", filter_str):
        if ":" in part:
            key, _, value = part.partition(":")
            result[key.strip()] = value.strip()
    return result


def _parse_date_range(value: str) -> tuple[str, str] | None:
    """Parse a CV date range value like ``2026-03-01|2026-03-07``.

    Returns ``(start, end)``, or ``(value, value)`` for a bare date,
    or ``None`` for empty/malformed input.
    """
    if not value:
        return None
    if "|" in value:
        start, _, end = value.partition("|")
        start, end = start.strip(), end.strip()
        if start and end:
            return start, end
        return None
    v = value.strip()
    return (v, v) if v else None


def _parse_id_list(value: str) -> list[int] | None:
    """Parse a pipe-separated list of integer IDs like ``1536|1537|898``.

    Returns the parsed list, or None if the value is empty or any token is
    non-integer.  Inputs are expected to be machine-generated, so a strict
    all-or-nothing parse is intentional.
    """
    if not value:
        return None
    try:
        ids = [int(part.strip()) for part in value.split("|") if part.strip()]
        return ids if ids else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Generic catch-all — single resource
# ---------------------------------------------------------------------------


@router.get("/{resource}/{resource_id}/")
async def generic_single(
    request: Request,
    resource: str,
    resource_id: str,
    api_key: str = Query(default="", alias="api_key"),
    format: str = Query(default="json", alias="format"),
    field_list: str = Query(default="", alias="field_list"),
) -> dict[str, Any]:
    """Generic single-resource handler for any CV API resource type.

    Handles characters (4005-X), teams (4060-X), story_arcs (4045-X),
    people (4040-X), locations (4020-X), concepts (4015-X), origins (4030-X),
    powers (4035-X), movies (4025-X), objects (4055-X), series (4075-X),
    episodes (4070-X), videos, and any future CV resource types.

    Checks cv_response_cache first. On miss, fetches from upstream and caches.
    """
    t0 = time.monotonic()
    rid = _parse_cv_id(resource_id)
    db = _get_db(request)
    cache_mode = _get_cache_mode(request)

    cached = None if cache_mode == "no-cache" else db.cache_get_single(resource, rid)
    if cached is not None:
        result = _apply_field_list(cached, field_list)
        _record(request, endpoint=resource, source="cache", start=t0)
        return _cv_envelope(result)

    if cache_mode == "only-if-cached":
        _record(request, endpoint=resource, source="miss", start=t0)
        return _cv_error(f"{resource.capitalize()} not in cache", status_code=101)

    client = _get_client(request)
    data = await _guarded_get(client, client.get(f"{resource}/{resource_id}/", None))
    if data is RATE_LIMITED:
        _record(request, endpoint=resource, source="miss", start=t0)
        return _cv_rate_limit_error()

    if data and data.get("results"):
        result = data["results"]
        # Cache the full result (field_list not forwarded so cache is reusable)
        try:
            db.cache_put_single(resource, rid, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to cache %s %s: %s", resource, rid, exc)
        if field_list:
            data = {**data, "results": _apply_field_list(result, field_list)}
        _record(request, endpoint=resource, source="upstream", start=t0)
        return data

    _record(request, endpoint=resource, source="miss", start=t0)
    return _cv_error("Object Not Found", status_code=101)


# ---------------------------------------------------------------------------
# Generic catch-all — list resource
# ---------------------------------------------------------------------------


@router.get("/{resources}/")
async def generic_list(
    request: Request,
    resources: str,
    filter: str = Query(default="", alias="filter"),
    limit: int = Query(default=100, alias="limit"),
    offset: int = Query(default=0, alias="offset"),
    api_key: str = Query(default="", alias="api_key"),
    format: str = Query(default="json", alias="format"),
    field_list: str = Query(default="", alias="field_list"),
    sort: str = Query(default="", alias="sort"),
) -> dict[str, Any]:
    """Generic list handler for any CV API resource type not explicitly wired up.

    Cache key excludes limit, offset, and field_list so that:
    - The same filter with different limits serves subsets from a single cache entry.
    - Different field_list values can all be served from the same cached full results.

    On first miss: fetches from upstream (without field_list to maximise cache reuse),
    stores the full result array, then applies field_list + limit/offset in Python.
    """
    t0 = time.monotonic()
    db = _get_db(request)
    cache_mode = _get_cache_mode(request)

    # Cache key covers filter + sort (not limit/offset/field_list)
    ph = _params_hash({"filter": filter, "sort": sort})

    cached_results = None if cache_mode == "no-cache" else db.cache_get_list(resources, ph)
    if cached_results is not None:
        if sort:
            cached_results = _apply_sort(cached_results, sort)  # Sort full list first
        page = cached_results[offset: offset + limit]           # Then slice
        if field_list:
            page = [_apply_field_list(r, field_list) for r in page]
        _record(request, endpoint=resources, source="cache", start=t0)
        return _cv_envelope(page, total=len(cached_results), limit=limit, offset=offset)

    if cache_mode == "only-if-cached":
        _record(request, endpoint=resources, source="miss", start=t0)
        return _cv_envelope([], total=0, limit=limit, offset=offset)

    client = _get_client(request)
    upstream_params: dict[str, Any] = {"filter": filter, "limit": limit, "offset": offset}
    if sort:
        upstream_params["sort"] = sort
    data = await _guarded_get(client, client.get(f"{resources}/", upstream_params))
    if data is RATE_LIMITED:
        _record(request, endpoint=resources, source="miss", start=t0)
        return _cv_rate_limit_error()

    if data and isinstance(data.get("results"), list):
        results: list[dict[str, Any]] = data["results"]
        try:
            db.cache_put_list(resources, ph, results)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to cache list %s: %s", resources, exc)

        if field_list:
            data = {**data, "results": [_apply_field_list(r, field_list) for r in results]}
        _record(request, endpoint=resources, source="upstream", start=t0)
        return data

    _record(request, endpoint=resources, source="miss", start=t0)
    return _cv_envelope([], total=0, limit=limit, offset=offset)
