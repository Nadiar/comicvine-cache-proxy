"""Background job registry, runner and persistent run history.

Every background job runs through :class:`JobRunner`, whether APScheduler
fired it or someone started it from the dashboard, so each run gets the same
record: trigger, parameters, status, stop reason, live progress, result, a
one-line summary, duration and the log lines it wrote.  Runs are stored in a
small SQLite file of their own (``jobs_db_path``), so history survives
restarts and never competes with the main database for its write lock.

Job functions report how they ended through a ``stop_reason`` key in the
result dict (see :data:`STOP_REASONS`); the runner turns that into a status:

    done       finished its work
    partial    stopped early (budget, rate limit, upstream error...); the next
               run carries on
    idle       ran and found nothing to do
    skipped    did not run (disabled, previous run still going, missed start)
    error      raised, or found a problem it was checking for
    cancelled  cancelled from the dashboard
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apscheduler.schedulers.base import BaseScheduler

    from cvproxy.config import Settings

logger = logging.getLogger("cvproxy.jobs")

RUNNING = "running"
DONE = "done"
PARTIAL = "partial"
IDLE = "idle"
SKIPPED = "skipped"
ERROR = "error"
CANCELLED = "cancelled"
STATUSES = (RUNNING, DONE, PARTIAL, IDLE, SKIPPED, ERROR, CANCELLED)
TERMINAL = (DONE, PARTIAL, IDLE, SKIPPED, ERROR, CANCELLED)

#: stop_reason -> (status, human-readable explanation)
STOP_REASONS: dict[str, tuple[str, str]] = {
    "nothing_to_do": (IDLE, "nothing to do"),
    "waiting": (IDLE, "queued requests are waiting for their endpoint's window"),
    "disabled": (SKIPPED, "disabled by configuration"),
    "overlap": (SKIPPED, "previous run was still running"),
    "missed": (SKIPPED, "missed its start time"),
    "budget": (PARTIAL, "hourly backfill budget for the endpoint is used up"),
    "client_queue": (PARTIAL, "paused so queued client requests go first"),
    "rate_limited": (PARTIAL, "ComicVine refused (rate limit or cooldown)"),
    "upstream_error": (PARTIAL, "a ComicVine request failed"),
    "cdn_error": (PARTIAL, "the image CDN refused or failed"),
    "run_limit": (PARTIAL, "per-run limit reached; the next run continues"),
    "wal_reader": (PARTIAL, "a long-running reader kept the WAL from being checkpointed"),
    "needs_attention": (PARTIAL, "found problems; see the result"),
    "check_failed": (ERROR, "the check found problems"),
}

#: Log lines kept per run (the newest ones).
_RUN_LOG_LINES = 300
#: History older than this is pruned.
_HISTORY_DAYS = 30

_current: contextvars.ContextVar[JobRun | None] = contextvars.ContextVar(
    "cvproxy_job_run", default=None
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def fmt_duration(seconds: float | None) -> str:
    """'850ms', '42.1s', '12m05s', '3h02m'."""
    if seconds is None:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Param:
    """A parameter a manual run accepts (rendered as a form field on the dashboard)."""

    name: str
    kind: str  # "int" | "float" | "bool" | "str" | "text"
    default: Any
    label: str
    help: str = ""
    min: float | None = None
    max: float | None = None

    def coerce(self, raw: Any) -> Any:
        if raw is None or raw == "":
            return self.default
        value: Any
        if self.kind == "bool":
            value = raw if isinstance(raw, bool) else str(raw).strip().lower() in (
                "1", "true", "yes", "on",
            )
        elif self.kind == "int":
            value = int(raw)
        elif self.kind == "float":
            value = float(raw)
        else:
            value = str(raw)
        if self.kind in ("int", "float"):
            if self.min is not None and value < self.min:
                raise ValueError(f"{self.name} must be at least {self.min:g}")
            if self.max is not None and value > self.max:
                raise ValueError(f"{self.name} must be at most {self.max:g}")
        return value

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "default": self.default,
            "label": self.label, "help": self.help, "min": self.min, "max": self.max,
        }


JobFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


def _never_disabled(_settings: Settings) -> str | None:
    return None


@dataclass
class JobSpec:
    """One background job: what it does, when it runs and how to run it."""

    id: str
    name: str
    category: str  # sync | backfill | export | maintenance | repair
    #: Plain text, or built from the current settings when the catalog is read
    description: str | Callable[[Settings], str]
    run: JobFn
    #: APScheduler trigger kwargs ({"trigger": "cron", "minute": 10}); None = manual only
    trigger: dict[str, Any] | None = None
    #: ComicVine API endpoints the job calls (shares their hourly windows)
    endpoints: tuple[str, ...] = ()
    params: tuple[Param, ...] = ()
    #: Parameters for scheduled runs (missing ones use the Param default)
    schedule_params: dict[str, Any] = field(default_factory=dict)
    manual: bool = True
    #: False for work that runs on a worker thread and can't be interrupted
    cancellable: bool = True
    #: False: runs that found nothing to do are not written to history
    persist_idle: bool = True
    #: The dashboard asks for confirmation before a manual run
    destructive: bool = False
    #: Returns why the job is disabled under these settings, or None
    disabled: Callable[[Settings], str | None] = _never_disabled
    summarize: Callable[[dict[str, Any]], str] | None = None

    def coerce_params(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        raw = dict(raw or {})
        known = {p.name for p in self.params}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"Unknown parameter(s) for {self.id}: {', '.join(unknown)}")
        return {p.name: p.coerce(raw.get(p.name)) for p in self.params}


def describe_trigger(trigger: dict[str, Any] | None) -> str:
    """Human-readable schedule for a trigger dict, in the container's local time."""
    if not trigger:
        return "Manual only"
    if trigger.get("trigger") == "interval":
        minutes = trigger.get("minutes")
        hours = trigger.get("hours")
        return f"Every {minutes} min" if minutes else f"Every {hours} h"
    minute = int(trigger.get("minute", 0))
    hour = trigger.get("hour")
    dow = trigger.get("day_of_week")
    if hour is None or hour == "*":
        return f"Hourly at :{minute:02d}"
    if isinstance(hour, str) and hour.startswith("*/"):
        return f"Every {hour[2:]} h at :{minute:02d}"
    when = f"{int(hour):02d}:{minute:02d}"
    if dow:
        return f"Weekly, {str(dow).capitalize()} {when}"
    return f"Daily at {when}"

# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@dataclass
class JobRun:
    run_id: str
    job_id: str
    name: str
    trigger: str  # "schedule" | "manual"
    params: dict[str, Any]
    started_at: str = field(default_factory=_now)
    status: str = RUNNING
    finished_at: str | None = None
    duration_s: float | None = None
    summary: str = ""
    stop_reason: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=_RUN_LOG_LINES))
    log_dropped: int = 0
    task: asyncio.Task[Any] | None = None
    t0: float = field(default_factory=time.monotonic)

    @property
    def tag(self) -> str:
        return f"{self.job_id}#{self.run_id[:6]}"

    def public(self, *, with_log: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "run_id": self.run_id,
            "job_id": self.run_id,  # older dashboards and scripts poll by job_id
            "job": self.job_id,
            "name": self.name,
            "trigger": self.trigger,
            "params": self.params,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": (
                round(time.monotonic() - self.t0, 1) if self.status == RUNNING
                else self.duration_s
            ),
            "summary": self.summary,
            "stop_reason": self.stop_reason,
            "stop_text": STOP_REASONS.get(self.stop_reason or "", ("", ""))[1] or None,
            "error": self.error,
            "result": self.result,
            "progress": self.progress,
        }
        if with_log:
            out["log"] = list(self.log)
            out["log_dropped"] = self.log_dropped
        return out


def current_run() -> JobRun | None:
    """The run executing in this context (also inside asyncio.to_thread)."""
    return _current.get()


def report_progress(done: int, total: int | None = None, note: str | None = None) -> None:
    """Record progress for the current run; a no-op outside a job."""
    run = _current.get()
    if run is not None:
        run.progress = {"done": done, "total": total, "note": note}


class _RunLogHandler(logging.Handler):
    """Copies log records emitted while a job runs into that run's log."""

    def emit(self, record: logging.LogRecord) -> None:
        run = _current.get()
        if run is None:
            return
        # Per-request httpx lines would crowd out the job's own messages
        if record.name.startswith(("httpx", "httpcore")) and record.levelno < logging.WARNING:
            return
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if record.exc_info:
            trace = record.exc_text or logging.Formatter().formatException(record.exc_info)
            msg = f"{msg}\n{trace}"
        ts = datetime.fromtimestamp(record.created, UTC).strftime("%H:%M:%S")
        if run.log.maxlen is not None and len(run.log) == run.log.maxlen:
            run.log_dropped += 1
        run.log.append(f"{ts} {record.levelname:<7} {record.name.removeprefix('cvproxy.')}: {msg}")


