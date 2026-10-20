"""Worker for the pending-fetch queue.

Requests that miss the cache while ComicVine can't be asked (cooldown after a
refusal, or the endpoint's hourly window is full) are queued by the routes.
Fetching them later warms the cache, so the client's retry becomes a hit.
Queued requests are served before any backfill on the same endpoint.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from cvproxy.cv_client import NOT_FOUND, RATE_LIMITED, CVClient, CVResponse
from cvproxy.db import Database
from cvproxy.routes.api import (
    _fetch_search_pages,
    _normalize_search_query,
    _refresh_volume_issues,
    _search_cache_key,
    _store_search_results,
)

logger = logging.getLogger(__name__)

#: Failed fetches (errors, not refusals) are retried this many times, then dropped.
MAX_ATTEMPTS = 5
#: Calls per endpoint per hour the worker leaves for live requests, so draining
#: a backlog can't fill the window and turn new requests into more 420s.
LIVE_RESERVE = 30
_BATCH = 50

Outcome = Literal["done", "rate_limited", "failed"]
_Fetcher = Callable[[CVClient, Database, dict[str, Any]], Awaitable[Outcome]]


def _detail_outcome(data: CVResponse, store: Callable[[dict[str, Any]], None]) -> Outcome:
    if data is RATE_LIMITED:
        return "rate_limited"
    if data is NOT_FOUND:
        return "done"  # CV says it doesn't exist; nothing to wait for
    if data and data.get("results"):
        store(data["results"])
        return "done"
    return "failed"


async def _fetch_issue(client: CVClient, db: Database, payload: dict[str, Any]) -> Outcome:
    return _detail_outcome(await client.get_issue(payload["id"]), db.upsert_issue)


async def _fetch_volume(client: CVClient, db: Database, payload: dict[str, Any]) -> Outcome:
    return _detail_outcome(await client.get_volume(payload["id"]), db.upsert_volume)


async def _fetch_publisher(client: CVClient, db: Database, payload: dict[str, Any]) -> Outcome:
    return _detail_outcome(await client.get_publisher(payload["id"]), db.upsert_publisher)


async def _fetch_volume_issues(
    client: CVClient, db: Database, payload: dict[str, Any]
) -> Outcome:
    result = await _refresh_volume_issues(client, db, payload["volume_id"])
    # "skipped" (volume too large, or upstream error) has no better retry
    return "rate_limited" if result == "rate_limited" else "done"


async def _fetch_issues_filter(
    client: CVClient, db: Database, payload: dict[str, Any]
) -> Outcome:
    data = await client.get("issues/", {"filter": payload["filter"], "limit": 100, "offset": 0})
    if data is RATE_LIMITED:
        return "rate_limited"
    if data is None:
        return "failed"
    results = data.get("results") or [] if data else []
    if results:
        db.insert_new_issues(results)
    elif payload.get("negative_key"):
        db.negative_cache_put("issues", payload["negative_key"])
    return "done"


async def _fetch_search(client: CVClient, db: Database, payload: dict[str, Any]) -> Outcome:
    # Page 1 only: nobody is waiting on a queued search, it just warms the cache
    query, resources = payload["query"], payload["resources"]
    results, total, outcome = await _fetch_search_pages(client, query, resources, 1, 1)
    if not results and outcome != "ok":
        return outcome
    key = _search_cache_key(query, resources)
    if results:
        _store_search_results(
            db, key, results, total, query=_normalize_search_query(query), resources=resources
        )
    else:
        db.negative_cache_put("search", key)
    return "done"


_FETCHERS: dict[str, _Fetcher] = {
    "issue": _fetch_issue,
    "volume": _fetch_volume,
    "publisher": _fetch_publisher,
    "volume_issues": _fetch_volume_issues,
    "issues_filter": _fetch_issues_filter,
    "search": _fetch_search,
}


async def process_pending(
    client: CVClient, db: Database, limit: int = _BATCH
) -> dict[str, int]:
    """Fetch queued requests, oldest first, while their endpoint has budget."""
    fetched = skipped = failed = 0
    for item in db.pending_list(limit=limit):
        kind, key = item["kind"], item["key"]
        endpoint = item["endpoint"]
        if (
            client.retry_after(endpoint) > 0
            or client.hourly_calls(endpoint) >= client.hourly_limit - LIVE_RESERVE
        ):
            skipped += 1
            continue
        outcome = await _FETCHERS[kind](client, db, item["payload"])
        if outcome == "rate_limited":
            skipped += 1  # stays queued; a refusal isn't the item's fault
        elif outcome == "failed":
            db.pending_failed(kind, key, max_attempts=MAX_ATTEMPTS)
            failed += 1
        else:
            db.pending_done(kind, key)
            fetched += 1
    if fetched or failed:
        logger.info(
            "Pending fetch: %d fetched, %d waiting, %d failed", fetched, skipped, failed
        )
    return {"fetched": fetched, "skipped": skipped, "failed": failed}
