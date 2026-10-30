"""Background jobs: what each one does, when it runs, and the job functions.

Every job is described by a :class:`~cvproxy.jobs.JobSpec` (see
:func:`build_job_specs`) and runs through the :class:`~cvproxy.jobs.JobRunner`,
which records each run (scheduled or manual) with the same fields.  Job
functions return a result dict; a ``stop_reason`` key says why a run ended
early or did nothing (see ``cvproxy.jobs.STOP_REASONS``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from cvproxy.config import get_settings
from cvproxy.covers import hash_covers
from cvproxy.cv_client import NOT_FOUND as _CV_NOT_FOUND
from cvproxy.cv_client import RATE_LIMITED as _CV_RATE_LIMITED
from cvproxy.db import backfill_window_sql
from cvproxy.jobs import JobRunner, JobSpec, Param, report_progress
from cvproxy.mcl import read_mcl, refresh_mcl
from cvproxy.pending import process_pending

if TYPE_CHECKING:
    from cvproxy.config import Settings
    from cvproxy.cv_client import CVClient, CVResponse
    from cvproxy.db import Database
    from cvproxy.image_cache import ImageCache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backfill rate pacing
# ---------------------------------------------------------------------------

#: Upstream calls per hour the backfill jobs may make to any one endpoint.
#: ComicVine budgets each resource path separately; the rest of that path's
#: hourly window (rate_limit_per_hour_per_endpoint, 180) is kept for live
#: requests.  Jobs that call the same endpoint share this budget.
_BACKFILL_HOURLY_BUDGET = 120

#: Hard cap on the time to sleep between individual backfill calls (seconds).
_BACKFILL_MAX_SLEEP = 120.0
#: Minimum sleep so the loop never hammers the API even if the budget maths
#: produce a tiny value.
_BACKFILL_MIN_SLEEP = 2.0

#: Seconds a job may start late and still run (see create_scheduler).
_MISFIRE_GRACE_SECONDS = 3600


def _backfill_call_interval(client: CVClient, endpoint: str) -> float:
    """Compute seconds to sleep after each backfill upstream call to *endpoint*.

    Divides the remaining seconds in the current clock-hour by the endpoint's
    remaining backfill budget.  This spreads the allowed calls evenly across the hour so
    that live client requests always have headroom between background fetches.

    Example: 45 min left, 100 budget remaining -> sleep 27 s per call.
    """
    now_utc = datetime.now(UTC)
    secs_left_in_hour = max(
        1.0,
        3600.0 - (now_utc.minute * 60 + now_utc.second + now_utc.microsecond / 1_000_000),
    )
    used = client.hourly_calls(endpoint)
    remaining_budget = max(1, _BACKFILL_HOURLY_BUDGET - used)
    interval = secs_left_in_hour / remaining_budget
    return max(_BACKFILL_MIN_SLEEP, min(interval, _BACKFILL_MAX_SLEEP))


def _budget_stop(client: CVClient, db: Database, endpoint: str, label: str) -> str | None:
    """Why a backfill must pause before its next call to *endpoint*, or None."""
    if db.pending_waiting(endpoint):
        logger.info("%s: client requests queued for /%s, pausing", label, endpoint)
        return "client_queue"
    used = client.hourly_calls(endpoint)
    if used >= _BACKFILL_HOURLY_BUDGET:
        logger.info(
            "%s: pausing after %d /%s calls in the past hour (backfill budget: %d)",
            label, used, endpoint, _BACKFILL_HOURLY_BUDGET,
        )
        return "budget"
    return None


def _upstream_failure(data: object) -> str:
    return "rate_limited" if data is _CV_RATE_LIMITED else "upstream_error"


# ---------------------------------------------------------------------------
# Result summaries (one line, shown in the job history and the log)
# ---------------------------------------------------------------------------


def _sum_backfill(r: dict[str, Any]) -> str:
    text = f"{r.get('updated', 0)}/{r.get('queued', 0)} updated"
    if r.get("missing"):
        text += f", {r['missing']} not on ComicVine"
    if r.get("failed"):
        text += f", {r['failed']} failed"
    return text


def _sum_sync(r: dict[str, Any]) -> str:
    text = f"{r.get('issues_upserted', 0)} issues upserted"
    if r.get("watermark"):
        text += f", watermark {r['watermark']}"
    if r.get("window_hours"):
        text += f" (window {r['window_hours']}h)"
    return text


def _sum_mcl_gap(r: dict[str, Any]) -> str:
    return (
        f"volumes {r.get('volumes_added', 0)}/{r.get('volumes_missing', 0)} added, "
        f"issues {r.get('issues_added', 0)}/{r.get('issues_missing', 0)} added"
    )


def _sum_covers(r: dict[str, Any]) -> str:
    return (
        f"{r.get('hashed', 0)} hashed, {r.get('failed', 0)} without image, "
        f"{r.get('issues', 0)} issues (scan {r.get('scan_s', 0)}s)"
    )


def _sum_pending(r: dict[str, Any]) -> str:
    return (
        f"{r.get('fetched', 0)} fetched, {r.get('skipped', 0)} waiting, "
        f"{r.get('failed', 0)} failed"
    )


def _sum_wal(r: dict[str, Any]) -> str:
    return (
        f"{r.get('checkpointed', 0)}/{r.get('log_frames', 0)} frames, "
        f"WAL {r.get('wal_bytes', 0) / 1_048_576:.0f} MB"
    )


def _sum_export(r: dict[str, Any]) -> str:
    if not r.get("path"):
        return "no export path"
    return (
        f"{r.get('cv_issue', 0)} issues, {r.get('cv_volume', 0)} volumes, "
        f"{r.get('cv_publisher', 0)} publishers, {r.get('comic_covers', 0)} covers"
    )


def _sum_evict(r: dict[str, Any]) -> str:
    if r.get("dry_run"):
        return (
            f"dry run: would remove {r.get('issues', 0)} issues, {r.get('volumes', 0)} volumes, "
            f"{r.get('response_cache', 0)} cache entries"
        )
    return (
        f"removed {r.get('issues_deleted', 0)} issues, {r.get('volumes_deleted', 0)} volumes, "
        f"{r.get('cache_deleted', 0)} cache entries"
    )


def _sum_refetch(r: dict[str, Any]) -> str:
    parts = [f"{r.get('fetched', 0)}/{r.get('requested', 0)} by ID"]
    if r.get("not_on_cv"):
        parts.append(f"{r['not_on_cv']} not on ComicVine")
    if r.get("volume_id"):
        parts.append(f"{r.get('volume_issues', 0)} from volume {r['volume_id']}")
    if r.get("details_requested"):
        parts.append(f"details {r.get('details_fetched', 0)}/{r['details_requested']}")
    return ", ".join(parts)


def _sum_queue(r: dict[str, Any]) -> str:
    pending = sum((r.get("pending_fetch") or {}).values())
    since = f" ({r['backfill_min_year']}+ or undated)" if r.get("backfill_min_year") else ""
    return (
        f"issues{since}: {r.get('issues_missing_details_in_window', 0)} need details, "
        f"{r.get('issues_missing_fields_in_window', 0)} need fields; "
        f"volumes: {r.get('volumes_missing_fields', 0)}; "
        f"publishers: {r.get('publishers_missing_fields', 0)}; pending: {pending}"
    )


def _sum_audit(r: dict[str, Any]) -> str:
    return (
        f"scanned {r.get('scanned', 0)}, {r.get('problems', 0)} suspicious "
        f"({r.get('purgeable', 0)} purgeable), deleted {r.get('deleted', 0)}"
    )


def _sum_integrity(r: dict[str, Any]) -> str:
    return "ok" if r.get("ok") else f"{len(r.get('problems') or [])} problem(s)"

# ---------------------------------------------------------------------------
# Job catalog
# ---------------------------------------------------------------------------


def _cron(**kw: Any) -> dict[str, Any]:
    return {"trigger": "cron", **kw}


def _no_backups(s: Settings) -> str | None:
    if s.backup_keep <= 0:
        return "BACKUP_KEEP=0 (snapshots come from the sqlite-backup container)"
    return None


def _no_export(s: Settings) -> str | None:
    return None if s.cv_db_export_path is not None else "CV_DB_EXPORT_PATH is not set"


def _no_mcl(s: Settings) -> str | None:
    return None if s.mcl_export_path is not None else "MCL_EXPORT_PATH is not set"


def _no_covers(s: Settings) -> str | None:
    return None if s.cover_hash_per_hour > 0 else "COVER_HASH_PER_HOUR=0"


def build_job_specs(
    settings: Settings,
    image_cache: ImageCache,
    db: Database,
    client: CVClient,
) -> list[JobSpec]:
    """Every background job.  Run functions read settings when they run."""
    h = settings.sync_cron_hour

    def job(fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> Any:
        return fn

    async def run_refetch(p: dict[str, Any]) -> dict[str, Any]:
        return await _refetch_issues(
            client, db, parse_id_list(p["ids"]), p["volume_id"], details=p["details"]
        )

    return [
        # -- sync ------------------------------------------------------------
        JobSpec(
            id="incremental_issue_sync", name="Incremental issue sync", category="sync",
            description=(
                "Pulls every issue ComicVine changed since the stored watermark (the newest "
                "date_last_updated already saved) and upserts it. Walks the gap in 24 h "
                "slices, at most 45 per run, and only moves the watermark over data that "
                "was fully stored, so a failed run is resumed by the next one."
            ),
            run=job(lambda p: _sync_issues_incremental(client, db)),
            trigger=_cron(minute=10), endpoints=("issues/",), summarize=_sum_sync,
        ),
        JobSpec(
            id="releases_prefetch", name="Releases prefetch", category="sync",
            description=(
                "Fetches every issue with a store date from two weeks ago to one week ahead, "
                "so new-release lists and calendars are answered from the local database."
            ),
            run=job(lambda p: _prefetch_releases(client, db)),
            trigger=_cron(hour=h, minute=0), endpoints=("issues/",),
        ),
        JobSpec(
            id="issue_resync", name="Issue re-sync (time window)", category="sync",
            description=(
                "Re-reads every issue ComicVine changed in the last N days, regardless of "
                "the watermark. Use it after a ComicVine outage or to pick up older "
                "corrections. Does not move the scheduled sync's watermark. Costs about one "
                "/issues/ call per 100 changed issues."
            ),
            run=job(lambda p: _sync_recent_issues(client, db, int(p["days"] * 24))),
            endpoints=("issues/",), summarize=_sum_sync,
            params=(Param("days", "float", 14.0, "Look-back (days)",
                          "Issues whose date_last_updated is within this many days.", 0.1, 365),),
        ),
        JobSpec(
            id="issue_refetch", name="Re-fetch issues", category="sync",
            description=(
                "Re-fetches specific issues from ComicVine: a list of issue IDs (100 per "
                "/issues/ call) and/or every issue of one volume. 'Full details' also fetches "
                "each issue from /issue/ for credits (one call each, first 100 only). Uses the "
                "backfill budget and waits for queued client requests."
            ),
            run=job(run_refetch), endpoints=("issues/", "issue/"), summarize=_sum_refetch,
            params=(
                Param("ids", "text", "", "Issue IDs",
                      "Comma, space or newline separated; 4000-12345 style IDs work too."),
                Param("volume_id", "int", 0, "Volume ID", "Also re-fetch all of this "
                      "volume's issues (0 = none).", 0),
                Param("details", "bool", False, "Full details",
                      "Fetch /issue/ details (credits) for up to 100 of them."),
            ),
        ),
        JobSpec(
            id="pending_fetch", name="Pending fetch", category="sync",
            description=(
                "Fetches cache misses that came in while ComicVine couldn't be asked "
                "(cooldown or a full hourly window), oldest first, keeping 30 calls per hour "
                "per endpoint for live requests. Runs that had nothing to fetch, or only "
                "waited for a window, are not kept in the history."
            ),
            run=job(lambda p: process_pending(client, db)),
            trigger={"trigger": "interval", "minutes": 2}, persist_idle=False,
            endpoints=("issue/", "volume/", "publisher/", "issues/", "search/"),
            summarize=_sum_pending,
        ),
        # -- backfill --------------------------------------------------------
        JobSpec(
            id="issue_detail_backfill", name="Issue detail backfill", category="backfill",
            description=(
                "Fetches /issue/ details (credits, variant covers) for up to 100 issues that "
                "were stored from list responses only, newest first, limited to issues dated "
                "BACKFILL_MIN_YEAR or later (or undated). Shares the /issue/ backfill budget "
                "(120 calls/h) and yields to queued client requests."
            ),
            run=job(lambda p: _backfill_issue_details(client, db)),
            trigger=_cron(minute=5), endpoints=("issue/",), summarize=_sum_backfill,
        ),
        JobSpec(
            id="issue_backfill", name="Issue backfill", category="backfill",
            description=(
                "Re-fetches up to 150 issues cached by an older cvproxy that lack the newer "
                "fields (no date_last_updated): issues dated BACKFILL_MIN_YEAR or later by "
                "store date, newest first, then undated ones and the rest of the window by "
                "newest ID. Older issues are refreshed only when a client asks for them. IDs "
                "ComicVine no longer has are marked and skipped from then on."
            ),
            run=job(lambda p: _backfill_issues(client, db)),
            trigger=_cron(minute=45), endpoints=("issue/",), summarize=_sum_backfill,
        ),
        JobSpec(
            id="volume_backfill", name="Volume backfill", category="backfill",
            description=(
                "Re-fetches volumes missing detail fields: up to 100 that have dated issues "
                "(calendar-relevant) plus 50 older ones."
            ),
            run=job(lambda p: _backfill_volumes(client, db)),
            trigger=_cron(minute=30), endpoints=("volume/",), summarize=_sum_backfill,
        ),
        JobSpec(
            id="publisher_backfill", name="Publisher backfill", category="backfill",
            description="Re-fetches up to 50 publishers missing detail fields.",
            run=job(lambda p: _backfill_publishers(client, db)),
            trigger=_cron(hour="*/6", minute=15), endpoints=("publisher/",),
            summarize=_sum_backfill,
        ),
        JobSpec(
            id="search_cache_volume_backfill", name="Search cache volume backfill",
            category="backfill",
            description=(
                "Fetches volumes that appear in cached search results but aren't stored yet "
                "(up to 50 per run), so later searches can be answered locally."
            ),
            run=job(lambda p: _backfill_search_cache_volumes(client, db)),
            trigger=_cron(hour="*/2", minute=50), endpoints=("volume/",),
            summarize=_sum_backfill,
        ),
        JobSpec(
            id="mcl_gap_backfill", name="MCL gap backfill", category="backfill",
            description=(
                "Compares the community latest.mcl with the database and fetches volumes and "
                "issues it lists that cvproxy doesn't have, 100 IDs per list call, newest "
                "first. Issues arrive list-shaped; the issue detail backfill adds credits."
            ),
            run=job(lambda p: _backfill_from_mcl(client, db, get_settings())),
            trigger=_cron(hour=(h + 6) % 24, minute=0), endpoints=("volumes/", "issues/"),
            disabled=_no_mcl, summarize=_sum_mcl_gap,
        ),
        JobSpec(
            id="cover_hash_backfill", name="Cover hash backfill", category="backfill",
            description=(
                "Hashes the cover images (pHash and average hash) of issues missing from "
                "comic_covers, for ComicManager's cover matching: up to COVER_HASH_PER_HOUR "
                "images per run from ComicVine's image CDN (no API quota), for issues dated "
                "BACKFILL_MIN_YEAR or later (or undated)."
            ),
            run=job(lambda p: _backfill_cover_hashes(db, get_settings())),
            trigger=_cron(minute=20), disabled=_no_covers, summarize=_sum_covers,
        ),        # -- export ----------------------------------------------------------
        JobSpec(
            id="comicvine_db_export", name="comicvine.db export", category="export",
            description=(
                "Builds a ComicManager-compatible comicvine.db on local disk from a read-only "
                "snapshot and moves it into CV_DB_EXPORT_PATH with an atomic rename "
                "(ComicManager opens it immutable)."
            ),
            run=job(lambda p: _export_comicvine_db(db, get_settings())),
            trigger=_cron(hour=(h + 5) % 24, minute=0), disabled=_no_export,
            cancellable=False, summarize=_sum_export,
        ),
        JobSpec(
            id="mcl_refresh", name="latest.mcl refresh", category="export",
            description=(
                "Downloads the newest community latest.mcl to MCL_EXPORT_PATH; the file is "
                "only replaced when it changed and parses."
            ),
            run=job(lambda p: _refresh_mcl(get_settings())),
            trigger=_cron(hour=(h + 5) % 24, minute=30), disabled=_no_mcl,
        ),
        # -- maintenance -----------------------------------------------------
        JobSpec(
            id="wal_checkpoint", name="WAL checkpoint", category="maintenance",
            description=(
                "PASSIVE WAL checkpoint on its own connection: copies committed pages into "
                "the database without taking the write lock. Ends 'partial' when a "
                "long-running reader keeps a large WAL from being checkpointed."
            ),
            run=job(lambda p: _checkpoint_wal(db)),
            trigger=_cron(minute=40), cancellable=False, summarize=_sum_wal,
        ),
        JobSpec(
            id="evict_stale_data", name="Cache eviction", category="maintenance",
            description=(
                "Deletes old, unaccessed issues (EVICT_OLDER_THAN_YEARS / "
                "EVICT_UNACCESSED_DAYS), volumes left without issues, and response and search "
                "cache entries past RESPONSE_CACHE_TTL_DAYS / SEARCH_CACHE_TTL_DAYS. Manual "
                "runs default to a dry run that only counts."
            ),
            run=job(lambda p: _evict_stale_data(db, get_settings(), dry_run=p["dry_run"])),
            trigger=_cron(hour=(h + 3) % 24, minute=0), destructive=True,
            params=(Param("dry_run", "bool", True, "Dry run (count only)",
                          "Uncheck to actually delete."),),
            schedule_params={"dry_run": False}, summarize=_sum_evict,
        ),
        JobSpec(
            id="image_cache_cleanup", name="Image cache cleanup", category="maintenance",
            description=(
                "Removes cached images not accessed within IMAGE_CACHE_TTL_DAYS."
            ),
            run=job(lambda p: _cleanup_images(image_cache)),
            trigger=_cron(hour=(h + 2) % 24, minute=0),
        ),
        JobSpec(
            id="database_backup", name="Database backup", category="maintenance",
            description=(
                "In-app VACUUM INTO snapshot of localcv.db into BACKUP_DIR, keeping "
                "BACKUP_KEEP copies."
            ),
            run=job(lambda p: _backup_database(db, get_settings())),
            trigger=_cron(hour=(h + 4) % 24, minute=0), disabled=_no_backups,
            cancellable=False,
        ),
        JobSpec(
            id="queue_snapshot", name="Backlog snapshot", category="maintenance",
            description=(
                "Counts what every backfill still has to do (issues without details or "
                "fields, volumes, publishers), the pending-fetch queue, response cache size "
                "and the sync watermark. Read-only; the issue count scans cv_issue, so it "
                "takes a few minutes on this disk. Shown in the Backlog card."
            ),
            run=job(lambda p: _queue_snapshot(db)),
            trigger=_cron(hour=(h + 2) % 24, minute=30), cancellable=False,
            summarize=_sum_queue,
        ),
        JobSpec(
            id="response_cache_audit", name="Response cache audit", category="maintenance",
            description=(
                "Scans cv_response_cache for cached failures: ComicVine error envelopes, "
                "unparseable rows and empty results (negative-cache entries are ignored). "
                "The weekly run only reports; a manual run with 'Purge' deletes error "
                "envelopes and unparseable rows so they are fetched again."
            ),
            run=job(lambda p: _audit_response_cache(db, purge=p["purge"])),
            trigger=_cron(day_of_week="sat", hour=(h + 1) % 24, minute=30),
            cancellable=False, destructive=True, summarize=_sum_audit,
            params=(Param("purge", "bool", False, "Purge bad entries",
                          "Delete error envelopes and unparseable rows."),),
        ),
        JobSpec(
            id="integrity_check", name="Integrity check", category="maintenance",
            description=(
                "PRAGMA quick_check on a read-only connection. Reads the whole 4 GB file, so "
                "it takes several minutes; run it after a crash or a disk problem."
            ),
            run=job(lambda p: _integrity_check(db)), cancellable=False,
            summarize=_sum_integrity,
        ),
        # -- repair ----------------------------------------------------------
        JobSpec(
            id="publisher_repair", name="Publisher repair", category="repair",
            description=(
                "Re-reads id and publisher for cached volumes from /volumes/ and writes "
                "ComicVine's publisher IDs back, for databases seeded by tools that "
                "numbered publishers themselves. 'Calendar volumes only' limits it to "
                "volumes with dated issues."
            ),
            run=job(lambda p: _repair_publishers(
                client, db, batch_size=p["batch_size"], calendar_only=p["calendar_only"]
            )),
            endpoints=("volumes/",),
            params=(
                Param("batch_size", "int", 100, "Batch size", "Volumes per /volumes/ call.",
                      10, 100),
                Param("calendar_only", "bool", True, "Calendar volumes only",
                      "Only volumes with an issue that has a store date."),
            ),
        ),
        JobSpec(
            id="fts_rebuild", name="FTS rebuild", category="repair",
            description=(
                "Rebuilds the full-text search indexes (volume_fts, issue_fts, "
                "publisher_fts) on a connection of its own. Triggers keep them current; "
                "only needed after editing the database outside cvproxy."
            ),
            run=job(lambda p: _rebuild_fts(db)), cancellable=False,
        ),
    ]


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------


def create_scheduler(
    settings: Settings,
    image_cache: ImageCache,
    db: Database,
    client: CVClient,
    runner: JobRunner | None = None,
) -> AsyncIOScheduler:
    """Create the scheduler with every enabled job that has a schedule.

    With *runner*, its registered specs are used (and registered here when it
    has none); without one a throwaway in-memory runner is created.
    """
    # A job held up past its start time (event loop busy, container restart)
    # still runs within the grace period instead of being dropped; APScheduler's
    # default grace is 1 second, which silently skipped whole sync runs.
    scheduler = AsyncIOScheduler(
        job_defaults={"coalesce": True, "misfire_grace_time": _MISFIRE_GRACE_SECONDS}
    )
    if runner is None:
        runner = JobRunner()
    if not runner.specs:
        runner.register(build_job_specs(settings, image_cache, db, client))
    for spec in runner.specs.values():
        if spec.trigger is None or spec.disabled(settings):
            continue
        trigger = dict(spec.trigger)
        kind = trigger.pop("trigger")
        scheduler.add_job(
            runner.scheduled(spec.id),
            kind,
            id=spec.id,
            name=spec.name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            **trigger,
        )
    runner.attach(scheduler)
    return scheduler


def parse_id_list(text: str, *, limit: int = 5000) -> list[int]:
    """Issue IDs from free text: commas, spaces, newlines; '4000-123' gives 123."""
    ids: list[int] = []
    bad: list[str] = []
    for token in re.split(r"[\s,;|]+", text or ""):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            token = token.rsplit("-", 1)[1]
        if token.isdigit():
            ids.append(int(token))
        else:
            bad.append(token)
    if bad:
        raise ValueError(f"Not issue IDs: {', '.join(bad[:5])}")
    unique = list(dict.fromkeys(ids))
    if len(unique) > limit:
        raise ValueError(f"At most {limit} IDs per run ({len(unique)} given)")
    return unique

# ---------------------------------------------------------------------------
# Image cache cleanup
# ---------------------------------------------------------------------------


async def _cleanup_images(image_cache: ImageCache) -> dict[str, Any]:
    """Remove expired image cache entries (errors propagate to the job record)."""
    removed = image_cache.cleanup_expired()
    logger.info("Image cache cleanup: removed %d expired images", removed)
    result: dict[str, Any] = {"removed": removed}
    if not removed:
        result["stop_reason"] = "nothing_to_do"
    return result


# ---------------------------------------------------------------------------
# Releases prefetch  (issues endpoint, store_date -2w / +1w)
# ---------------------------------------------------------------------------


async def _prefetch_releases(client: CVClient, db: Database) -> dict[str, Any]:
    """Fetch upcoming/recent releases from CV and upsert into local DB.

    Window: 2 weeks in the past through 1 week in the future.
    Comics data on CV typically lags, so looking backward catches
    recently-added entries.
    """
    today = datetime.now(UTC).date()
    start = (today - timedelta(weeks=2)).isoformat()
    end = (today + timedelta(weeks=1)).isoformat()

    logger.info("Releases prefetch: fetching store_date %s to %s", start, end)

    total_upserted = 0
    offset = 0
    limit = 100
    stop: str | None = None

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
        if data is _CV_NOT_FOUND:
            break
        if not data:
            stop = _upstream_failure(data)
            logger.warning("Releases prefetch: upstream request failed at offset %d (%s)",
                           offset, stop)
            break

        results = data.get("results") or []
        if results:
            _store_issue_page(db, results)
        total_upserted += len(results)

        total_available = data.get("number_of_total_results", 0)
        offset += limit
        report_progress(min(offset, total_available), total_available)

        if offset >= total_available or not results:
            break

    logger.info("Releases prefetch: upserted %d issues", total_upserted)
    result: dict[str, Any] = {"window": f"{start}..{end}", "issues_upserted": total_upserted}
    if stop:
        result["stop_reason"] = stop
    return result


# ---------------------------------------------------------------------------
# Entity backfill  (re-fetch rows cached before new columns were added)
# ---------------------------------------------------------------------------

# ``date_last_updated`` is the sentinel column: CV always returns it, so a
# NULL value means the row was cached by an older version of the code that
# didn't store these fields yet.

_VOLUME_CALENDAR_BATCH = 100  # new/updated volumes (calendar-relevant)
_VOLUME_OLD_BATCH = 50  # older stale rows
_ISSUE_BATCH = 150
_PUBLISHER_BATCH = 50  # publishers table is much smaller
_ISSUE_DETAIL_BATCH = 100
_SEARCH_BACKFILL_BATCH = 50


async def _backfill_ids(
    client: CVClient,
    db: Database,
    *,
    label: str,
    endpoint: str,
    table: str,
    ids: list[int],
    fetch: Callable[[int], Awaitable[CVResponse]],
    store: Callable[[dict[str, Any]], None],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch *ids* one by one from a detail endpoint and store the results.

    The shared loop of every per-ID backfill: yields to queued client
    requests, stays inside the endpoint's backfill budget, paces calls across
    the hour, stops at the first ComicVine refusal, and marks IDs ComicVine
    no longer has.  Upserts are committed once at the end.
    """
    result: dict[str, Any] = {"queued": len(ids), "updated": 0, "missing": 0, "failed": 0}
    result.update(extra or {})
    if not ids:
        logger.debug("%s: nothing to do", label)
        result["stop_reason"] = "nothing_to_do"
        return result

    logger.info("%s: fetching %d", label, len(ids))
    stop: str | None = None
    for done, entity_id in enumerate(ids, 1):
        stop = _budget_stop(client, db, endpoint, label)
        if stop:
            break
        sleep_secs = _backfill_call_interval(client, endpoint)
        data = await fetch(entity_id)
        if data is _CV_RATE_LIMITED:
            logger.info("%s: rate limited by CV, stopping this run", label)
            stop = "rate_limited"
            break
        if data is _CV_NOT_FOUND:
            db.mark_backfill_missing(table, entity_id)
            result["missing"] += 1
        elif data and data.get("results"):
            store(data["results"])
            result["updated"] += 1
        else:
            result["failed"] += 1
        report_progress(done, len(ids))
        await asyncio.sleep(sleep_secs)
    db.conn.commit()
    if stop:
        result["stop_reason"] = stop
    logger.info(
        "%s: updated %d/%d (%d not on ComicVine, %d failed)",
        label, result["updated"], len(ids), result["missing"], result["failed"],
    )
    return result