class JobTagFilter(logging.Filter):
    """Adds ``%(jobtag)s`` ("[job_id#run] " inside a job, "" outside) to records."""

    def filter(self, record: logging.LogRecord) -> bool:
        run = _current.get()
        record.jobtag = f"[{run.tag}] " if run is not None else ""
        return True


_capture_lock = threading.Lock()
_capture_installed = False


def install_run_log_capture() -> None:
    """Attach the per-run log handler to the root logger (once per process)."""
    global _capture_installed
    with _capture_lock:
        if _capture_installed:
            return
        logging.getLogger().addHandler(_RunLogHandler(level=logging.INFO))
        _capture_installed = True


# ---------------------------------------------------------------------------
# History store
# ---------------------------------------------------------------------------

_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS job_runs (
    run_id      TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    trigger     TEXT NOT NULL,
    params      TEXT,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    duration_s  REAL,
    summary     TEXT,
    stop_reason TEXT,
    error       TEXT,
    result      TEXT,
    log         TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job_id, started_at);
"""

_LIST_COLS = (
    "run_id, job_id, name, trigger, params, status, started_at, finished_at,"
    " duration_s, summary, stop_reason, error, result"
)


class JobHistory:
    """Job runs in their own small SQLite file (or in memory when path is None)."""

    def __init__(self, path: Path | None = None) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path) if path is not None else ":memory:", check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        if path is not None:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_HISTORY_DDL)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save(self, run: JobRun) -> None:
        log = "\n".join(run.log) if run.status != RUNNING else None
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO job_runs (run_id, job_id, name, trigger, params,"
                " status, started_at, finished_at, duration_s, summary, stop_reason, error,"
                " result, log) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run.run_id, run.job_id, run.name, run.trigger,
                    json.dumps(run.params, default=str), run.status, run.started_at,
                    run.finished_at, run.duration_s, run.summary, run.stop_reason, run.error,
                    json.dumps(run.result, default=str) if run.result is not None else None,
                    log,
                ],
            )
            self._conn.commit()

    def mark_interrupted(self) -> int:
        """Runs left 'running' by a previous process: it stopped while they ran."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE job_runs SET status = ?, finished_at = ?, error = ?, summary = ?"
                " WHERE status = ?",
                [
                    ERROR, _now(), "interrupted: cvproxy stopped while the job was running",
                    "interrupted by a restart", RUNNING,
                ],
            )
            self._conn.commit()
            return cur.rowcount

    def prune(self, days: int = _HISTORY_DAYS) -> int:
        cutoff = datetime.fromtimestamp(time.time() - days * 86400, UTC).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM job_runs WHERE started_at < ?", [cutoff])
            self._conn.commit()
            return cur.rowcount
    @staticmethod
    def _row(row: sqlite3.Row, *, with_log: bool = False) -> dict[str, Any]:
        reason = row["stop_reason"]
        out: dict[str, Any] = {
            "run_id": row["run_id"],
            "job_id": row["run_id"],
            "job": row["job_id"],
            "name": row["name"],
            "trigger": row["trigger"],
            "params": json.loads(row["params"] or "{}"),
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "duration_s": row["duration_s"],
            "summary": row["summary"] or "",
            "stop_reason": reason,
            "stop_text": STOP_REASONS.get(reason or "", ("", ""))[1] or None,
            "error": row["error"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "progress": None,
        }
        if with_log:
            out["log"] = (row["log"] or "").splitlines()
            out["log_dropped"] = 0
        return out

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        job_id: str | None = None,
        status: str | None = None,
        trigger: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        for col, val in (("job_id", job_id), ("status", status), ("trigger", trigger)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        sql = f"SELECT {_LIST_COLS} FROM job_runs"  # noqa: S608
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._conn.execute(sql, [*args, limit, offset]).fetchall()
        return [self._row(r) for r in rows]

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_LIST_COLS}, log FROM job_runs WHERE run_id = ?",  # noqa: S608
                [run_id],
            ).fetchone()
        return self._row(row, with_log=True) if row else None

    def latest_per_job(self) -> dict[str, dict[str, Any]]:
        """{job_id: {"last": run, "last_ok": run}} (last_ok = newest done/idle run)."""
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM (SELECT {_LIST_COLS},"  # noqa: S608
                "  ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY started_at DESC) AS rk,"
                "  ROW_NUMBER() OVER (PARTITION BY job_id, status IN ('done', 'idle')"
                "    ORDER BY started_at DESC) AS ok_rk"
                " FROM job_runs WHERE status != 'running')"
                " WHERE rk = 1 OR (ok_rk = 1 AND status IN ('done', 'idle'))"
            ).fetchall()
        for row in rows:
            entry = out.setdefault(row["job_id"], {})
            run = self._row(row)
            if row["rk"] == 1:
                entry["last"] = run
            if row["ok_rk"] == 1 and row["status"] in (DONE, IDLE):
                entry["last_ok"] = run
        return out

    def counts_since(self, since_iso: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM job_runs WHERE started_at >= ? GROUP BY status",
                [since_iso],
            ).fetchall()
        return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class JobError(Exception):
    """A run could not be started (unknown job, disabled, already running...)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def generic_summary(result: dict[str, Any]) -> str:
    parts = []
    for key, value in result.items():
        if key in ("stop_reason", "error") or isinstance(value, (dict, list)) or value is None:
            continue
        parts.append(f"{key.replace('_', ' ')}: {value}")
    return ", ".join(parts)


class JobRunner:
    """Runs jobs, tracks live runs and records every run in :class:`JobHistory`."""

    def __init__(
        self,
        history: JobHistory | None = None,
        settings_provider: Callable[[], Settings] | None = None,
    ) -> None:
        self.specs: dict[str, JobSpec] = {}
        self.history = history or JobHistory(None)
        self._settings_provider = settings_provider
        self._active: dict[str, JobRun] = {}
        #: Newest run per job kept in memory (includes runs not written to history)
        self._latest: dict[str, JobRun] = {}
        self._scheduler: BaseScheduler | None = None
        self._finished = 0
        install_run_log_capture()

    # -- setup ---------------------------------------------------------------

    def register(self, specs: Iterable[JobSpec]) -> None:
        for spec in specs:
            self.specs[spec.id] = spec

    def _settings(self) -> Settings:
        if self._settings_provider is not None:
            return self._settings_provider()
        from cvproxy.config import get_settings

        return get_settings()

    def disabled_reason(self, spec: JobSpec) -> str | None:
        try:
            return spec.disabled(self._settings())
        except Exception:  # noqa: BLE001 - no settings available (unit tests)
            return None

    def description(self, spec: JobSpec) -> str:
        if isinstance(spec.description, str):
            return spec.description
        return spec.description(self._settings())

    def attach(self, scheduler: BaseScheduler) -> None:
        """Record APScheduler's missed and overlapping runs as skipped runs."""
        from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED

        self._scheduler = scheduler
        scheduler.add_listener(
            self._on_scheduler_event, EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES
        )

    def sync_schedule(self) -> None:
        """Schedule every enabled job that has a trigger and unschedule disabled ones.

        Called at start-up and whenever settings change, since a setting can turn
        a job on or off (BACKUP_KEEP=0, COVER_HASH_PER_HOUR=0).
        """
        if self._scheduler is None:
            return
        for spec in self.specs.values():
            if spec.trigger is None:
                continue
            scheduled = self._scheduler.get_job(spec.id) is not None
            if self.disabled_reason(spec) is not None:
                if scheduled:
                    self._scheduler.remove_job(spec.id)
                continue
            if scheduled:
                continue
            trigger = dict(spec.trigger)
            kind = trigger.pop("trigger")
            self._scheduler.add_job(
                self.scheduled(spec.id), kind, id=spec.id, name=spec.name,
                replace_existing=True, max_instances=1, coalesce=True, **trigger,
            )

    def startup(self) -> None:
        interrupted = self.history.mark_interrupted()
        if interrupted:
            logger.warning(
                "Marked %d job run(s) left running by the previous process as interrupted",
                interrupted,
            )
        self.history.prune()

    def shutdown(self) -> None:
        for run in list(self._active.values()):
            run.status = ERROR
            run.error = "interrupted: cvproxy stopped while the job was running"
            run.summary = "interrupted by shutdown"
            run.finished_at = _now()
            run.duration_s = round(time.monotonic() - run.t0, 1)
            self._save(run)
        self.history.close()
    # -- starting runs -------------------------------------------------------

    def running(self, job_id: str) -> JobRun | None:
        return next((r for r in self._active.values() if r.job_id == job_id), None)

    @staticmethod
    def _new_run(spec: JobSpec, trigger: str, params: dict[str, Any]) -> JobRun:
        return JobRun(
            run_id=uuid.uuid4().hex, job_id=spec.id, name=spec.name, trigger=trigger,
            params=params,
        )

    def start(self, job_id: str, params: dict[str, Any] | None = None) -> JobRun:
        """Start a manual run in the background and return it."""
        spec = self.specs.get(job_id)
        if spec is None:
            raise JobError(f"Unknown job: {job_id}", 404)
        if not spec.manual:
            raise JobError(f"{spec.name} can't be started by hand")
        reason = self.disabled_reason(spec)
        if reason:
            raise JobError(f"{spec.name} is disabled: {reason}")
        active = self.running(job_id)
        if active is not None:
            raise JobError(f"{spec.name} is already running (started {active.started_at})", 409)
        try:
            clean = spec.coerce_params(params)
        except (TypeError, ValueError) as exc:
            raise JobError(str(exc)) from exc
        run = self._new_run(spec, "manual", clean)
        self._begin(spec, run)
        run.task = asyncio.create_task(self._execute(spec, run))
        return run

    def scheduled(self, job_id: str) -> Callable[[], Awaitable[None]]:
        """The coroutine function APScheduler calls for *job_id*."""

        async def _fire() -> None:
            spec = self.specs[job_id]
            active = self.running(job_id)
            if active is not None:  # a manual run of the same job is in progress
                self._record_skip(spec, "overlap", running_since=active.started_at)
                return
            reason = self.disabled_reason(spec)
            if reason:
                self._record_skip(spec, "disabled", note=reason)
                return
            run = self._new_run(spec, "schedule", spec.coerce_params(spec.schedule_params))
            run.task = asyncio.current_task()
            self._begin(spec, run)
            await self._execute(spec, run)

        _fire.__name__ = _fire.__qualname__ = f"run_{job_id}"
        return _fire

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self._active.get(run_id)
        if run is None:
            stored = self.get(run_id)
            if stored is None:
                raise JobError("Run not found", 404)
            return stored
        spec = self.specs.get(run.job_id)
        if spec is not None and not spec.cancellable:
            raise JobError(f"{spec.name} works on a worker thread and can't be cancelled", 409)
        if run.task is not None and not run.task.done():
            logger.info("Cancelling %s run %s", run.name, run.tag)
            run.task.cancel()
        return run.public()

    # -- execution -----------------------------------------------------------

    def _begin(self, spec: JobSpec, run: JobRun) -> None:
        """Make the run visible (and durable) before its task first runs."""
        self._active[run.run_id] = run
        self._latest[spec.id] = run
        if spec.persist_idle:
            self._save(run)

    async def _execute(self, spec: JobSpec, run: JobRun) -> None:
        token = _current.set(run)
        params = f", params {json.dumps(run.params, default=str)}" if run.params else ""
        # Jobs that don't keep idle runs (pending fetch, every 2 min) log at DEBUG
        # unless they actually do something; see _finish.
        logger.log(
            logging.INFO if spec.persist_idle else logging.DEBUG,
            "Job start: %s (trigger %s%s)", spec.name, run.trigger, params,
        )
        try:
            result = await spec.run(run.params)
        except asyncio.CancelledError:
            self._finish(spec, run, cancelled=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s raised", spec.name)
            self._finish(spec, run, error=exc)
        else:
            self._finish(spec, run, result=result or {})
        finally:
            self._active.pop(run.run_id, None)
            _current.reset(token)

    def _finish(
        self,
        spec: JobSpec,
        run: JobRun,
        *,
        result: dict[str, Any] | None = None,
        error: BaseException | None = None,
        cancelled: bool = False,
    ) -> None:
        run.finished_at = _now()
        run.duration_s = round(time.monotonic() - run.t0, 2)
        if cancelled:
            run.status = CANCELLED
            run.summary = "cancelled"
        elif error is not None:
            run.status = ERROR
            run.error = f"{type(error).__name__}: {error}"
            run.result = {"error": str(error)}
            run.summary = run.error
        else:
            result = result or {}
            run.result = result
            reason = result.get("stop_reason")
            run.stop_reason = reason
            run.status = STOP_REASONS[reason][0] if reason in STOP_REASONS else DONE
            summary = (spec.summarize or generic_summary)(result)
            if reason in STOP_REASONS:
                text = STOP_REASONS[reason][1]
                summary = f"{summary}; {text}" if summary else text
            run.summary = summary
            if run.status == ERROR:
                run.error = str(result.get("error") or STOP_REASONS.get(reason or "", ("", ""))[1])
        if run.status == ERROR:
            level = logging.WARNING
        elif run.status == IDLE and not spec.persist_idle:
            level = logging.DEBUG
        else:
            level = logging.INFO
        logger.log(
            level, "Job %s: %s in %s: %s",
            run.status, spec.name, fmt_duration(run.duration_s), run.summary or "-",
        )
        if spec.persist_idle or run.status != IDLE:
            self._save(run)
        self._finished += 1
        if self._finished % 200 == 0:
            self.history.prune()

    def _record_skip(self, spec: JobSpec, reason: str, **extra: Any) -> None:
        run = self._new_run(spec, "schedule", {})
        token = _current.set(run)
        try:
            self._latest.setdefault(spec.id, run)
            self._finish(spec, run, result={"stop_reason": reason, **extra})
        finally:
            _current.reset(token)

    def _on_scheduler_event(self, event: Any) -> None:
        from apscheduler.events import EVENT_JOB_MAX_INSTANCES

        spec = self.specs.get(getattr(event, "job_id", ""))
        if spec is None:
            return
        times = getattr(event, "scheduled_run_times", None) or [
            getattr(event, "scheduled_run_time", None)
        ]
        when = times[0].isoformat() if times and times[0] is not None else None
        if event.code == EVENT_JOB_MAX_INSTANCES:
            active = self.running(spec.id)
            self._record_skip(
                spec, "overlap", scheduled_for=when,
                running_since=active.started_at if active else None,
            )
        else:
            self._record_skip(spec, "missed", scheduled_for=when)

    def _save(self, run: JobRun) -> None:
        try:
            self.history.save(run)
        except Exception:  # noqa: BLE001 - history must never break a job
            logger.warning("Could not write job history for %s", run.tag, exc_info=True)
    # -- queries -------------------------------------------------------------

    def get(self, run_id: str) -> dict[str, Any] | None:
        run = self._active.get(run_id)
        if run is not None:
            return run.public(with_log=True)
        for latest in self._latest.values():
            if latest.run_id == run_id:
                return latest.public(with_log=True)
        return self.history.get(run_id)

    def list_runs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        job_id: str | None = None,
        status: str | None = None,
        trigger: str | None = None,
    ) -> list[dict[str, Any]]:
        """Runs newest first; live runs carry their current progress."""
        rows = self.history.list(
            limit=limit, offset=offset, job_id=job_id, status=status, trigger=trigger
        )
        live = {
            r.run_id: r for r in self._active.values()
            if (not job_id or r.job_id == job_id) and (not trigger or r.trigger == trigger)
        }
        out = []
        for row in rows:
            run = live.pop(row["run_id"], None)
            out.append(run.public() if run is not None else row)
        if offset == 0 and status in (None, "", RUNNING):
            # live runs that aren't in history yet (jobs that only keep busy runs)
            extra = sorted((r.public() for r in live.values()),
                           key=lambda r: r["started_at"], reverse=True)
            out = extra + out
        return out

    def catalog(self) -> list[dict[str, Any]]:
        """Every job with its description, schedule, next run and latest runs."""
        stored = self.history.latest_per_job()
        out = []
        for spec in self.specs.values():
            job = self._scheduler.get_job(spec.id) if self._scheduler is not None else None
            entry = stored.get(spec.id, {})
            last = entry.get("last")
            last_ok = entry.get("last_ok")
            mem = self._latest.get(spec.id)
            if mem is not None and mem.status != RUNNING:
                if last is None or mem.started_at > last["started_at"]:
                    last = mem.public()
                if mem.status in (DONE, IDLE) and (
                    last_ok is None or mem.started_at > last_ok["started_at"]
                ):
                    last_ok = mem.public()
            active = self.running(spec.id)
            next_run = getattr(job, "next_run_time", None) if job is not None else None
            if job is not None:
                schedule = describe_trigger(spec.trigger)
            else:
                schedule = "Manual only" if spec.trigger is None else "Not scheduled"
            out.append({
                "id": spec.id,
                "name": spec.name,
                "category": spec.category,
                "description": self.description(spec),
                "endpoints": list(spec.endpoints),
                "schedule": schedule,
                "scheduled": job is not None,
                "next_run": next_run.isoformat() if next_run is not None else None,
                "disabled": self.disabled_reason(spec),
                "manual": spec.manual,
                "cancellable": spec.cancellable,
                "destructive": spec.destructive,
                "params": [p.public() for p in spec.params],
                "running": active.public() if active is not None else None,
                "last": last,
                "last_ok": last_ok,
            })
        return out
