"""Job runner, run history, new jobs and the /admin/jobs API."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cvproxy.cv_client import NOT_FOUND, RATE_LIMITED
from cvproxy.db import Database
from cvproxy.jobs import (
    Backlog,
    JobError,
    JobHistory,
    JobRun,
    JobRunner,
    JobSpec,
    Param,
    describe_trigger,
    report_progress,
)
from cvproxy.scheduler import (
    _backfill_volumes,
    _refetch_issues,
    build_job_specs,
    create_scheduler,
    parse_id_list,
)

_log = logging.getLogger("cvproxy.test_jobs")


def _spec(job_id: str, fn: Any, **kw: Any) -> JobSpec:
    return JobSpec(id=job_id, name=job_id.title(), category="maintenance",
                   description="test job", run=fn, **kw)


async def _wait(runner: JobRunner, run_id: str) -> dict[str, Any]:
    for _ in range(200):
        run = runner.get(run_id)
        assert run is not None
        if run["status"] != "running":
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


# -- runner: statuses -------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ({"updated": 3}, "done"),
        ({"updated": 1, "stop_reason": "budget"}, "partial"),
        ({"updated": 0, "stop_reason": "nothing_to_do"}, "idle"),
        ({"stop_reason": "disabled"}, "skipped"),
        ({"ok": False, "stop_reason": "check_failed"}, "error"),
    ],
)
async def test_status_follows_stop_reason(result: dict[str, Any], status: str) -> None:
    async def fn(_p: dict[str, Any]) -> dict[str, Any]:
        return result

    runner = JobRunner()
    runner.register([_spec("one", fn)])
    run = await _wait(runner, runner.start("one").run_id)
    assert run["status"] == status
    assert run["result"] == result
    assert run["trigger"] == "manual"
    assert run["duration_s"] is not None


async def test_exception_is_recorded_as_error_with_traceback_in_log() -> None:
    async def fn(_p: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("database is locked")

    runner = JobRunner()
    runner.register([_spec("boom", fn)])
    run = await _wait(runner, runner.start("boom").run_id)
    assert run["status"] == "error"
    assert "database is locked" in run["error"]
    assert any("database is locked" in line for line in run["log"])


async def test_cancel_marks_run_cancelled() -> None:
    async def fn(_p: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {}

    runner = JobRunner()
    runner.register([_spec("slow", fn)])
    run = runner.start("slow")
    await asyncio.sleep(0.01)
    runner.cancel(run.run_id)
    assert (await _wait(runner, run.run_id))["status"] == "cancelled"


async def test_uncancellable_job_refuses_cancel() -> None:
    async def fn(_p: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return {}

    runner = JobRunner()
    runner.register([_spec("thread", fn, cancellable=False)])
    run = runner.start("thread")
    with pytest.raises(JobError):
        runner.cancel(run.run_id)
    await _wait(runner, run.run_id)


async def test_start_rejects_second_run_bad_params_and_unknown_job() -> None:
    gate = asyncio.Event()

    async def fn(_p: dict[str, Any]) -> dict[str, Any]:
        await gate.wait()
        return {}

    runner = JobRunner()
    runner.register([_spec("job", fn, params=(Param("days", "int", 1, "Days", min=1, max=5),))])
    run = runner.start("job", {"days": "3"})
    assert run.params == {"days": 3}
    with pytest.raises(JobError) as busy:
        runner.start("job")
    assert busy.value.status_code == 409
    with pytest.raises(JobError):
        runner.start("other_job_that_does_not_exist")
    gate.set()
    await _wait(runner, run.run_id)
    with pytest.raises(JobError):
        runner.start("job", {"days": 9})
    with pytest.raises(JobError):
        runner.start("job", {"nope": 1})


async def test_disabled_job_cannot_be_started(settings: Any) -> None:
    async def fn(_p: dict[str, Any]) -> dict[str, Any]:
        return {}

    runner = JobRunner(settings_provider=lambda: settings)
    runner.register([_spec("off", fn, disabled=lambda s: "turned off")])
    with pytest.raises(JobError, match="turned off"):
        runner.start("off")


# -- runner: logs, progress, history ----------------------------------------


async def test_log_lines_and_progress_are_captured_including_threads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)  # production logs at INFO (main.py)
    seen: list[Any] = []

    def in_thread() -> None:
        _log.info("from the worker thread")

    async def fn(_p: dict[str, Any]) -> dict[str, Any]:
        _log.info("hello from the job")
        await asyncio.to_thread(in_thread)
        report_progress(5, 10, "half")
        seen.append(runner.get(run_id)["progress"])  # type: ignore[index]
        return {}

    runner = JobRunner()
    runner.register([_spec("logs", fn)])
    run_id = runner.start("logs").run_id
    run = await _wait(runner, run_id)
    text = "\n".join(run["log"])
    assert "hello from the job" in text
    assert "from the worker thread" in text
    assert "Job start: Logs" in text
    assert seen == [{"done": 5, "total": 10, "note": "half"}]


async def test_history_survives_restart_and_marks_interrupted_runs(tmp_path: Path) -> None:
    async def fn(_p: dict[str, Any]) -> dict[str, Any]:
        return {"updated": 2}

    path = tmp_path / "jobs.db"
    runner = JobRunner(JobHistory(path))
    runner.register([_spec("kept", fn)])
    done_id = runner.start("kept").run_id
    await _wait(runner, done_id)
    # a run the previous process never finished
    runner.history._conn.execute(
        "INSERT INTO job_runs (run_id, job_id, name, trigger, status, started_at)"
        " VALUES ('old', 'kept', 'Kept', 'schedule', 'running', '2026-01-01T00:00:00+00:00')"
    )
    runner.history._conn.commit()
    runner.history.close()

    again = JobRunner(JobHistory(path))
    again.register([_spec("kept", fn)])
    again.history._conn.execute(
        "UPDATE job_runs SET started_at = datetime('now') WHERE run_id = 'old'"
    )
    again.startup()
    assert again.get(done_id)["result"] == {"updated": 2}  # type: ignore[index]
    old = again.get("old")
    assert old is not None and old["status"] == "error" and "interrupted" in old["error"]
    assert done_id in [r["run_id"] for r in again.list_runs(job_id="kept")]

async def test_idle_runs_of_quiet_jobs_are_not_persisted() -> None:
    async def fn(_p: dict[str, Any]) -> dict[str, Any]:
        return {"fetched": 0, "stop_reason": "nothing_to_do"}

    runner = JobRunner()
    runner.register([_spec("quiet", fn, persist_idle=False)])
    run = await _wait(runner, runner.start("quiet").run_id)
    assert run["status"] == "idle"
    assert runner.history.list() == []
    assert runner.catalog()[0]["last"]["status"] == "idle"


async def test_scheduled_run_skips_while_manual_run_is_active() -> None:
    gate = asyncio.Event()

    async def fn(_p: dict[str, Any]) -> dict[str, Any]:
        await gate.wait()
        return {}

    runner = JobRunner()
    runner.register([_spec("busy", fn)])
    manual = runner.start("busy")
    await asyncio.sleep(0)
    await runner.scheduled("busy")()
    skipped = [r for r in runner.list_runs() if r["status"] == "skipped"]
    assert skipped and skipped[0]["stop_reason"] == "overlap"
    gate.set()
    await _wait(runner, manual.run_id)


def test_describe_trigger() -> None:
    assert describe_trigger(None) == "Manual only"
    assert describe_trigger({"trigger": "cron", "minute": 5}) == "Hourly at :05"
    assert describe_trigger({"trigger": "interval", "minutes": 2}) == "Every 2 min"
    assert describe_trigger({"trigger": "cron", "hour": "*/6", "minute": 15}) == "Every 6 h at :15"
    daily = describe_trigger({"trigger": "cron", "hour": 3, "minute": 0})
    assert daily == "Daily at 03:00"
    weekly = describe_trigger({"trigger": "cron", "day_of_week": "sat", "hour": 4, "minute": 30})
    assert weekly == "Weekly, Sat 04:30"


# -- job catalog ------------------------------------------------------------


def test_every_job_has_a_description_and_scheduled_jobs_are_registered(settings: Any) -> None:
    specs = build_job_specs(settings, MagicMock(), MagicMock(), MagicMock())
    assert len({s.id for s in specs}) == len(specs)
    runner = JobRunner(settings_provider=lambda: settings)
    for spec in specs:
        assert len(runner.description(spec)) > 30, spec.id
        assert spec.category in ("sync", "backfill", "export", "maintenance", "repair")
    scheduler = create_scheduler(settings, MagicMock(), MagicMock(), MagicMock(), runner)
    scheduled = {j.id for j in scheduler.get_jobs()}
    assert {"incremental_issue_sync", "queue_snapshot", "response_cache_audit"} <= scheduled
    assert "database_backup" in scheduled  # BACKUP_KEEP defaults to 3
    assert "integrity_check" not in scheduled
    assert "issue_refetch" not in scheduled
    catalog = {c["id"]: c for c in runner.catalog()}
    assert catalog["integrity_check"]["schedule"] == "Manual only"
    hour = settings.sync_cron_hour
    assert catalog["releases_prefetch"]["schedule"] == f"Daily at {hour:02d}:00"
    assert catalog["comicvine_db_export"]["disabled"] == "CV_DB_EXPORT_PATH is not set"


def _descriptions(settings: Any) -> dict[str, str]:
    runner = JobRunner(settings_provider=lambda: settings)
    return {s.id: runner.description(s)
            for s in build_job_specs(settings, MagicMock(), MagicMock(), MagicMock())}


def test_job_descriptions_show_configured_values_and_their_setting(settings: Any) -> None:
    settings = settings.model_copy(update={
        "backfill_min_year": 2015, "cover_hash_per_hour": 250, "image_cache_ttl_days": 9,
        "backup_keep": 4, "evict_older_than_years": 0, "search_cache_ttl_days": 0,
    })
    desc = _descriptions(settings)
    assert "2015 or later (BACKFILL_MIN_YEAR)" in desc["issue_detail_backfill"]
    assert "250 covers per run (COVER_HASH_PER_HOUR)" in desc["cover_hash_backfill"]
    assert "9 days (IMAGE_CACHE_TTL_DAYS)" in desc["image_cache_cleanup"]
    assert "4 copies (BACKUP_KEEP)" in desc["database_backup"]
    assert "off (EVICT_OLDER_THAN_YEARS)" in desc["evict_stale_data"]
    assert "kept forever (SEARCH_CACHE_TTL_DAYS)" in desc["evict_stale_data"]
    desc = _descriptions(settings.model_copy(update={"backfill_min_year": 0}))
    assert "any year (BACKFILL_MIN_YEAR)" in desc["issue_backfill"]


# -- backlog estimates -------------------------------------------------------


def _stored(history: JobHistory, job_id: str, started: str, result: dict[str, Any]) -> None:
    history.save(JobRun(run_id=f"{job_id}-{started}", job_id=job_id, name=job_id,
                        trigger="schedule", params={}, started_at=started, status="done",
                        finished_at=started, result=result))


async def _noop(params: dict[str, Any]) -> dict[str, Any]:
    return {}


def _backlog_runner() -> tuple[JobRunner, JobHistory]:
    history = JobHistory()
    runner = JobRunner(history)
    runner.register([
        _spec("queue_snapshot", _noop),
        _spec("vol", _noop, backlog=Backlog(count=lambda r: r.get("volumes"),
                                            done=lambda r: r["updated"])),
        _spec("plain", _noop),
    ])
    return runner, history


def test_catalog_estimates_remaining_as_snapshot_count_minus_work_done_since() -> None:
    runner, history = _backlog_runner()
    _stored(history, "vol", "2026-11-01T01:00:00", {"updated": 50})  # before the count
    _stored(history, "queue_snapshot", "2026-11-01T02:00:00", {"volumes": 400})
    _stored(history, "vol", "2026-11-01T03:00:00", {"updated": 30})
    _stored(history, "vol", "2026-11-01T04:00:00", {"updated": 20})

    catalog = {j["id"]: j for j in runner.catalog()}

    assert catalog["vol"]["remaining"] == {
        "estimate": 350, "counted": 400, "done_since": 50, "counted_at": "2026-11-01T02:00:00",
    }
    assert catalog["plain"]["remaining"] is None


def test_backlog_estimate_is_missing_without_a_count_and_never_negative() -> None:
    runner, history = _backlog_runner()
    assert {j["id"]: j for j in runner.catalog()}["vol"]["remaining"] is None
    _stored(history, "queue_snapshot", "2026-11-01T02:00:00", {"other": 1})
    assert {j["id"]: j for j in runner.catalog()}["vol"]["remaining"] is None
    _stored(history, "queue_snapshot", "2026-11-01T05:00:00", {"volumes": 10})
    _stored(history, "vol", "2026-11-01T06:00:00", {"updated": 25})
    assert {j["id"]: j for j in runner.catalog()}["vol"]["remaining"]["estimate"] == 0


def test_backfill_jobs_read_their_backlog_from_the_snapshot(settings: Any) -> None:
    specs = {s.id: s for s in build_job_specs(settings, MagicMock(), MagicMock(), MagicMock())}
    snapshot = {"issues_missing_details_in_window": 7, "issues_missing_fields_in_window": 8,
                "volumes_missing_fields": 9, "publishers_missing_fields": 10,
                "issues_missing_covers_in_window": 11}
    expected = {"issue_detail_backfill": 7, "issue_backfill": 8, "volume_backfill": 9,
                "publisher_backfill": 10, "cover_hash_backfill": 11}
    for job_id, count in expected.items():
        backlog = specs[job_id].backlog
        assert backlog is not None, job_id
        assert backlog.count(snapshot) == count, job_id
    run = {"updated": 4, "missing": 1, "queued": 9}
    assert specs["issue_detail_backfill"].backlog.done(run) == 5  # type: ignore[union-attr]
    assert specs["cover_hash_backfill"].backlog.done(  # type: ignore[union-attr]
        {"hashed": 6, "failed": 1, "issues": 3}) == 3


def test_backup_job_not_scheduled_when_keep_is_zero(settings: Any) -> None:
    off = settings.model_copy(update={"backup_keep": 0})
    scheduler = create_scheduler(off, MagicMock(), MagicMock(), MagicMock())
    assert "database_backup" not in {j.id for j in scheduler.get_jobs()}


# -- standardized backfill behaviour ----------------------------------------


async def test_volume_backfill_stops_on_rate_limit(tmp_db: Database) -> None:
    client = MagicMock()
    client.hourly_calls.return_value = 0
    client.get_volume = AsyncMock(return_value=RATE_LIMITED)
    with (
        patch.object(tmp_db, "volume_ids_needing_backfill", return_value=([1, 2], [3])),
        patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await _backfill_volumes(client, tmp_db)
    assert result["stop_reason"] == "rate_limited"
    assert client.get_volume.await_count == 1
    assert result["calendar"] == 2 and result["older"] == 1


# -- new jobs ---------------------------------------------------------------


def test_parse_id_list() -> None:
    assert parse_id_list("1, 2 4000-345\n2") == [1, 2, 345]
    with pytest.raises(ValueError):
        parse_id_list("12 abc")


async def test_refetch_issues_by_id(tmp_db: Database) -> None:
    client = MagicMock()
    client.hourly_calls.return_value = 0
    client.get = AsyncMock(return_value={
        "results": [{"id": 7001, "volume": {"id": 160294}, "name": "Fresh", "issue_number": "3",
                     "date_last_updated": "2026-10-29 01:00:00"}],
        "number_of_total_results": 1,
    })
    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        result = await _refetch_issues(client, tmp_db, [7001, 7002])
    assert result == {"requested": 2, "fetched": 1, "not_on_cv": 1}
    assert client.get.await_args.args[1]["filter"] == "id:7001|7002"
    assert tmp_db.get_issue(7001) is not None


async def test_refetch_issues_stops_when_rate_limited(tmp_db: Database) -> None:
    client = MagicMock()
    client.hourly_calls.return_value = 0
    client.get = AsyncMock(return_value=RATE_LIMITED)
    result = await _refetch_issues(client, tmp_db, [1], details=True)
    assert result["stop_reason"] == "rate_limited"


async def test_refetch_counts_ids_comicvine_does_not_have(tmp_db: Database) -> None:
    client = MagicMock()
    client.hourly_calls.return_value = 0
    client.get = AsyncMock(return_value=NOT_FOUND)
    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        result = await _refetch_issues(client, tmp_db, [5, 6])
    assert result == {"requested": 2, "fetched": 0, "not_on_cv": 2}


def test_response_cache_audit_reports_and_purges(tmp_db: Database) -> None:
    ok = json.dumps({"status_code": 1, "error": "OK", "results": {"id": 1}})
    rows = [
        ("issue", 1, None, ok),
        ("issue", 2, None, json.dumps({"status_code": 107, "error": "Rate limit", "results": []})),
        ("volume", 3, None, "not json"),
        ("search", None, "h1", json.dumps({"status_code": 1, "error": "OK", "results": []})),
        ("negative:search", None, "h2", "[]"),
    ]
    tmp_db.conn.executemany(
        "INSERT INTO cv_response_cache (resource_type, resource_id, params_hash, data_json)"
        " VALUES (?, ?, ?, ?)", rows,
    )
    tmp_db.conn.commit()
    report = tmp_db.audit_response_cache(purge=False)
    assert report["by_type"] == {
        "issue/error_envelope": 1, "volume/unparseable": 1, "search/empty_results": 1,
    }
    assert report["purgeable"] == 2 and report["deleted"] == 0
    assert tmp_db.audit_response_cache(purge=True)["deleted"] == 2
    left = {r[0] for r in tmp_db.conn.execute("SELECT resource_id FROM cv_response_cache")}
    assert 2 not in left and 3 not in left and 1 in left


def test_queue_snapshot_and_quick_check(tmp_db: Database) -> None:
    snap = tmp_db.queue_snapshot()
    for key in ("issues_missing_details", "issues_missing_fields", "volumes_missing_fields",
                "publishers_missing_fields", "pending_fetch", "response_cache", "db_bytes"):
        assert key in snap
    assert tmp_db.quick_check() == {"ok": True, "problems": []}


def test_rebuild_fts_and_snapshot_reads_use_own_connections(tmp_db: Database) -> None:
    result = tmp_db.rebuild_fts()
    assert result["after"]["volume_fts"] >= 2
    assert 160294 in tmp_db.existing_ids_snapshot("cv_volume")


def test_issue_backfill_ids_read_on_own_connection(tmp_db: Database) -> None:
    from cvproxy.scheduler import _issue_backfill_ids

    tmp_db.conn.execute(
        "UPDATE cv_issue SET date_last_updated = '2026-01-01 00:00:00' WHERE id = 1073108"
    )
    tmp_db.conn.commit()
    assert _issue_backfill_ids(tmp_db) == [1073109]


def test_backfill_window_limits_issue_detail_and_cover_selection(tmp_db: Database) -> None:
    from cvproxy.scheduler import _issue_backfill_ids

    for iid, store, cover in [(9101, "2005-03-01", None), (9102, None, "2015-06-01"),
                              (9103, None, None), (9104, "2024-01-10", "2023-12-01")]:
        tmp_db.upsert_issue({"id": iid, "volume": {"id": 160294}, "name": f"I{iid}",
                             "issue_number": "1", "store_date": store, "cover_date": cover,
                             "image": {"original_url": f"https://x/{iid}.jpg"}})
    tmp_db.conn.execute("UPDATE cv_issue SET date_last_updated = '2026-01-01 00:00:00'"
                        " WHERE id < 9000000 AND id NOT BETWEEN 9101 AND 9104")
    tmp_db.conn.commit()
    picked = _issue_backfill_ids(tmp_db, 2010)
    assert set(picked) == {9102, 9103, 9104}
    assert picked[0] == 9104  # dated by store date, newest first
    assert set(_issue_backfill_ids(tmp_db, 0)) >= {9101, 9102, 9103, 9104}
    detail = tmp_db.issue_ids_needing_detail(100, 2010)
    assert 9101 not in detail and {9102, 9103, 9104} <= set(detail)
    covers = {cvid for cvid, _ in tmp_db.cover_hash_candidates(100, 2010)}
    assert 9101 not in covers and {9102, 9103, 9104} <= covers
    snap = tmp_db.queue_snapshot(2010)
    assert snap["issues_missing_fields_in_window"] == 3
    assert snap["issues_missing_fields"] >= 4


def test_disabled_backup_reason_names_only_the_setting(settings: Any) -> None:
    from cvproxy.scheduler import _no_backups

    reason = _no_backups(settings.model_copy(update={"backup_keep": 0}))
    assert reason == "BACKUP_KEEP is 0"