async def _backfill_volumes(client: CVClient, db: Database) -> dict[str, Any]:
    """Re-fetch volumes that are missing the new detail fields.

    Two-tier batching:
      - Up to 100 calendar-relevant volumes (tied to issues with a
        store_date): these are new/updated volumes that matter most.
      - Up to 50 older stale volumes: backfills the long tail.
    """
    calendar_ids, old_ids = db.volume_ids_needing_backfill(
        _VOLUME_CALENDAR_BATCH, _VOLUME_OLD_BATCH
    )
    return await _backfill_ids(
        client, db, label="Volume backfill", endpoint="volume/", table="cv_volume",
        ids=calendar_ids + old_ids, fetch=client.get_volume,
        store=lambda r: db.upsert_volume(r, commit=False),
        extra={"calendar": len(calendar_ids), "older": len(old_ids)},
    )


def _issue_backfill_ids(db: Database, min_year: int = 0) -> list[int]:
    """Issue IDs missing detail fields: dated issues newest first, then newest IDs.

    Uses a read-only connection of its own so it can run on a worker thread:
    with ~900k rows still missing fields, the dated-issues walk took from 15 s
    to 7 minutes on this disk and stalled every request while it ran on the
    event loop (see the loop watchdog).
    """
    conn = db._ro_connect(timeout=30)
    try:
        return _issue_backfill_ids_on(conn, min_year)
    finally:
        conn.close()


