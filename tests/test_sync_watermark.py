"""Tests for the watermark-based incremental issue sync and its supporting DB helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from cvproxy.cv_client import NOT_FOUND, RATE_LIMITED
from cvproxy.db import Database
from cvproxy.scheduler import (
    _CV_TS,
    _MISFIRE_GRACE_SECONDS,
    _SYNC_OVERLAP,
    ISSUE_SYNC_WATERMARK,
    _sync_issues_incremental,
    _sync_window,
    create_scheduler,
)


@pytest.fixture(autouse=True)
def _test_settings(settings: Any) -> Any:
    """The syncs read BACKFILL_MIN_YEAR and the publisher lists through get_settings()."""
    return settings


def _issue(iid: int, updated: str) -> dict[str, Any]:
    return {
        "id": iid, "volume": {"id": 1}, "issue_number": str(iid), "name": f"#{iid}",
        "date_last_updated": updated,
    }


class FakeCV:
    """Serves /issues/ date_last_updated windows from a fixed set of issues.

    *fail_from* makes every request whose slice starts at or after that time
    return None (an upstream error), like ComicVine's 502s.
    """

    def __init__(self, issues: list[dict[str, Any]], fail_from: str | None = None) -> None:
        self.issues = sorted(issues, key=lambda i: i["date_last_updated"])
        self.fail_from = fail_from
        self.calls: list[dict[str, Any]] = []

    async def get(self, endpoint: str, params: dict[str, Any]) -> Any:
        self.calls.append(params)
        lo, hi = params["filter"].removeprefix("date_last_updated:").split("|")
        if self.fail_from is not None and lo >= self.fail_from:
            return None
        hits = [i for i in self.issues if lo <= i["date_last_updated"] <= hi]
        if not hits:
            return NOT_FOUND
        page = hits[params["offset"]:params["offset"] + params["limit"]]
        return {"status_code": 1, "results": page, "number_of_total_results": len(hits)}


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _recent(hours_ago: float) -> str:
    return (_utcnow() - timedelta(hours=hours_ago)).strftime(_CV_TS)


async def test_first_run_stores_issues_and_sets_watermark(tmp_db: Database) -> None:
    newest = _recent(2)
    cv = FakeCV([_issue(9001, _recent(30)), _issue(9002, newest)])

    result = await _sync_issues_incremental(cv, tmp_db)  # type: ignore[arg-type]

    assert result == {"issues_upserted": 2, "watermark": newest, "complete": True}
    assert tmp_db.get_sync_watermark(ISSUE_SYNC_WATERMARK) == newest
    assert tmp_db.get_issue(9001) is not None


async def test_next_run_starts_one_overlap_before_watermark(tmp_db: Database) -> None:
    mark = _recent(5)
    tmp_db.set_sync_watermark(ISSUE_SYNC_WATERMARK, mark)
    cv = FakeCV([])

    await _sync_issues_incremental(cv, tmp_db)  # type: ignore[arg-type]

    first_lo = cv.calls[0]["filter"].removeprefix("date_last_updated:").split("|")[0]
    expected = (datetime.strptime(mark, _CV_TS) - _SYNC_OVERLAP).strftime(_CV_TS)
    assert first_lo == expected
    # Nothing new: the watermark stays where it was
    assert tmp_db.get_sync_watermark(ISSUE_SYNC_WATERMARK) == mark


async def test_failed_run_keeps_progress_and_next_run_fills_the_gap(tmp_db: Database) -> None:
    """The outage case: a 5xx mid-backlog must not leave a permanent hole."""
    start = _utcnow() - timedelta(days=4)
    tmp_db.set_sync_watermark(ISSUE_SYNC_WATERMARK, start.strftime(_CV_TS))
    early = (start + timedelta(hours=6)).strftime(_CV_TS)
    late = (start + timedelta(days=2, hours=6)).strftime(_CV_TS)
    issues = [_issue(9101, early), _issue(9102, late)]

    # Day 1 succeeds, everything from day 2 on fails
    fail_from = (start + timedelta(hours=23)).strftime(_CV_TS)
    first = await _sync_issues_incremental(FakeCV(issues, fail_from=fail_from), tmp_db)  # type: ignore[arg-type]
    assert first["complete"] is False
    assert first["watermark"] == early
    assert tmp_db.get_issue(9102) is None

    second = await _sync_issues_incremental(FakeCV(issues), tmp_db)  # type: ignore[arg-type]
    assert second["complete"] is True
    assert tmp_db.get_issue(9102) is not None
    assert tmp_db.get_sync_watermark(ISSUE_SYNC_WATERMARK) == late


async def test_failure_before_any_data_leaves_watermark_unset(tmp_db: Database) -> None:
    cv = FakeCV([_issue(9201, _recent(3))], fail_from="0000")

    result = await _sync_issues_incremental(cv, tmp_db)  # type: ignore[arg-type]

    assert result["complete"] is False
    assert tmp_db.get_sync_watermark(ISSUE_SYNC_WATERMARK) is None


async def test_rate_limit_stops_the_window(tmp_db: Database) -> None:
    cv = MagicMock()

    async def _get(endpoint: str, params: dict[str, Any]) -> Any:
        return RATE_LIMITED

    cv.get = _get
    now = _utcnow()
    upserted, latest, complete = await _sync_window(cv, tmp_db, now - timedelta(days=1), now)
    assert (upserted, latest, complete) == (0, None, False)


async def test_window_is_walked_in_daily_slices(tmp_db: Database) -> None:
    now = _utcnow()
    cv = FakeCV([_issue(9300 + d, (now - timedelta(days=d, hours=1)).strftime(_CV_TS))
                 for d in range(5)])

    upserted, _latest, complete = await _sync_window(
        cv, tmp_db, now - timedelta(days=5), now  # type: ignore[arg-type]
    )

    assert complete is True
    assert upserted == 5
    assert len(cv.calls) == 5


async def test_max_chunks_bounds_a_run(tmp_db: Database) -> None:
    now = _utcnow()
    cv = FakeCV([])
    _u, _l, complete = await _sync_window(
        cv, tmp_db, now - timedelta(days=10), now, max_chunks=3  # type: ignore[arg-type]
    )
    assert complete is False
    assert len(cv.calls) == 3


def test_scheduler_runs_sync_hourly_with_misfire_grace(settings: Any) -> None:
    scheduler = create_scheduler(settings, MagicMock(), MagicMock(), MagicMock())
    job = scheduler.get_job("incremental_issue_sync")
    assert job is not None
    assert str(job.trigger.fields[job.trigger.FIELD_NAMES.index("minute")]) == "10"
    assert str(job.trigger.fields[job.trigger.FIELD_NAMES.index("hour")]) == "*"
    # job_defaults apply when the started scheduler adds the job; check them directly
    assert scheduler._job_defaults["misfire_grace_time"] == _MISFIRE_GRACE_SECONDS


@pytest.mark.parametrize("missing", [None, 1])
def test_volume_ids_needing_backfill(tmp_db: Database, missing: int | None) -> None:
    filled = {"id": 7001, "name": "Filled", "date_last_updated": "2026-01-01 00:00:00",
              "characters": [{"id": 1, "name": "Hero"}]}  # fetched from /volume/
    for vol in (
        {"id": 7002, "name": "Old stale"},
        {"id": 7003, "name": "Dated stale"},
        {"id": 7004, "name": "Dated stale newer"},
        filled,
    ):
        tmp_db.upsert_volume(vol)
    for iid, vid, date in ((7103, 7003, "2026-01-01"), (7104, 7004, "2026-06-01")):
        tmp_db.upsert_issue({"id": iid, "volume": {"id": vid}, "store_date": date})
    if missing:
        tmp_db.mark_backfill_missing("cv_volume", 7002)

    calendar, old = tmp_db.volume_ids_needing_backfill(calendar_limit=10, old_limit=10)

    # Fixture volumes may also qualify; ours must be ordered by latest store_date
    assert [v for v in calendar if v in (7003, 7004)] == [7004, 7003]
    assert 7002 not in calendar
    assert 7001 not in calendar + old
    assert (7002 in old) is (missing is None)


def test_count_rows_snapshot_matches_get_counts(tmp_db: Database) -> None:
    assert tmp_db.count_rows_snapshot() == tmp_db.get_counts()


def test_checkpoint_wal_reports_frames(tmp_db: Database) -> None:
    tmp_db.upsert_publisher({"id": 99, "name": "Checkpoint Press"})
    result = tmp_db.checkpoint_wal()
    assert result["busy"] == 0
    assert result["checkpointed"] == result["log_frames"]


def test_wal_checkpoint_job_registered(settings: Any) -> None:
    scheduler = create_scheduler(settings, MagicMock(), MagicMock(), MagicMock())
    assert scheduler.get_job("wal_checkpoint") is not None
