"""Background scheduler for DB sync, data backfill, and image cache cleanup."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from cvproxy.cv_client import NOT_FOUND as _CV_NOT_FOUND

if TYPE_CHECKING:
    from cvproxy.config import Settings
    from cvproxy.cv_client import CVClient
    from cvproxy.db import Database
    from cvproxy.image_cache import ImageCache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler job history log
# ---------------------------------------------------------------------------

_scheduler_log: deque[dict[str, Any]] = deque(maxlen=100)


def get_scheduler_log() -> list[dict[str, Any]]:
    """Return a snapshot of recent scheduled job runs, newest first."""
    return list(_scheduler_log)


def _make_tracked(name: str, fn: Any) -> Any:
    """Wrap a scheduler job function to record each run in the history log."""
    async def _wrapper(*args: Any) -> None:
        entry: dict[str, Any] = {
            "name": name,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "status": "running",
            "result": None,
        }
        _scheduler_log.appendleft(entry)
        try:
            if asyncio.iscoroutinefunction(fn):
                result = await fn(*args)
            else:
                result = fn(*args)
            entry["status"] = "done"
            entry["result"] = result
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scheduled job %s failed", name)
            entry["status"] = "error"
            entry["result"] = {"error": str(exc)}
        finally:
            entry["finished_at"] = datetime.now(timezone.utc).isoformat()
    return _wrapper


# ---------------------------------------------------------------------------
# Backfill rate pacing
# ---------------------------------------------------------------------------

#: Maximum upstream calls the backfill jobs may consume per hour.  The
#: remainder of the hourly budget is reserved for live client requests.
_BACKFILL_HOURLY_BUDGET = 149

#: Hard cap on the time to sleep between individual backfill calls (seconds).
_BACKFILL_MAX_SLEEP = 120.0
#: Minimum sleep so the loop never hammers the API even if the budget maths
#: produce a tiny value.
_BACKFILL_MIN_SLEEP = 2.0


def _backfill_call_interval(client: "CVClient") -> float:
    """Compute seconds to sleep after each backfill upstream call.

    Divides the remaining seconds in the current clock-hour by the remaining
    backfill budget.  This spreads the allowed calls evenly across the hour so
    that live client requests always have headroom between background fetches.

    Example: 45 min left, 100 budget remaining → sleep 27 s per call.
    """
    now_utc = datetime.now(timezone.utc)
    secs_left_in_hour = max(
        1.0,
        3600.0 - (now_utc.minute * 60 + now_utc.second + now_utc.microsecond / 1_000_000),
    )
    used = client.total_hourly_calls()
    remaining_budget = max(1, _BACKFILL_HOURLY_BUDGET - used)
    interval = secs_left_in_hour / remaining_budget
    return max(_BACKFILL_MIN_SLEEP, min(interval, _BACKFILL_MAX_SLEEP))

# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------


def create_scheduler(
    settings: Settings,
    image_cache: ImageCache,
    db: "Database",
    client: "CVClient",
) -> AsyncIOScheduler:
    """Create and configure the background scheduler.

    Jobs:
      - Image cache cleanup: daily
      - Releases prefetch: daily (store_date -2w / +1w)
      - Volume backfill: hourly (100 calendar + 50 old/batch, fills new fields)
      - Issue backfill: hourly (150/batch, fills new fields)
      - Publisher backfill: every 6h (50/batch, fills new fields)
      - Incremental issue sync: daily (date_last_updated last 48h)
    """
    scheduler = AsyncIOScheduler()
    hour = settings.sync_cron_hour  # default 3 AM UTC

    # Image cache cleanup — runs daily
    scheduler.add_job(
        _make_tracked("Image cache cleanup", _cleanup_images),
        "cron",
        hour=(hour + 2) % 24,
        minute=0,
        args=[image_cache],
        id="image_cache_cleanup",
        name="Image cache cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Releases prefetch — daily at sync hour
    scheduler.add_job(
        _make_tracked("Releases prefetch", _prefetch_releases),
        "cron",
        hour=hour,
        minute=0,
        args=[client, db],
        id="releases_prefetch",
        name="Releases prefetch",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Volume backfill — hourly (fills new fields on stale rows)
    scheduler.add_job(
        _make_tracked("Volume backfill", _backfill_volumes),
        "cron",
        minute=30,
        args=[client, db],
        id="volume_backfill",
        name="Volume backfill",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Issue backfill — hourly (fills new fields on stale rows)
    scheduler.add_job(
        _make_tracked("Issue backfill", _backfill_issues),
        "cron",
        minute=45,
        args=[client, db],
        id="issue_backfill",
        name="Issue backfill",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Publisher backfill — every 6 hours (small table, fewer records)
    scheduler.add_job(
        _make_tracked("Publisher backfill", _backfill_publishers),
        "cron",
        hour="*/6",
        minute=15,
        args=[client, db],
        id="publisher_backfill",
        name="Publisher backfill",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Incremental issue sync — daily, 1 hour after releases prefetch
    scheduler.add_job(
        _make_tracked("Incremental issue sync", _sync_recent_issues),
        "cron",
        hour=(hour + 1) % 24,
        minute=0,
        args=[client, db, 48],
        id="incremental_issue_sync",
        name="Incremental issue sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Stale entity eviction — daily, 3 hours after sync hour
    scheduler.add_job(
        _make_tracked("Stale data eviction", _evict_stale_data),
        "cron",
        hour=(hour + 3) % 24,
        minute=0,
        args=[db, settings],
        id="evict_stale_data",
        name="Stale data eviction",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Search-cache volume backfill — every 2 hours
    scheduler.add_job(
        _make_tracked("Search cache backfill", _backfill_search_cache_volumes),
        "cron",
        minute=50,
        hour="*/2",
        args=[client, db],
        id="search_cache_volume_backfill",
        name="Search cache volume backfill",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    return scheduler


# ---------------------------------------------------------------------------
# Image cache cleanup
# ---------------------------------------------------------------------------


def _cleanup_images(image_cache: ImageCache) -> dict[str, Any]:
    """Remove expired image cache entries."""
    try:
        removed = image_cache.cleanup_expired()
        if removed:
            logger.info("Scheduled cleanup: removed %d expired images", removed)
        else:
            logger.debug("Scheduled cleanup: no expired images")
        return {"removed": removed}
    except Exception:
        logger.exception("Image cache cleanup failed")
        return {"removed": 0}


# ---------------------------------------------------------------------------
# Releases prefetch  (issues endpoint, store_date -2w / +1w)
# ---------------------------------------------------------------------------


async def _prefetch_releases(client: "CVClient", db: "Database") -> None:
    """Fetch upcoming/recent releases from CV and upsert into local DB.

    Window: 2 weeks in the past through 1 week in the future.
    Comics data on CV typically lags, so looking backward catches
    recently-added entries.
    """
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(weeks=2)).isoformat()
    end = (today + timedelta(weeks=1)).isoformat()

    logger.info("Releases prefetch: fetching store_date %s to %s", start, end)

    total_upserted = 0
    offset = 0
    limit = 100

    while True:
        data = await client.get(
            "issues/",
            {
                "filter": f"store_date:{start}|{end}",
                "sort": "store_date:asc",
                "limit": limit,
                "offset": offset,
            },
        )
        if not data:
            logger.warning("Releases prefetch: upstream request failed at offset %d", offset)
            break

        results = data.get("results") or []
        for issue in results:
            db.upsert_issue(issue)
        total_upserted += len(results)

        total_available = data.get("number_of_total_results", 0)
        offset += limit

        if offset >= total_available or not results:
            break

    logger.info("Releases prefetch: upserted %d issues", total_upserted)
    return {"issues_upserted": total_upserted}


# ---------------------------------------------------------------------------
# Entity backfill  (re-fetch rows cached before new columns were added)
# ---------------------------------------------------------------------------

# ``date_last_updated`` is the sentinel column — CV always returns it, so a
# NULL value means the row was cached by an older version of the code that
# didn't store these fields yet.

_VOLUME_CALENDAR_BATCH = 100  # new/updated volumes (calendar-relevant)
_VOLUME_OLD_BATCH = 50  # older stale rows
_ISSUE_BATCH = 150
_PUBLISHER_BATCH = 50  # publishers table is much smaller


async def _backfill_volumes(client: "CVClient", db: "Database") -> None:
    """Re-fetch volumes that are missing the new detail fields.

    Two-tier batching:
      - Up to 100 calendar-relevant volumes (tied to issues with a
        store_date) — these are new/updated volumes that matter most.
      - Up to 50 older stale volumes — backfills the long tail.

    All upserts are deferred to a single commit at the end so the asyncio
    event loop is never blocked by repeated fsync calls during the run.
    """
    # Tier 1: calendar-relevant volumes (new/updated)
    cur = db.conn.execute(
        """
        SELECT DISTINCT i.volume_id FROM cv_issue i
        JOIN cv_volume v ON v.id = i.volume_id
        WHERE i.store_date IS NOT NULL
          AND v.date_last_updated IS NULL
          AND (v.backfill_missing IS NULL OR v.backfill_missing = 0)
        ORDER BY i.store_date DESC
        LIMIT ?
        """,
        [_VOLUME_CALENDAR_BATCH],
    )
    calendar_ids = [row[0] for row in cur.fetchall()]

    # Tier 2: older stale volumes (excluding any already in tier 1)
    seen = set(calendar_ids)
    cur2 = db.conn.execute(
        """
        SELECT id FROM cv_volume
        WHERE date_last_updated IS NULL
          AND (backfill_missing IS NULL OR backfill_missing = 0)
        ORDER BY id DESC
        LIMIT ?
        """,
        [_VOLUME_OLD_BATCH + len(seen)],
    )
    old_ids = [row[0] for row in cur2.fetchall() if row[0] not in seen][
        :_VOLUME_OLD_BATCH
    ]

    volume_ids = calendar_ids + old_ids

    if not volume_ids:
        logger.debug("Volume backfill: nothing to do")
        return {"updated": 0, "queued": 0}

    logger.info(
        "Volume backfill: fetching %d volumes (%d calendar, %d old)",
        len(volume_ids),
        len(calendar_ids),
        len(old_ids),
    )
    updated = 0
    for vid in volume_ids:
        if client.total_hourly_calls() > _BACKFILL_HOURLY_BUDGET:
            logger.info(
                "Volume backfill: pausing after %d upstream calls in the past hour "
                "(threshold: %d)",
                client.total_hourly_calls(),
                _BACKFILL_HOURLY_BUDGET,
            )
            break
        sleep_secs = _backfill_call_interval(client)
        data = await client.get_volume(vid)
        if data is _CV_NOT_FOUND:
            db.mark_backfill_missing("cv_volume", vid)
        elif data and data.get("results"):
            db.upsert_volume(data["results"], commit=False)
            updated += 1
        await asyncio.sleep(sleep_secs)
    db.conn.commit()
    logger.info("Volume backfill: updated %d/%d volumes", updated, len(volume_ids))
    return {"updated": updated, "queued": len(volume_ids)}


async def _backfill_issues(client: "CVClient", db: "Database") -> None:
    """Re-fetch issues that are missing the new detail fields.

    Prioritises issues with a recent store_date, then falls back to
    newest issues first.  IDs that CV confirms as not found are marked
    so they are skipped in subsequent runs.

    All upserts are deferred to a single commit at the end so the asyncio
    event loop is never blocked by repeated fsync calls during the run.
    """
    cur = db.conn.execute(
        """
        SELECT id FROM cv_issue
        WHERE date_last_updated IS NULL
          AND (backfill_missing IS NULL OR backfill_missing = 0)
          AND store_date IS NOT NULL
        ORDER BY store_date DESC
        LIMIT ?
        """,
        [_ISSUE_BATCH],
    )
    issue_ids = [row[0] for row in cur.fetchall()]

    if len(issue_ids) < _ISSUE_BATCH:
        seen = set(issue_ids)
        cur2 = db.conn.execute(
            """
            SELECT id FROM cv_issue
            WHERE date_last_updated IS NULL
              AND (backfill_missing IS NULL OR backfill_missing = 0)
            ORDER BY id DESC
            LIMIT ?
            """,
            [_ISSUE_BATCH - len(issue_ids)],
        )
        for row in cur2.fetchall():
            if row[0] not in seen:
                issue_ids.append(row[0])
                seen.add(row[0])

    if not issue_ids:
        logger.debug("Issue backfill: nothing to do")
        return {"updated": 0, "queued": 0}

    logger.info("Issue backfill: fetching %d issues", len(issue_ids))
    updated = 0
    for iid in issue_ids:
        if client.total_hourly_calls() > _BACKFILL_HOURLY_BUDGET:
            logger.info(
                "Issue backfill: pausing after %d upstream calls in the past hour "
                "(threshold: %d)",
                client.total_hourly_calls(),
                _BACKFILL_HOURLY_BUDGET,
            )
            break
        sleep_secs = _backfill_call_interval(client)
        data = await client.get_issue(iid)
        if data is _CV_NOT_FOUND:
            db.mark_backfill_missing("cv_issue", iid)
        elif data and data.get("results"):
            db.upsert_issue(data["results"], commit=False)
            updated += 1
        await asyncio.sleep(sleep_secs)
    db.conn.commit()
    logger.info("Issue backfill: updated %d/%d issues", updated, len(issue_ids))
    return {"updated": updated, "queued": len(issue_ids)}


async def _backfill_publishers(client: "CVClient", db: "Database") -> None:
    """Re-fetch publishers that are missing the new detail fields."""
    cur = db.conn.execute(
        """
        SELECT id FROM cv_publisher
        WHERE date_last_updated IS NULL
          AND (backfill_missing IS NULL OR backfill_missing = 0)
        ORDER BY id DESC
        LIMIT ?
        """,
        [_PUBLISHER_BATCH],
    )
    publisher_ids = [row[0] for row in cur.fetchall()]

    if not publisher_ids:
        logger.debug("Publisher backfill: nothing to do")
        return {"updated": 0, "queued": 0}

    logger.info("Publisher backfill: fetching %d publishers", len(publisher_ids))
    updated = 0
    for pid in publisher_ids:
        if client.total_hourly_calls() > _BACKFILL_HOURLY_BUDGET:
            logger.info(
                "Publisher backfill: pausing after %d upstream calls in the past hour "
                "(threshold: %d)",
                client.total_hourly_calls(),
                _BACKFILL_HOURLY_BUDGET,
            )
            break
        sleep_secs = _backfill_call_interval(client)
        data = await client.get_publisher(pid)
        if data is _CV_NOT_FOUND:
            db.mark_backfill_missing("cv_publisher", pid)
        elif data and data.get("results"):
            db.upsert_publisher(data["results"], commit=False)
            updated += 1
        await asyncio.sleep(sleep_secs)
    db.conn.commit()
    logger.info("Publisher backfill: updated %d/%d publishers", updated, len(publisher_ids))
    return {"updated": updated, "queued": len(publisher_ids)}


# ---------------------------------------------------------------------------
# Incremental issue sync  (issues endpoint, date_last_updated last 48h)
# ---------------------------------------------------------------------------


async def _sync_recent_issues(client: "CVClient", db: "Database", hours: int = 48) -> None:
    """Pull issues updated in the last *hours* hours and upsert them.

    Keeps the local cache fresh for recently-edited CV entries
    (description added, store_date corrected, etc.).

    Unlike the individual backfill jobs this function queries CV by date range
    rather than fetching specific IDs from the local DB, so backfill_missing
    has no effect here and is never set.  Issues previously marked missing by a
    backfill run will have their flag cleared automatically the next time an
    INSERT OR REPLACE succeeds (because backfill_missing is not in the column
    list, so the replacement row gets NULL).
    """
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    until = now.strftime("%Y-%m-%d %H:%M:%S")

    logger.info("Incremental sync: issues updated %s to %s (window: %dh)", since, until, hours)

    total_upserted = 0
    offset = 0
    limit = 100

    while True:
        data = await client.get(
            "issues/",
            {
                "filter": f"date_last_updated:{since}|{until}",
                "sort": "date_last_updated:asc",
                "limit": limit,
                "offset": offset,
            },
        )
        # NOT_FOUND on a list endpoint means the date window returned nothing —
        # treat it as an empty result rather than a warning.
        if data is _CV_NOT_FOUND:
            break
        if not data:
            logger.warning("Incremental sync: upstream request failed at offset %d", offset)
            break

        results = data.get("results") or []
        for issue in results:
            db.upsert_issue(issue, commit=False)
        if results:
            db.conn.commit()
        total_upserted += len(results)

        total_available = data.get("number_of_total_results", 0)
        offset += limit

        if offset >= total_available or not results:
            break

    logger.info("Incremental sync: upserted %d issues", total_upserted)
    return {"issues_upserted": total_upserted}


# Public alias — lets app.py (and tests) call this without referencing the
# private name directly.
sync_recent_issues = _sync_recent_issues


# ---------------------------------------------------------------------------
# Stale entity eviction
# ---------------------------------------------------------------------------


async def _evict_stale_data(db: "Database", settings: "Settings") -> None:
    """Remove stale entities and expired response cache entries.

    Issues:         deleted if cover_date < cutoff AND last_accessed older than access window
    Volumes:        cascade-deleted if no remaining issues AND last_accessed stale
    Response cache: deleted if cached_at older than response_cache_ttl_days
    All operations are no-ops when the relevant setting is 0 (disabled).
    """
    from datetime import date as _date

    issues_deleted = vol_deleted = cache_deleted = 0

    if settings.evict_older_than_years > 0:
        cutoff = (_date.today() - timedelta(days=settings.evict_older_than_years * 365)).isoformat()
        access_expiry = (
            datetime.now(timezone.utc) - timedelta(days=settings.evict_unaccessed_days)
        ).strftime("%Y-%m-%d %H:%M:%S")
        issues_deleted = db.evict_stale_issues(cutoff, access_expiry)
        vol_deleted = db.evict_orphaned_volumes(access_expiry)
        logger.info("Eviction: removed %d issues, %d orphaned volumes", issues_deleted, vol_deleted)

    if settings.response_cache_ttl_days > 0 or settings.search_cache_ttl_days > 0:
        cache_cutoff = (
            (datetime.now(timezone.utc) - timedelta(days=settings.response_cache_ttl_days)).strftime("%Y-%m-%dT%H:%M:%S")
            if settings.response_cache_ttl_days > 0
            else "1900-01-01T00:00:00"
        )
        search_cutoff = (
            (datetime.now(timezone.utc) - timedelta(days=settings.search_cache_ttl_days)).strftime("%Y-%m-%dT%H:%M:%S")
            if settings.search_cache_ttl_days > 0
            else None
        )
        cache_deleted = db.evict_response_cache(cache_cutoff, search_cutoff_date=search_cutoff)
        logger.info("Eviction: removed %d response cache entries", cache_deleted)

    if not issues_deleted and not vol_deleted and not cache_deleted:
        logger.debug("Eviction: nothing to remove (all settings disabled or no eligible rows)")
    return {"issues_deleted": issues_deleted, "volumes_deleted": vol_deleted, "cache_deleted": cache_deleted}


# Public alias for app.py admin endpoint
evict_stale_data = _evict_stale_data


# ---------------------------------------------------------------------------
# Search cache volume backfill  (fetch volumes seen in search results but not
# yet indexed locally)
# ---------------------------------------------------------------------------

_SEARCH_BACKFILL_BATCH = 50


async def _backfill_search_cache_volumes(client: "CVClient", db: "Database") -> None:
    """Fetch volumes referenced in search caches but absent from cv_volume.

    When a search result references a volume ID that isn't in our local
    cv_volume table yet, this job fetches and upserts those volumes so that
    future search requests can enrich results from local data.
    """
    missing_ids = db.get_search_cache_missing_volume_ids(limit=_SEARCH_BACKFILL_BATCH)

    if not missing_ids:
        logger.debug("Search cache volume backfill: nothing to do")
        return {"updated": 0, "queued": 0}

    logger.info("Search cache volume backfill: fetching %d missing volumes", len(missing_ids))
    updated = 0
    for vid in missing_ids:
        if client.total_hourly_calls() > _BACKFILL_HOURLY_BUDGET:
            logger.info(
                "Search cache volume backfill: pausing after %d upstream calls in the past hour "
                "(threshold: %d)",
                client.total_hourly_calls(),
                _BACKFILL_HOURLY_BUDGET,
            )
            break
        sleep_secs = _backfill_call_interval(client)
        data = await client.get_volume(vid)
        if data is _CV_NOT_FOUND:
            db.mark_backfill_missing("cv_volume", vid)
        elif data and data.get("results"):
            db.upsert_volume(data["results"], commit=False)
            updated += 1
        await asyncio.sleep(sleep_secs)
    db.conn.commit()
    logger.info(
        "Search cache volume backfill: upserted %d/%d volumes", updated, len(missing_ids)
    )
    return {"updated": updated, "queued": len(missing_ids)}