def _issue_backfill_ids_on(conn: Any, min_year: int = 0) -> list[int]:
    floor = f"{min_year:04d}" if min_year > 0 else ""
    cur = conn.execute(
        """
        SELECT id FROM cv_issue
        WHERE date_last_updated IS NULL
          AND (backfill_missing IS NULL OR backfill_missing = 0)
          AND store_date IS NOT NULL AND store_date >= ?
        ORDER BY store_date DESC
        LIMIT ?
        """,
        [floor, _ISSUE_BATCH],
    )
    issue_ids = [row[0] for row in cur.fetchall()]

    if len(issue_ids) < _ISSUE_BATCH:
        seen = set(issue_ids)
        cur2 = conn.execute(
            """
            SELECT id FROM cv_issue
            WHERE date_last_updated IS NULL
              AND (backfill_missing IS NULL OR backfill_missing = 0)
              AND """ + backfill_window_sql(min_year) + """
            ORDER BY id DESC
            LIMIT ?
            """,
            [_ISSUE_BATCH - len(issue_ids)],
        )
        for row in cur2.fetchall():
            if row[0] not in seen:
                issue_ids.append(row[0])
                seen.add(row[0])
    return issue_ids


async def _backfill_issues(client: CVClient, db: Database) -> dict[str, Any]:
    """Re-fetch issues that are missing the new detail fields.

    Prioritises issues with a recent store_date, then falls back to newest
    issues first.  IDs that CV confirms as not found are marked so they are
    skipped in subsequent runs.
    """
    return await _backfill_ids(
        client, db, label="Issue backfill", endpoint="issue/", table="cv_issue",
        ids=await asyncio.to_thread(_issue_backfill_ids, db, get_settings().backfill_min_year),
        fetch=client.get_issue,
        store=lambda r: db.upsert_issue(r, commit=False),
    )


