"""FastAPI application factory and lifespan management."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cvproxy.auth import (
    DEFAULT_NEXT,
    SESSION_COOKIE,
    SESSION_SECONDS,
    AuthMiddleware,
    credentials_match,
    login_enabled,
    login_page,
    safe_next,
    session_token,
)
from cvproxy.config import Settings, get_settings, override_settings
from cvproxy.cv_client import CVClient
from cvproxy.dashboard import DASHBOARD_HTML
from cvproxy.db import Database
from cvproxy.image_cache import ImageCache
from cvproxy.jobs import STATUSES, JobError, JobHistory, JobRunner
from cvproxy.overrides import (
    InvalidOverride,
    OverrideStore,
    apply_overrides,
    describe_settings,
    usable_overrides,
)
from cvproxy.routes.api import router as api_router
from cvproxy.scheduler import build_job_specs, create_scheduler
from cvproxy.stats import StatsTracker
from cvproxy.watchdog import LoopWatchdog

logger = logging.getLogger(__name__)

#: Seconds /health reuses its row counts before refreshing them in the background.
_HEALTH_COUNTS_TTL = 300.0


class TrailingSlashMiddleware:
    """ASGI middleware that normalises request paths to include a trailing slash.

    This prevents FastAPI's redirect_slashes from issuing 307 redirects,
    which break when the app runs behind a TLS-terminating reverse proxy
    (the redirect URL gets generated with http:// instead of https://).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope["path"]
            # Only normalise /api/ paths (where routes expect trailing slashes)
            last_segment = path.rsplit("/", 1)[-1]
            if path.startswith("/api/") and not path.endswith("/") and "." not in last_segment:
                scope["path"] = path + "/"
        await self.app(scope, receive, send)


class XmlFormatMiddleware:
    """ASGI middleware that converts JSON responses to CV-compatible XML.

    When a request to /api/ includes ``format=xml`` in the query string,
    this middleware intercepts the JSON response body and re-serialises it
    as XML matching the Comic Vine XML API format.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        qs = parse_qs(scope.get("query_string", b"").decode())
        if qs.get("format", ["json"])[0] != "xml":
            await self.app(scope, receive, send)
            return

        # Collect the response body, then convert JSON -> XML.
        response_body: list[bytes] = []
        response_started = False
        original_headers: list[tuple[bytes, bytes]] = []
        original_status = 200

        async def capture_send(message: Message) -> None:
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
            xml_body += (
                "<response>" + _dict_to_cv_xml(data, resource_type=resource_type) + "</response>"
            )
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
    base_settings = get_settings()
    jobs_db = base_settings.jobs_db_path or base_settings.stats_db_path.with_name("jobs.db")
    settings = _load_overrides(app, base_settings, jobs_db)
    if not login_enabled(settings):
        logger.warning("DASHBOARD_PASSWORD is not set: /dashboard, /admin and /docs are open")

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
        rate_limit_cooldown_seconds=settings.rate_limit_cooldown_seconds,
        state_path=settings.rate_state_path,
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

    # Row counts for /health, refreshed off the event loop (see health())
    app.state.health_counts = counts
    app.state.health_counts_at = time.monotonic()
    app.state.health_refresh = None

    # Logs the blocking stack whenever the event loop stalls
    watchdog = LoopWatchdog(asyncio.get_running_loop())
    watchdog.start()

    # Background jobs: one runner records scheduled and manual runs alike
    runner = JobRunner(JobHistory(jobs_db))
    runner.register(build_job_specs(settings, cache, db, client))
    runner.startup()
    app.state.jobs = runner
    logger.info("Job runner ready: %d jobs, history in %s", len(runner.specs), jobs_db)

    scheduler = None
    if settings.sync_enabled:
        scheduler = create_scheduler(settings, cache, db, client, runner)
        scheduler.start()
        logger.info("Scheduler started: %d scheduled jobs", len(scheduler.get_jobs()))

    yield

    # Shutdown
    watchdog.stop()
    if scheduler:
        scheduler.shutdown(wait=False)
    runner.shutdown()
    await client.close()
    cache.close()
    stats.close()
    db.close()
    app.state.override_store.close()
    override_settings(base_settings)
    logger.info("Shutdown complete")


def _load_overrides(app: FastAPI, base: Settings, jobs_db: Path) -> Settings:
    """Apply the dashboard's stored setting overrides before anything reads settings."""
    store = OverrideStore(jobs_db)
    overrides = usable_overrides(store.load())
    settings = apply_overrides(base, overrides)
    override_settings(settings)
    app.state.override_store = store
    app.state.base_settings = base
    app.state.setting_overrides = overrides
    if overrides:
        logger.info("Setting overrides from the dashboard: %s", overrides)
    return settings


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
    app.add_middleware(AuthMiddleware)

    # Health check at root level (not under /api)
    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Liveness plus row counts that are at most a few minutes old.

        COUNT(*) over cv_issue is a full index walk; on slow storage it outlasted
        the container healthcheck timeout, Traefik dropped the route and every
        request 404'd.  Counts are refreshed on a worker thread instead.
        """
        db: Database = app.state.db
        cache: ImageCache = app.state.image_cache
        state = app.state
        refresh = getattr(state, "health_refresh", None)
        stale = time.monotonic() - getattr(state, "health_counts_at", 0.0) > _HEALTH_COUNTS_TTL
        if stale and (refresh is None or refresh.done()):

            async def _refresh() -> None:
                try:
                    state.health_counts = await asyncio.to_thread(db.count_rows_snapshot)
                    state.health_counts_at = time.monotonic()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Health row-count refresh failed: %s", exc)

            state.health_refresh = asyncio.create_task(_refresh())
        return {
            "status": "ok",
            "database": getattr(state, "health_counts", {}),
            "image_cache": cache.stats(),
        }

    # Dashboard — pretty HTML UI
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page() -> str:
        logout = (
            '<form method="post" action="/logout" class="m-0"><button class="btn btn-sm '
            'btn-outline-secondary" title="Sign out"><i class="bi bi-box-arrow-right"></i>'
            "</button></form>"
            if login_enabled(get_settings()) else ""
        )
        return DASHBOARD_HTML.replace("<!--LOGOUT-->", logout)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Any:
        if not login_enabled(get_settings()):
            return RedirectResponse(DEFAULT_NEXT, status_code=303)
        return login_page(safe_next(request.query_params.get("next", DEFAULT_NEXT)))

    @app.post("/login")
    async def login_submit(request: Request) -> Any:
        form = parse_qs((await request.body()).decode())
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        target = safe_next(form.get("next", [DEFAULT_NEXT])[0])
        settings = get_settings()
        if not login_enabled(settings):
            return RedirectResponse(target, status_code=303)
        if not credentials_match(settings, username, password):
            logger.warning("Failed dashboard login for %r from %s", username,
                           request.client.host if request.client else "?")
            await asyncio.sleep(1)  # slows down password guessing
            return HTMLResponse(login_page(target, "Wrong username or password."),
                                status_code=401)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session_token(settings.dashboard_username, settings.dashboard_password,
                          expires=int(time.time()) + SESSION_SECONDS),
            max_age=SESSION_SECONDS, httponly=True, samesite="lax", path="/",
            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        )
        return response

    @app.post("/logout")
    def logout() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

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

    # -- background jobs -----------------------------------------------------

    def _runner() -> JobRunner:
        runner: JobRunner = app.state.jobs
        return runner

    def _start(job: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            run = _runner().start(job, params)
        except JobError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {
            "status": "started", "job_id": run.run_id, "run_id": run.run_id,
            "job": job, "params": run.params,
        }

    @app.get("/admin/jobs")
    def admin_jobs_list(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        job: str | None = Query(default=None, description="Only runs of this job id"),
        status: str | None = Query(default=None, description="|".join(STATUSES)),
        trigger: str | None = Query(default=None, description="schedule | manual"),
    ) -> list[dict[str, Any]]:
        """Job runs (scheduled and manual), newest first; live runs show progress."""
        return _runner().list_runs(
            limit=limit, offset=offset, job_id=job, status=status, trigger=trigger
        )

    @app.get("/admin/jobs/catalog")
    def admin_jobs_catalog() -> list[dict[str, Any]]:
        """Every job: description, schedule, next run, parameters, latest runs."""
        return _runner().catalog()

    @app.get("/admin/jobs/{run_id}")
    def admin_job_status(run_id: str) -> dict[str, Any]:
        """One run, including the log lines it wrote."""
        run = _runner().get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return run

    @app.delete("/admin/jobs/{run_id}")
    def admin_job_cancel(run_id: str) -> dict[str, Any]:
        """Cancel a running job run."""
        try:
            return _runner().cancel(run_id)
        except JobError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/admin/jobs/{job}/run")
    async def admin_job_run(
        job: str, params: Annotated[dict[str, Any] | None, Body()] = None
    ) -> dict[str, Any]:
        """Start any job by id now; the body holds its parameters (see the catalog)."""
        return _start(job, params)

    def _settings_view() -> list[dict[str, Any]]:
        return describe_settings(
            app.state.base_settings, get_settings(), app.state.setting_overrides
        )

    @app.get("/admin/settings")
    def admin_settings() -> list[dict[str, Any]]:
        """Settings editable here, with their value, default and source."""
        return _settings_view()

    @app.put("/admin/settings")
    def admin_settings_update(
        changes: Annotated[dict[str, Any], Body()],
    ) -> list[dict[str, Any]]:
        """Override settings ({name: value}); a null value goes back to env/default."""
        overrides = {**app.state.setting_overrides, **changes}
        overrides = {k: v for k, v in overrides.items() if v is not None}
        try:
            settings = apply_overrides(app.state.base_settings, overrides)
        except InvalidOverride as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        app.state.override_store.save(overrides)
        app.state.setting_overrides = overrides
        override_settings(settings)
        _runner().sync_schedule()
        logger.info("Setting overrides changed: %s", changes)
        return _settings_view()

    # Older per-job endpoints, kept for scripts; they start the same jobs.

    @app.post("/admin/sync/issues")
    async def admin_sync_issues(
        days: float = Query(
            default=14.0,
            description="Re-sync issues whose date_last_updated is within this many days.",
            ge=0.1,
            le=365,
        ),
    ) -> dict[str, Any]:
        """Issue re-sync over a time window (does not move the watermark)."""
        return {**_start("issue_resync", {"days": days}), "window_hours": int(days * 24)}

    @app.post("/admin/repair/publishers")
    async def admin_repair_publishers(
        batch_size: int = Query(default=100, ge=10, le=100),
        calendar_only: bool = Query(default=True),
    ) -> dict[str, Any]:
        """Re-sync publisher IDs for cached volumes."""
        started = _start(
            "publisher_repair", {"batch_size": batch_size, "calendar_only": calendar_only}
        )
        return {**started, "calendar_only": calendar_only}

    @app.post("/admin/evict")
    async def admin_evict(
        dry_run: bool = Query(default=True, description="Count only (safe default)."),
    ) -> dict[str, Any]:
        """Cache eviction with the configured settings."""
        return {**_start("evict_stale_data", {"dry_run": dry_run}), "dry_run": dry_run}

    @app.post("/admin/export/comicvine-db")
    async def admin_export_comicvine_db() -> dict[str, Any]:
        """Write the ComicManager-compatible comicvine.db now (CV_DB_EXPORT_PATH)."""
        started = _start("comicvine_db_export")
        return {**started, "path": str(get_settings().cv_db_export_path)}

    @app.post("/admin/mcl/refresh")
    async def admin_refresh_mcl() -> dict[str, Any]:
        """Download the newest community latest.mcl now (MCL_EXPORT_PATH)."""
        return _start("mcl_refresh")

    @app.post("/admin/mcl/backfill")
    async def admin_backfill_from_mcl() -> dict[str, Any]:
        """Fetch the volumes and issues latest.mcl lists but cvproxy doesn't have."""
        return _start("mcl_gap_backfill")

    @app.post("/admin/rebuild/fts")
    async def admin_rebuild_fts() -> dict[str, Any]:
        """Rebuild all FTS indexes."""
        return _start("fts_rebuild")

    @app.post("/admin/cleanup/images")
    async def admin_cleanup_images() -> dict[str, Any]:
        """Remove image cache entries past their TTL now."""
        return _start("image_cache_cleanup")

    @app.get("/admin/scheduler/history")
    def admin_scheduler_history() -> list[dict[str, Any]]:
        """The last 100 scheduled runs, newest first."""
        return _runner().list_runs(trigger="schedule", limit=100)

    app.include_router(api_router)

    return app
