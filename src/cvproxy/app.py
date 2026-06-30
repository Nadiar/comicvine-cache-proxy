"""FastAPI application factory and lifespan management."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from cvproxy.config import get_settings
from cvproxy.cv_client import CVClient
from cvproxy.dashboard import DASHBOARD_HTML
from cvproxy.db import Database
from cvproxy.image_cache import ImageCache
from cvproxy.routes.api import router as api_router
from cvproxy.scheduler import create_scheduler, sync_recent_issues, evict_stale_data, get_scheduler_log
from cvproxy.stats import StatsTracker

logger = logging.getLogger(__name__)


class TrailingSlashMiddleware:
    """ASGI middleware that normalises request paths to include a trailing slash.

    This prevents FastAPI's redirect_slashes from issuing 307 redirects,
    which break when the app runs behind a TLS-terminating reverse proxy
    (the redirect URL gets generated with http:// instead of https://).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            # Only normalise /api/ paths (where routes expect trailing slashes)
            if path.startswith("/api/") and not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
                scope["path"] = path + "/"
        await self.app(scope, receive, send)


class XmlFormatMiddleware:
    """ASGI middleware that converts JSON responses to CV-compatible XML.

    When a request to /api/ includes ``format=xml`` in the query string,
    this middleware intercepts the JSON response body and re-serialises it
    as XML matching the Comic Vine XML API format.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        qs = parse_qs(scope.get("query_string", b"").decode())
        if qs.get("format", ["json"])[0] != "xml":
            await self.app(scope, receive, send)
            return

        # Collect the response body, then convert JSON -> XML.
        response_body = []
        response_started = False
        original_headers = []
        original_status = 200

        async def capture_send(message):
            nonlocal response_started, original_status
            if message["type"] == "http.response.start":
                response_started = True
                original_status = message.get("status", 200)
                original_headers[:] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                response_body.append(message.get("body", b""))

        await self.app(scope, receive, capture_send)

        body = b"".join(response_body)
        try:
            data = json.loads(body)
            # Determine resource type from URL path so list items inside
            # <results> get the correct tag (e.g. <volume>, <issue>).
            resource_type = _resource_type_from_path(scope["path"])
            xml_body = '<?xml version="1.0" encoding="utf-8"?>'
            xml_body += "<response>" + _dict_to_cv_xml(data, resource_type=resource_type) + "</response>"
            body = xml_body.encode("utf-8")
            content_type = b"application/xml; charset=utf-8"
        except (json.JSONDecodeError, TypeError):
            # Not JSON — pass through unchanged.
            content_type = None

        # Build new headers, replacing content-type and content-length.
        headers = []
        for k, v in original_headers:
            name = k.lower() if isinstance(k, bytes) else k.encode().lower()
            if name == b"content-type" and content_type:
                headers.append((b"content-type", content_type))
            elif name == b"content-length":
                headers.append((b"content-length", str(len(body)).encode()))
            else:
                headers.append((k, v))

        await send({"type": "http.response.start", "status": original_status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _resource_type_from_path(path: str) -> str:
    """Extract the CV resource type from an API path for XML list-item tags.

    /api/search/       -> "volume"  (search always returns volumes)
    /api/volumes/      -> "volume"
    /api/volume/4050-X -> "volume"  (single, but doesn't hurt)
    /api/issues/       -> "issue"
    /api/issue/4000-X  -> "issue"
    /api/publishers/   -> "publisher"
    """
    # Strip /api/ prefix and trailing slash, then take first segment.
    trimmed = path.removeprefix("/api/").strip("/").split("/")[0]
    if trimmed == "search":
        return "volume"
    # Normalise plural to singular: "volumes" -> "volume", "issues" -> "issue"
    if trimmed.endswith("s"):
        return trimmed[:-1]
    return trimmed


# Irregular plural → singular mappings for CV XML list items.
_XML_SINGULARS: dict[str, str] = {
    "people": "person",
}


def _dict_to_cv_xml(obj: Any, tag: str = "", resource_type: str = "") -> str:
    """Recursively convert a Python value to CV-compatible XML.

    *resource_type* is used as the item tag for list items inside the
    ``<results>`` element, matching the real Comic Vine XML format where
    e.g. ``<results><volume>...</volume></results>``.
    """
    if isinstance(obj, dict):
        inner = "".join(_dict_to_cv_xml(v, k, resource_type) for k, v in obj.items())
        return f"<{tag}>{inner}</{tag}>" if tag else inner
    if isinstance(obj, list):
        if not obj:
            return f"<{tag}/>" if tag else ""
        if tag == "results" and resource_type:
            # Top-level results list: use resource type tag for each item.
            item_tag = resource_type
        elif tag in _XML_SINGULARS:
            # Irregular plurals (e.g. people → person).
            item_tag = _XML_SINGULARS[tag]
        elif tag.endswith("s"):
            # Nested lists like character_credits, person_credits, etc.
            item_tag = tag[:-1]
        else:
            item_tag = tag
        items = "".join(_dict_to_cv_xml(item, item_tag, resource_type) for item in obj)
        return f"<{tag}>{items}</{tag}>" if tag else items
    if obj is None:
        return f"<{tag}/>" if tag else ""
    if isinstance(obj, bool):
        val = "true" if obj else "false"
        return f"<{tag}>{val}</{tag}>" if tag else val
    if isinstance(obj, (int, float)):
        return f"<{tag}>{obj}</{tag}>" if tag else str(obj)
    # String -- wrap in CDATA like the real CV API.
    s = str(obj)
    return f"<{tag}><![CDATA[{s}]]></{tag}>" if tag else s


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Start-up / shut-down lifecycle for the application."""
    settings = get_settings()

    # Database
    db = Database(settings.db_path, cutoff_year=settings.cache_cutoff_year)
    db.connect()
    app.state.db = db
    counts = db.get_counts()
    logger.info(
        "Database ready: %d volumes, %d issues, %d publishers",
        counts["volume"],
        counts["issue"],
        counts["publisher"],
    )

    # CV API client
    client = CVClient(
        api_key=settings.cv_api_key,
        base_url=settings.cv_api_base_url,
        rate_limit_per_minute=settings.rate_limit_per_minute,
        rate_limit_per_hour_per_endpoint=settings.rate_limit_per_hour_per_endpoint,
    )
    app.state.cv_client = client

    # Image cache
    cache = ImageCache(
        cache_dir=settings.image_cache_dir,
        ttl_days=settings.image_cache_ttl_days,
    )
    cache.open()
    app.state.image_cache = cache
    cache_stats = cache.stats()
    logger.info(
        "Image cache ready: %d images, %.1f MB",
        cache_stats["images_cached"],
        cache_stats["cache_size_bytes"] / 1_048_576,
    )

    # Stats tracker
    stats = StatsTracker(settings.stats_db_path)
    stats.open()
    app.state.stats = stats
    logger.info("Stats tracker ready: %s", settings.stats_db_path)

    # Background scheduler
    scheduler = None
    if settings.sync_enabled:
        scheduler = create_scheduler(settings, cache, db, client)
        scheduler.start()
        logger.info("Scheduler started")

    yield

    # Shutdown
    if scheduler:
        scheduler.shutdown(wait=False)
    await client.close()
    cache.close()
    stats.close()
    db.close()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="CVProxy",
        description="ComicVine API proxy with local SQLite cache",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Convert JSON responses to CV-compatible XML when format=xml is requested.
    app.add_middleware(XmlFormatMiddleware)

    # Normalise paths so clients without trailing slashes don't trigger
    # broken 307 redirects when behind a TLS-terminating reverse proxy.
    app.add_middleware(TrailingSlashMiddleware)

    # Health check at root level (not under /api)
    @app.get("/health")
    async def health() -> dict[str, Any]:
        db: Database = app.state.db
        cache: ImageCache = app.state.image_cache
        return {
            "status": "ok",
            "database": db.get_counts(),
            "image_cache": cache.stats(),
        }

    # Dashboard — pretty HTML UI
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page() -> str:
        return DASHBOARD_HTML

    # Dashboard — JSON data API (consumed by the HTML dashboard)
    @app.get("/dashboard/data")
    def dashboard_data(
        hours: float | None = Query(
            default=None,
            description="Filter to last N hours (omit for all-time)",
        ),
    ) -> dict[str, Any]:
        stats: StatsTracker = app.state.stats
        cv_client: CVClient = app.state.cv_client
        data = stats.summary(since_hours=hours)
        usage = cv_client.get_hourly_usage()
        data["cv_quota"] = usage
        data["cv_quota_limit"] = cv_client.hourly_limit
        data["cv_quota_total_used"] = cv_client.total_hourly_calls()
        data["cv_quota_clock_hour_reset_at"] = cv_client.next_clock_hour_reset()
        cfg = get_settings()
        data["eviction_settings"] = {
            "evict_older_than_years": cfg.evict_older_than_years,
            "evict_unaccessed_days": cfg.evict_unaccessed_days,
            "response_cache_ttl_days": cfg.response_cache_ttl_days,
            "search_cache_ttl_days": cfg.search_cache_ttl_days,
            "cache_cutoff_year": cfg.cache_cutoff_year,
        }
        return data

    # Custom 404 handler for /api/ routes
    @app.exception_handler(404)
    async def _api_not_found(request: Request, exc: Exception) -> JSONResponse:
        """Return CV error envelope for /api/ routes; standard 404 otherwise."""
        if str(request.url.path).startswith("/api/"):
            return JSONResponse(
                content={"status_code": 101, "error": "Object Not Found", "results": None},
                status_code=200,
            )
        return JSONResponse(content={"detail": "Not Found"}, status_code=404)

    # Background job registry — keyed by job_id (uuid hex).
    # Each entry: {"status": "running"|"done"|"error", "result": {...}, "started_at": str}
    app.state.jobs: dict[str, dict[str, Any]] = {}

    _MAX_JOBS = 200

    def _trim_jobs() -> None:
        """Keep only the most recent _MAX_JOBS entries; drop the oldest finished ones."""
        jobs = app.state.jobs
        if len(jobs) <= _MAX_JOBS:
            return
        finished = sorted(
            ((jid, job) for jid, job in jobs.items() if job["status"] != "running"),
            key=lambda x: x[1]["started_at"],
        )
        for jid, _ in finished[: len(jobs) - _MAX_JOBS]:
            del jobs[jid]

    def _start_job(name: str, params: dict[str, Any], coro: Any) -> str:
        """Register a job, start it as a background asyncio task, return its ID."""
        _trim_jobs()
        job_id = uuid.uuid4().hex
        task_holder: dict[str, Any] = {}
        app.state.jobs[job_id] = {
            "name": name,
            "params": params,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "result": None,
            "_task": task_holder,  # holds the asyncio.Task for cancellation
        }

        async def _wrap() -> None:
            try:
                result = await coro
                app.state.jobs[job_id]["status"] = "done"
                app.state.jobs[job_id]["result"] = result
            except asyncio.CancelledError:
                app.state.jobs[job_id]["status"] = "cancelled"
                app.state.jobs[job_id]["result"] = None
            except Exception as exc:  # noqa: BLE001
                logger.exception("Background job %s failed", job_id)
                app.state.jobs[job_id]["status"] = "error"
                app.state.jobs[job_id]["result"] = {"error": str(exc)}
            finally:
                app.state.jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()

        task = asyncio.create_task(_wrap())
        task_holder["task"] = task
        return job_id

    def _public_job(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        """Return a job dict safe to serialise (strip internal _task ref)."""
        return {k: v for k, v in {**job, "job_id": job_id}.items() if not k.startswith("_")}

    # Admin — list all jobs
    @app.get("/admin/jobs")
    def admin_jobs_list() -> list[dict[str, Any]]:
        """Return all background jobs, most recent first."""
        jobs = app.state.jobs
        return [
            _public_job(jid, job)
            for jid, job in sorted(jobs.items(), key=lambda x: x[1]["started_at"], reverse=True)
        ]

    # Admin — poll single job status
    @app.get("/admin/jobs/{job_id}")
    def admin_job_status(job_id: str) -> dict[str, Any]:
        """Return the current status of a background admin job."""
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return _public_job(job_id, job)

    # Admin — cancel a running job
    @app.delete("/admin/jobs/{job_id}")
    def admin_job_cancel(job_id: str) -> dict[str, Any]:
        """Cancel a running background job."""
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job["status"] != "running":
            return _public_job(job_id, job)
        task = job.get("_task", {}).get("task")
        if task and not task.done():
            task.cancel()
        return _public_job(job_id, job)

    # Admin — one-off sync trigger
    @app.post("/admin/sync/issues")
    async def admin_sync_issues(
        days: float = Query(
            default=14.0,
            description="Re-sync issues whose date_last_updated is within this many days.",
            ge=0.1,
            le=365,
        ),
    ) -> dict[str, Any]:
        """Trigger an on-demand incremental issue sync for a wider time window.

        Returns immediately with a job_id; poll GET /admin/jobs/{job_id} for status.
        Useful for refreshing stale cached records corrected on ComicVine more than
        48 hours ago (outside the daily scheduler window).
        """
        hours = int(days * 24)
        client = app.state.cv_client
        db: Database = app.state.db
        logger.info("Admin sync triggered: last %.1f days (%d hours)", days, hours)

        async def _run() -> dict[str, Any]:
            await sync_recent_issues(client, db, hours)
            return {"window_hours": hours}

        job_id = _start_job("Issue sync", {"days": days, "window_hours": hours}, _run())
        return {"status": "started", "job_id": job_id, "window_hours": hours}

    # Admin — publisher ID repair
    @app.post("/admin/repair/publishers")
    async def admin_repair_publishers(
        batch_size: int = Query(
            default=100,
            description="Number of volumes to fetch per upstream API call.",
            ge=10,
            le=100,
        ),
        calendar_only: bool = Query(
            default=True,
            description="Only repair volumes referenced by calendar issues (store_date IS NOT NULL). Much faster than repairing all 150k+ volumes.",
        ),
    ) -> dict[str, Any]:
        """Re-sync publisher IDs for locally cached volumes.

        Returns immediately with a job_id; poll GET /admin/jobs/{job_id} for status.

        When localcv.db was originally created by an external tool (e.g. an
        older sqlite_cv_updater.py) it may use sequential auto-increment
        publisher IDs instead of ComicVine's canonical IDs.  This endpoint
        batch-fetches ``id,publisher`` from the CV API for the selected volumes
        and writes the correct CV publisher IDs back into
        ``cv_volume.publisher_id`` and ``cv_publisher``.

        Set ``calendar_only=false`` to repair all 150k+ volumes (slow, burns
        most of the hourly CV API quota).
        """
        db: Database = app.state.db
        cv_client = app.state.cv_client

        async def _run() -> dict[str, Any]:
            fetch_fn = db.get_calendar_volume_ids if calendar_only else db.get_all_volume_ids
            volume_ids = await asyncio.to_thread(fetch_fn)
            total = len(volume_ids)
            logger.info(
                "Publisher repair: %d volumes queued (batch_size=%d, calendar_only=%s)",
                total, batch_size, calendar_only,
            )
            volumes_updated = 0
            publishers_seen: set[int] = set()
            errors = 0

            for i in range(0, total, batch_size):
                chunk = volume_ids[i : i + batch_size]
                id_filter = "|".join(str(v) for v in chunk)

                data = await cv_client.get(
                    "volumes/",
                    {
                        "filter": f"id:{id_filter}",
                        "field_list": "id,publisher",
                        "limit": batch_size,
                        "offset": 0,
                    },
                )
                if not data:
                    logger.warning(
                        "Publisher repair: upstream failed for batch %d-%d", i, i + len(chunk)
                    )
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

                logger.info(
                    "Publisher repair: %d/%d volumes processed",
                    min(i + batch_size, total),
                    total,
                )

            logger.info(
                "Publisher repair done: %d updated, %d publishers, %d errors",
                volumes_updated, len(publishers_seen), errors,
            )
            return {
                "volumes_total": total,
                "volumes_updated": volumes_updated,
                "publishers_updated": len(publishers_seen),
                "errors": errors,
            }

        job_id = _start_job(
            "Publisher repair",
            {"batch_size": batch_size, "calendar_only": calendar_only},
            _run(),
        )
        return {"status": "started", "job_id": job_id, "calendar_only": calendar_only}

    # Admin — cache eviction trigger
    @app.post("/admin/evict")
    async def admin_evict(
        dry_run: bool = Query(
            default=True,
            description="Preview candidate counts without deleting (safe default). Set false to evict.",
        ),
    ) -> dict[str, Any]:
        """Trigger cache eviction based on current eviction settings.

        Returns immediately with job_id; poll GET /admin/jobs/{job_id} for result.
        dry_run=true (default) returns counts of what would be removed without deleting.
        Eviction has no effect when evict_older_than_years=0 and response_cache_ttl_days=0.
        """
        db: Database = app.state.db
        settings = get_settings()

        async def _run() -> dict[str, Any]:
            if settings.evict_older_than_years <= 0 and settings.response_cache_ttl_days <= 0:
                return {
                    "dry_run": dry_run,
                    "issues": 0,
                    "volumes": 0,
                    "response_cache": 0,
                    "note": "Eviction disabled — set EVICT_OLDER_THAN_YEARS or RESPONSE_CACHE_TTL_DAYS",
                }

            access_expiry = (
                datetime.now(timezone.utc) - timedelta(days=settings.evict_unaccessed_days)
            ).strftime("%Y-%m-%d %H:%M:%S")

            if dry_run:
                counts: dict[str, Any] = {"dry_run": True}
                if settings.evict_older_than_years > 0:
                    cutoff = (
                        date.today() - timedelta(days=settings.evict_older_than_years * 365)
                    ).isoformat()
                    counts.update(db.count_eviction_candidates(cutoff, access_expiry))
                else:
                    counts.update({"issues": 0, "volumes": 0})
                if settings.response_cache_ttl_days > 0:
                    cache_cutoff = (
                        datetime.now(timezone.utc) - timedelta(days=settings.response_cache_ttl_days)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    counts["response_cache"] = db.count_response_cache_before(cache_cutoff)
                else:
                    counts["response_cache"] = 0
                return counts

            result = await evict_stale_data(db, settings)
            return {"dry_run": False, **(result or {})}

        job_id = _start_job("Cache eviction", {"dry_run": dry_run}, _run())
        return {"status": "started", "job_id": job_id, "dry_run": dry_run}

    # Admin — FTS index rebuild
    @app.post("/admin/rebuild/fts")
    async def admin_rebuild_fts() -> dict[str, Any]:
        """Rebuild all FTS indexes (volume_fts, issue_fts, publisher_fts).

        Returns immediately with a job_id; poll GET /admin/jobs/{job_id} for status.
        Only needed if an index becomes out of sync (e.g. after a manual DB import).
        Under normal operation the triggers installed at startup keep all indexes current.
        """
        db: Database = app.state.db

        async def _run() -> dict[str, Any]:
            def _rebuild() -> dict[str, Any]:
                exists = db.conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='volume_fts'"
                ).fetchone()[0]
                if not exists:
                    raise RuntimeError(
                        "FTS tables do not exist — FTS5 is unavailable in this SQLite build"
                    )
                counts_before = {
                    t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("volume_fts", "issue_fts", "publisher_fts")
                }
                for t in ("volume_fts", "issue_fts", "publisher_fts"):
                    db.conn.execute(f"INSERT INTO {t}({t}) VALUES ('rebuild')")
                db.conn.commit()
                counts_after = {
                    t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("volume_fts", "issue_fts", "publisher_fts")
                }
                return {"before": counts_before, "after": counts_after}

            result = await asyncio.to_thread(_rebuild)
            logger.info("FTS rebuild complete: %s", result)
            return result

        job_id = _start_job("FTS rebuild", {}, _run())
        return {"status": "started", "job_id": job_id}

    # Admin — image cache cleanup
    @app.post("/admin/cleanup/images")
    async def admin_cleanup_images() -> dict[str, Any]:
        """Trigger immediate image cache cleanup (remove entries past TTL).

        Returns immediately with a job_id; poll GET /admin/jobs/{job_id} for status.
        """
        image_cache: ImageCache = app.state.image_cache

        async def _run() -> dict[str, Any]:
            removed = await asyncio.to_thread(image_cache.cleanup_expired)
            return {"removed": removed}

        job_id = _start_job("Image cache cleanup", {}, _run())
        return {"status": "started", "job_id": job_id}

    # Admin — scheduler job history
    @app.get("/admin/scheduler/history")
    def admin_scheduler_history() -> list[dict[str, Any]]:
        """Return the last 100 scheduled background job runs, newest first."""
        return get_scheduler_log()

    app.include_router(api_router)

    return app