async def _backfill_issue_details(client: CVClient, db: Database) -> dict[str, Any]:
    """Fetch /issue/ details for rows stored from list responses (no credits yet).

    List endpoints (release prefetch, incremental sync, volume issue lists)
    store issues without credits.  Newest issues go first since scrapers ask
    about those most.  Shares the backfill hourly budget with the other jobs
    and stops early when CV rate limits.
    """
    return await _backfill_ids(
        client, db, label="Issue detail backfill", endpoint="issue/", table="cv_issue",
        ids=await asyncio.to_thread(
            db.issue_ids_needing_detail, _ISSUE_DETAIL_BATCH, get_settings().backfill_min_year
        ),
        fetch=client.get_issue,
        store=lambda r: db.upsert_issue(r, commit=False),
    )


async def _backfill_publishers(client: CVClient, db: Database) -> dict[str, Any]:
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
    return await _backfill_ids(
        client, db, label="Publisher backfill", endpoint="publisher/", table="cv_publisher",
        ids=[row[0] for row in cur.fetchall()], fetch=client.get_publisher,
        store=lambda r: db.upsert_publisher(r, commit=False),
    )


async def _backfill_search_cache_volumes(client: CVClient, db: Database) -> dict[str, Any]:
    """Fetch volumes referenced in search caches but absent from cv_volume.

    Finding them reads every cached search, so that runs on a worker thread
    with its own connection.
    """
    missing_ids = await asyncio.to_thread(
        db.get_search_cache_missing_volume_ids, _SEARCH_BACKFILL_BATCH
    )
    return await _backfill_ids(
        client, db, label="Search cache volume backfill", endpoint="volume/",
        table="cv_volume", ids=missing_ids, fetch=client.get_volume,
        store=lambda r: db.upsert_volume(r, commit=False),
    )

# ---------------------------------------------------------------------------
# WAL checkpoint
# ---------------------------------------------------------------------------

#: Warn when a checkpoint leaves the WAL bigger than this (a reader is stuck).
_WAL_WARN_BYTES = 256 * 1024 * 1024


async def _checkpoint_wal(db: Database) -> dict[str, Any]:
    """Checkpoint the WAL on a worker thread so the event loop never does the copy."""
    result: dict[str, Any] = dict(await asyncio.to_thread(db.checkpoint_wal))
    incomplete = result["log_frames"] > result["checkpointed"]
    if incomplete and result["wal_bytes"] > _WAL_WARN_BYTES:
        logger.warning(
            "WAL checkpoint incomplete (%d/%d frames, WAL %.0f MB): a long-running "
            "reader is holding an old snapshot",
            result["checkpointed"], result["log_frames"], result["wal_bytes"] / 1_048_576,
        )
        result["stop_reason"] = "wal_reader"
    else:
        logger.info(
            "WAL checkpoint: %d/%d frames, WAL %.0f MB",
            result["checkpointed"], result["log_frames"], result["wal_bytes"] / 1_048_576,
        )
    return result


# ---------------------------------------------------------------------------
# Incremental issue sync  (issues endpoint, date_last_updated watermark)
# ---------------------------------------------------------------------------

#: cv_sync_metadata key holding the incremental issue sync watermark.
ISSUE_SYNC_WATERMARK = "issues_incremental"
#: CV timestamp format (date_last_updated and filter bounds).
_CV_TS = "%Y-%m-%d %H:%M:%S"
#: Re-read this far behind the watermark: edits sharing its second, or saved
#: upstream slightly out of order, are picked up again (upserts are idempotent).
_SYNC_OVERLAP = timedelta(hours=1)
#: CV answered 502 to whole 48h and 14-day windows during its outage, so wide
#: windows are walked in slices and progress is kept per completed slice.
_SYNC_CHUNK = timedelta(hours=24)
#: Upper bound past UTC now, so the window reaches CV's clock (US time) without
#: this code having to know CV's time zone.
_SYNC_FUTURE_PAD = timedelta(days=1)
#: Look-back for the very first run, before any watermark exists.
_SYNC_INITIAL_LOOKBACK = timedelta(hours=72)
#: Slices per scheduled run; a longer backlog continues on the next run.
_SYNC_MAX_CHUNKS = 45


def _store_issue_page(db: Database, results: list[dict[str, Any]]) -> None:
    """Upsert one page of list-endpoint issues and commit.

    Runs on the event loop thread: db.conn is shared, and Python's sqlite3
    connection objects must not be used from two threads at once.  Only work
    that opens its own connection (backup, export, health counts) goes to a
    worker thread.
    """
    for issue in results:
        db.upsert_issue(issue, commit=False)
    db.conn.commit()


async def _sync_issue_window(
    client: CVClient,
    db: Database,
    start: datetime,
    end: datetime,
    *,
    max_chunks: int | None = None,
    outcome: dict[str, Any] | None = None,
) -> tuple[int, str | None, bool]:
    """Upsert issues CV updated between *start* and *end*, oldest first.

    Walks the range in _SYNC_CHUNK slices.  Returns (upserted, latest
    date_last_updated stored, complete).  Stops at the first failed request;
    because pages are sorted ascending, every issue up to *latest* is stored.
    When incomplete, ``outcome["stop_reason"]`` says why.
    """
    upserted = 0
    latest: str | None = None
    chunk_start = start
    chunks = 0
    total_chunks = max(1, int((end - start) / _SYNC_CHUNK + 0.999))
    while chunk_start < end:
        if max_chunks is not None and chunks >= max_chunks:
            if outcome is not None:
                outcome["stop_reason"] = "run_limit"
            return upserted, latest, False
        chunk_end = min(chunk_start + _SYNC_CHUNK, end)
        offset = 0
        while True:
            data = await client.get(
                "issues/",
                {
                    "filter": (
                        f"date_last_updated:{chunk_start.strftime(_CV_TS)}"
                        f"|{chunk_end.strftime(_CV_TS)}"
                    ),
                    "sort": "date_last_updated:asc",
                    "limit": 100,
                    "offset": offset,
                },
            )
            # NOT_FOUND on a list endpoint means this slice has nothing in it
            if data is _CV_NOT_FOUND:
                break
            if not data:
                logger.warning(
                    "Issue sync: upstream request failed for slice %s at offset %d%s",
                    chunk_start.strftime(_CV_TS),
                    offset,
                    " (rate limited)" if data is _CV_RATE_LIMITED else "",
                )
                if outcome is not None:
                    outcome["stop_reason"] = _upstream_failure(data)
                return upserted, latest, False
            results = data.get("results") or []
            if results:
                _store_issue_page(db, results)
                upserted += len(results)
                page_latest = max(str(r.get("date_last_updated") or "") for r in results)
                if page_latest and (latest is None or page_latest > latest):
                    latest = page_latest
            offset += 100
            if offset >= data.get("number_of_total_results", 0) or not results:
                break
        chunks += 1
        chunk_start = chunk_end
        report_progress(chunks, total_chunks, f"{upserted} issues")
    return upserted, latest, True


async def _sync_issues_incremental(client: CVClient, db: Database) -> dict[str, Any]:
    """Pull issues updated since the stored watermark and upsert them.

    The watermark is the newest date_last_updated actually stored, in CV's own
    clock, and it only moves forward over data that was fully written.  A run
    that fails (CV 5xx, rate limit) or never fires leaves it in place, so the
    next run resumes where the last good one stopped instead of leaving a gap.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    mark = db.get_sync_watermark(ISSUE_SYNC_WATERMARK)
    if mark:
        start = datetime.strptime(mark, _CV_TS) - _SYNC_OVERLAP
    else:
        start = now - _SYNC_INITIAL_LOOKBACK
    end = now + _SYNC_FUTURE_PAD
    logger.info(
        "Incremental sync: issues updated since %s (watermark %s)",
        start.strftime(_CV_TS),
        mark or "none",
    )
    outcome: dict[str, Any] = {}
    upserted, latest, complete = await _sync_issue_window(
        client, db, start, end, max_chunks=_SYNC_MAX_CHUNKS, outcome=outcome
    )
    if latest and (mark is None or latest > mark):
        db.set_sync_watermark(ISSUE_SYNC_WATERMARK, latest)
        mark = latest
    logger.info(
        "Incremental sync: upserted %d issues, watermark %s%s",
        upserted,
        mark or "none",
        "" if complete else " (incomplete; next run resumes from watermark)",
    )
    result: dict[str, Any] = {"issues_upserted": upserted, "watermark": mark, "complete": complete}
    if not complete:
        result["stop_reason"] = outcome.get("stop_reason", "upstream_error")
    return result


async def _sync_recent_issues(client: CVClient, db: Database, hours: int = 48) -> dict[str, Any]:
    """Pull issues updated in the last *hours* hours and upsert them (on demand).

    Keeps the local cache fresh for recently-edited CV entries
    (description added, store_date corrected, etc.).  Used for wide
    re-syncs; it does not move the scheduled sync watermark.  Issues
    previously marked missing by a backfill run have the flag cleared when a
    list row for them is stored.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    start = now - timedelta(hours=hours)
    logger.info(
        "Issue re-sync: issues updated %s to %s (window: %dh)",
        start.strftime(_CV_TS),
        now.strftime(_CV_TS),
        hours,
    )
    outcome: dict[str, Any] = {}
    upserted, _latest, complete = await _sync_issue_window(
        client, db, start, now + _SYNC_FUTURE_PAD, outcome=outcome
    )
    logger.info(
        "Issue re-sync: upserted %d issues%s", upserted, "" if complete else " (incomplete)"
    )
    result: dict[str, Any] = {
        "window_hours": hours, "issues_upserted": upserted, "complete": complete,
    }
    if not complete:
        result["stop_reason"] = outcome.get("stop_reason", "upstream_error")
    return result


# Public aliases: let app.py (and tests) call these without the private names.
sync_recent_issues = _sync_recent_issues
sync_issues_incremental = _sync_issues_incremental

# ---------------------------------------------------------------------------
# Stale entity eviction
# ---------------------------------------------------------------------------


def _eviction_disabled(settings: Settings) -> bool:
    return (
        settings.evict_older_than_years <= 0
        and settings.response_cache_ttl_days <= 0
        and settings.search_cache_ttl_days <= 0
    )


async def _evict_stale_data(
    db: Database, settings: Settings, *, dry_run: bool = False
) -> dict[str, Any]:
    """Remove stale entities and expired response cache entries.

    Issues:         deleted if cover_date < cutoff AND last_accessed older than access window
    Volumes:        cascade-deleted if no remaining issues AND last_accessed stale
    Response cache: deleted if cached_at older than response_cache_ttl_days
    All operations are no-ops when the relevant setting is 0 (disabled).
    With *dry_run* only the candidates are counted.
    """
    from datetime import date as _date

    if _eviction_disabled(settings):
        note = (
            "Eviction disabled: set EVICT_OLDER_THAN_YEARS, RESPONSE_CACHE_TTL_DAYS"
            " or SEARCH_CACHE_TTL_DAYS"
        )
        if dry_run:
            return {"dry_run": True, "issues": 0, "volumes": 0, "response_cache": 0, "note": note}
        return {"issues_deleted": 0, "volumes_deleted": 0, "cache_deleted": 0,
                "note": note, "stop_reason": "disabled"}

    access_expiry = (
        datetime.now(UTC) - timedelta(days=settings.evict_unaccessed_days)
    ).strftime("%Y-%m-%d %H:%M:%S")
    cutoff = (
        (_date.today() - timedelta(days=settings.evict_older_than_years * 365)).isoformat()
        if settings.evict_older_than_years > 0 else None
    )

    if dry_run:
        counts: dict[str, Any] = {"dry_run": True}
        if cutoff is not None:
            counts.update(db.count_eviction_candidates(cutoff, access_expiry))
        else:
            counts.update({"issues": 0, "volumes": 0})
        if settings.response_cache_ttl_days > 0:
            cache_cutoff = (
                datetime.now(UTC) - timedelta(days=settings.response_cache_ttl_days)
            ).strftime("%Y-%m-%d %H:%M:%S")
            counts["response_cache"] = db.count_response_cache_before(cache_cutoff)
        else:
            counts["response_cache"] = 0
        logger.info("Eviction dry run: %s", counts)
        return counts

    issues_deleted = vol_deleted = cache_deleted = 0
    if cutoff is not None:
        issues_deleted = db.evict_stale_issues(cutoff, access_expiry)
        vol_deleted = db.evict_orphaned_volumes(access_expiry)
        logger.info("Eviction: removed %d issues, %d orphaned volumes", issues_deleted, vol_deleted)

    if settings.response_cache_ttl_days > 0 or settings.search_cache_ttl_days > 0:
        cache_cutoff = (
            (datetime.now(UTC) - timedelta(days=settings.response_cache_ttl_days))
            .strftime("%Y-%m-%dT%H:%M:%S")
            if settings.response_cache_ttl_days > 0
            else "1900-01-01T00:00:00"
        )
        search_cutoff = (
            (datetime.now(UTC) - timedelta(days=settings.search_cache_ttl_days))
            .strftime("%Y-%m-%dT%H:%M:%S")
            if settings.search_cache_ttl_days > 0
            else None
        )
        cache_deleted = db.evict_response_cache(cache_cutoff, search_cutoff_date=search_cutoff)
        logger.info("Eviction: removed %d response cache entries", cache_deleted)

    result: dict[str, Any] = {
        "dry_run": False,
        "issues_deleted": issues_deleted,
        "volumes_deleted": vol_deleted,
        "cache_deleted": cache_deleted,
    }
    if not issues_deleted and not vol_deleted and not cache_deleted:
        result["stop_reason"] = "nothing_to_do"
    return result


# Public alias for app.py admin endpoint
evict_stale_data = _evict_stale_data


# ---------------------------------------------------------------------------
# Backup and exports
# ---------------------------------------------------------------------------


async def _backup_database(db: Database, settings: Settings) -> dict[str, Any]:
    """Snapshot the database into backup_dir, keeping the newest backup_keep copies.

    VACUUM INTO runs on a worker thread with its own connection, so requests
    and the other jobs keep using the main connection meanwhile.
    """
    if settings.backup_keep <= 0:
        return {"backup": None, "removed": 0, "stop_reason": "disabled"}
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    name = f"localcv-{datetime.now(UTC):%Y%m%d-%H%M%S}.db"
    await asyncio.to_thread(db.backup_to, settings.backup_dir / name)

    backups = sorted(
        settings.backup_dir.glob("localcv-*.db"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    stale = backups[settings.backup_keep:]
    for path in stale:
        path.unlink()
    logger.info("Database backup written: %s (removed %d old)", name, len(stale))
    return {"backup": name, "removed": len(stale)}


async def _export_comicvine_db(db: Database, settings: Settings) -> dict[str, Any]:
    """Publish a ComicManager-compatible comicvine.db at cv_db_export_path.

    Runs on a worker thread with its own connection, like the backup.  The
    file is built next to the live database (local disk) and then moved.
    """
    dest = settings.cv_db_export_path
    if dest is None:
        return {"path": None, "stop_reason": "disabled"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    counts = await asyncio.to_thread(
        db.export_comicvine_db, dest, staging_dir=settings.db_path.parent
    )
    logger.info("comicvine.db export written to %s: %s", dest, counts)
    return {"path": str(dest), **counts}


async def _refresh_mcl(settings: Settings) -> dict[str, Any]:
    """Download the newest community latest.mcl to mcl_export_path."""
    if settings.mcl_export_path is None:
        return {"updated": False, "stop_reason": "disabled"}
    settings.mcl_export_path.parent.mkdir(parents=True, exist_ok=True)
    result = dict(await refresh_mcl(settings.mcl_source_url, settings.mcl_export_path))
    if not result.get("updated"):
        logger.info("latest.mcl unchanged (%s)", result.get("source"))
        result["stop_reason"] = "nothing_to_do"
    return result


#: IDs per list request when filling gaps (ComicVine's maximum page size).
_MCL_ID_BATCH = 100


async def _fetch_ids_in_batches(
    client: CVClient,
    db: Database,
    endpoint: str,
    ids: list[int],
    store: Any,
    label: str = "MCL gap backfill",
) -> tuple[int, str | None]:
    """Fetch *ids* from a list endpoint with ``filter=id:a|b|...``.

    Returns (rows stored, stop reason or None when every batch was asked).
    """
    stored = 0
    for start in range(0, len(ids), _MCL_ID_BATCH):
        stop = _budget_stop(client, db, endpoint, label)
        if stop:
            return stored, stop
        batch = ids[start:start + _MCL_ID_BATCH]
        sleep_secs = _backfill_call_interval(client, endpoint)
        data = await client.get(
            endpoint,
            {"filter": "id:" + "|".join(str(i) for i in batch), "limit": _MCL_ID_BATCH},
        )
        if data is _CV_RATE_LIMITED:
            logger.info("%s: rate limited by CV, stopping this run", label)
            return stored, "rate_limited"
        if data:  # NOT_FOUND (no rows) and failures are falsy
            stored += store(data.get("results") or [])
        report_progress(min(start + _MCL_ID_BATCH, len(ids)), len(ids), f"/{endpoint}")
        await asyncio.sleep(sleep_secs)
    return stored, None


async def _backfill_from_mcl(
    client: CVClient, db: Database, settings: Settings
) -> dict[str, Any]:
    """Fetch volumes and issues the community MCL lists but cvproxy doesn't have.

    The MCL maps every ComicVine volume to its full issue list, so the gaps
    can be filled 100 IDs per list request (newest IDs first) rather than one
    request per item.  Issues arrive list-shaped; the issue detail backfill
    adds their credits later.  The ID sets come from a read-only connection
    on a worker thread (reading 1M+ IDs on the loop stalled requests).
    """
    result: dict[str, Any] = {
        "volumes_missing": 0, "volumes_added": 0, "issues_missing": 0, "issues_added": 0,
    }
    path = settings.mcl_export_path
    if path is None:
        result["stop_reason"] = "disabled"
        return result
    if not path.exists():
        result["note"] = "latest.mcl has not been downloaded yet"
        result["stop_reason"] = "nothing_to_do"
        return result
    mcl = await asyncio.to_thread(read_mcl, path)

    def store_volumes(rows: list[dict[str, Any]]) -> int:
        for row in rows:
            db.upsert_volume(row, commit=False)
        db.conn.commit()
        return len(rows)

    local_volumes = await asyncio.to_thread(db.existing_ids_snapshot, "cv_volume")
    missing_volumes = sorted(set(mcl) - local_volumes, reverse=True)
    result["volumes_missing"] = len(missing_volumes)
    result["volumes_added"], stop = await _fetch_ids_in_batches(
        client, db, "volumes/", missing_volumes, store_volumes
    )

    local_issues = await asyncio.to_thread(db.existing_ids_snapshot, "cv_issue")
    missing_issues = sorted(
        {iid for ids in mcl.values() for iid in ids} - local_issues, reverse=True
    )
    result["issues_missing"] = len(missing_issues)
    if stop != "rate_limited":  # after a refusal every endpoint waits out the cooldown
        result["issues_added"], issue_stop = await _fetch_ids_in_batches(
            client, db, "issues/", missing_issues, db.insert_new_issues
        )
        stop = stop or issue_stop
    if stop:
        result["stop_reason"] = stop
    elif not missing_volumes and not missing_issues:
        result["stop_reason"] = "nothing_to_do"
    logger.info("MCL gap backfill: %s", result)
    return result


async def _backfill_cover_hashes(db: Database, settings: Settings) -> dict[str, Any]:
    """Hash up to cover_hash_per_hour cover images of issues missing from comic_covers."""
    if settings.cover_hash_per_hour <= 0:
        return {"hashed": 0, "failed": 0, "issues": 0, "stop_reason": "disabled"}
    return dict(await hash_covers(
        db, limit=settings.cover_hash_per_hour, min_year=settings.backfill_min_year
    ))

# ---------------------------------------------------------------------------
# Repairs
# ---------------------------------------------------------------------------


async def _repair_publishers(
    client: CVClient, db: Database, *, batch_size: int = 100, calendar_only: bool = True
) -> dict[str, Any]:
    """Re-sync canonical ComicVine publisher IDs onto cached volumes.

    Databases created by older external tools numbered publishers themselves;
    this batch-fetches ``id,publisher`` from /volumes/ and writes the real
    IDs back.  Runs on the loop thread (shared connection).
    """
    fetch_ids = db.get_calendar_volume_ids if calendar_only else db.get_all_volume_ids
    volume_ids = fetch_ids()
    total = len(volume_ids)
    logger.info(
        "Publisher repair: %d volumes queued (batch_size=%d, calendar_only=%s)",
        total, batch_size, calendar_only,
    )
    volumes_updated = errors = 0
    publishers_seen: set[int] = set()
    stop: str | None = None
    for i in range(0, total, batch_size):
        chunk = volume_ids[i:i + batch_size]
        data = await client.get(
            "volumes/",
            {
                "filter": "id:" + "|".join(str(v) for v in chunk),
                "field_list": "id,publisher",
                "limit": batch_size,
                "offset": 0,
            },
        )
        if data is _CV_RATE_LIMITED:
            logger.info("Publisher repair: rate limited by CV after %d volumes, stopping", i)
            stop = "rate_limited"
            break
        if not data:
            logger.warning("Publisher repair: upstream failed for batch %d-%d", i, i + len(chunk))
            errors += len(chunk)
            continue
        for vol in data.get("results") or []:
            vid = vol.get("id")
            pub = vol.get("publisher") or {}
            pid = pub.get("id")
            if not vid or not pid:
                continue
            try:
                db.upsert_publisher(pub)
                db.update_volume_publisher(vid, pid)
                volumes_updated += 1
                publishers_seen.add(pid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Publisher repair: volume %s: %s", vid, exc)
                errors += 1
        report_progress(min(i + batch_size, total), total)
    logger.info(
        "Publisher repair done: %d updated, %d publishers, %d errors",
        volumes_updated, len(publishers_seen), errors,
    )
    result: dict[str, Any] = {
        "volumes_total": total,
        "volumes_updated": volumes_updated,
        "publishers_updated": len(publishers_seen),
        "errors": errors,
    }
    if stop:
        result["stop_reason"] = stop
    elif not total:
        result["stop_reason"] = "nothing_to_do"
    return result


async def _rebuild_fts(db: Database) -> dict[str, Any]:
    """Rebuild the FTS indexes on a worker thread with a connection of its own."""
    result = await asyncio.to_thread(db.rebuild_fts)
    logger.info("FTS rebuild complete: %s", result)
    return dict(result)


# ---------------------------------------------------------------------------
# Audits and snapshots (read-only connections on worker threads)
# ---------------------------------------------------------------------------


async def _queue_snapshot(db: Database) -> dict[str, Any]:
    result = await asyncio.to_thread(db.queue_snapshot, get_settings().backfill_min_year)
    logger.info("Backlog snapshot: %s", {k: v for k, v in result.items() if k != "timings_s"})
    return result


async def _integrity_check(db: Database) -> dict[str, Any]:
    result = dict(await asyncio.to_thread(db.quick_check))
    if result["ok"]:
        logger.info("Integrity check: ok")
    else:
        logger.error("Integrity check found problems: %s", result["problems"])
        result["stop_reason"] = "check_failed"
    return result


async def _audit_response_cache(db: Database, *, purge: bool = False) -> dict[str, Any]:
    result = dict(await asyncio.to_thread(db.audit_response_cache, purge=purge))
    result["purge"] = purge
    logger.info(
        "Response cache audit: scanned %d, %d suspicious (%d purgeable), deleted %d; %s",
        result["scanned"], result["problems"], result["purgeable"], result["deleted"],
        result["by_type"] or "nothing found",
    )
    if result["purgeable"] and not purge:
        result["stop_reason"] = "needs_attention"
    return result


# ---------------------------------------------------------------------------
# Re-fetch specific issues
# ---------------------------------------------------------------------------

#: /issue/ detail fetches allowed per manual re-fetch.
_REFETCH_DETAIL_MAX = 100


async def _refetch_issues(
    client: CVClient,
    db: Database,
    ids: list[int],
    volume_id: int = 0,
    *,
    details: bool = False,
) -> dict[str, Any]:
    """Re-fetch issues by ID (100 per /issues/ call) and/or a whole volume's issues.

    List rows only update list fields (credits survive, and a newer
    date_last_updated flags the details for the detail backfill).  With
    *details*, up to _REFETCH_DETAIL_MAX of the issues are also fetched from
    /issue/ right away.
    """
    label = "Issue re-fetch"
    result: dict[str, Any] = {"requested": len(ids), "fetched": 0, "not_on_cv": 0}
    if volume_id:
        result.update({"volume_id": volume_id, "volume_issues": 0})
    if not ids and not volume_id:
        raise ValueError("Give issue IDs, a volume ID, or both")
    stop: str | None = None
    seen: list[int] = []

    for start in range(0, len(ids), _MCL_ID_BATCH):
        stop = _budget_stop(client, db, "issues/", label)
        if stop:
            break
        batch = ids[start:start + _MCL_ID_BATCH]
        data = await client.get(
            "issues/",
            {"filter": "id:" + "|".join(str(i) for i in batch), "limit": _MCL_ID_BATCH},
        )
        if data is _CV_NOT_FOUND:
            rows: list[dict[str, Any]] = []
        elif not data:
            stop = _upstream_failure(data)
            break
        else:
            rows = data.get("results") or []
        _store_issue_page(db, rows)
        found = {r.get("id") for r in rows}
        seen.extend(i for i in batch if i in found)
        result["fetched"] += len(rows)
        result["not_on_cv"] += len(batch) - len(rows)
        report_progress(min(start + _MCL_ID_BATCH, len(ids)), len(ids), "by ID")
        await asyncio.sleep(_backfill_call_interval(client, "issues/"))

    if volume_id and not stop:
        offset = 0
        while True:
            stop = _budget_stop(client, db, "issues/", label)
            if stop:
                break
            data = await client.get(
                "issues/",
                {"filter": f"volume:{volume_id}", "sort": "id:asc", "limit": 100,
                 "offset": offset},
            )
            if data is _CV_NOT_FOUND:
                break
            if not data:
                stop = _upstream_failure(data)
                break
            rows = data.get("results") or []
            _store_issue_page(db, rows)
            seen.extend(r["id"] for r in rows if r.get("id"))
            result["volume_issues"] += len(rows)
            total = data.get("number_of_total_results", 0)
            offset += 100
            report_progress(min(offset, total), total, f"volume {volume_id}")
            if offset >= total or not rows:
                break
            await asyncio.sleep(_backfill_call_interval(client, "issues/"))

    if details and not stop:
        targets = list(dict.fromkeys(seen))[:_REFETCH_DETAIL_MAX]
        result["details_requested"] = len(targets)
        detail = await _backfill_ids(
            client, db, label=label + " (details)", endpoint="issue/", table="cv_issue",
            ids=targets, fetch=client.get_issue,
            store=lambda r: db.upsert_issue(r, commit=False),
        )
        result["details_fetched"] = detail["updated"]
        if detail.get("stop_reason") not in (None, "nothing_to_do"):
            stop = detail["stop_reason"]
    if stop:
        result["stop_reason"] = stop
    logger.info("%s: %s", label, result)
    return result
