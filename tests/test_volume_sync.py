"""Incremental volume sync: volumes ComicVine changed after cvproxy stored them."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from cvproxy.db import Database
from cvproxy.scheduler import (
    _CV_TS,
    ISSUE_SYNC_WATERMARK,
    VOLUME_SYNC_WATERMARK,
    _rewind_volume_sync,
    _sync_volumes_incremental,
    create_scheduler,
)
from tests.test_sync_watermark import FakeCV, _recent, _utcnow


def _volume(vid: int, updated: str, publisher: int = 1, **extra: Any) -> dict[str, Any]:
    return {"id": vid, "name": f"Volume {vid}", "publisher": {"id": publisher, "name": "P"},
            "count_of_issues": 3, "date_last_updated": updated, **extra}


async def test_volume_sync_stores_changed_volumes_and_keeps_its_own_watermark(
    tmp_db: Database,
) -> None:
    newest = _recent(2)
    cv = FakeCV([_volume(8001, _recent(30)), _volume(160294, newest, name="Renamed")])

    result = await _sync_volumes_incremental(cv, tmp_db)  # type: ignore[arg-type]

    assert result == {"volumes_upserted": 2, "watermark": newest, "complete": True}
    assert {c["filter"].split(":")[0] for c in cv.calls} == {"date_last_updated"}
    assert tmp_db.get_sync_watermark(VOLUME_SYNC_WATERMARK) == newest
    assert tmp_db.get_sync_watermark(ISSUE_SYNC_WATERMARK) is None
    assert tmp_db.get_volume(160294)["name"] == "Renamed"  # type: ignore[index]
    assert tmp_db.get_volume(8001) is not None


async def test_volume_sync_asks_the_volumes_endpoint(tmp_db: Database) -> None:
    endpoints: list[str] = []
    cv = FakeCV([])
    original = cv.get

    async def get(endpoint: str, params: dict[str, Any]) -> Any:
        endpoints.append(endpoint)
        return await original(endpoint, params)

    cv.get = get  # type: ignore[method-assign]
    await _sync_volumes_incremental(cv, tmp_db)  # type: ignore[arg-type]
    assert endpoints and set(endpoints) == {"volumes/"}


async def test_volume_sync_skips_blocked_publishers(tmp_db: Database) -> None:
    tmp_db.set_publisher_list(900, "block")
    cv = FakeCV([_volume(8101, _recent(3), publisher=900)])
    await _sync_volumes_incremental(cv, tmp_db)  # type: ignore[arg-type]
    assert tmp_db.get_volume(8101) is None


def test_list_rows_keep_a_volumes_detail_fields(tmp_db: Database) -> None:
    tmp_db.upsert_volume(_volume(8201, "2026-01-01 00:00:00",
                                 characters=[{"id": 1, "name": "Hero"}]))
    tmp_db.upsert_volume(_volume(8201, "2026-02-01 00:00:00", name="New name"))
    stored = tmp_db.get_volume(8201)
    assert stored is not None
    assert stored["name"] == "New name"
    assert stored["characters"] == [{"id": 1, "name": "Hero"}]


def test_rewind_moves_the_volume_watermark_back_but_never_forward(tmp_db: Database) -> None:
    result = _rewind_volume_sync(tmp_db, days=730)
    mark = tmp_db.get_sync_watermark(VOLUME_SYNC_WATERMARK)
    assert mark == result["watermark"]
    assert abs(datetime.strptime(mark, _CV_TS) - (_utcnow() - timedelta(days=730))) \
        < timedelta(minutes=1)
    _rewind_volume_sync(tmp_db, days=1)
    assert tmp_db.get_sync_watermark(VOLUME_SYNC_WATERMARK) == mark


def test_volume_sync_runs_hourly(settings: Any) -> None:
    scheduler = create_scheduler(settings, MagicMock(), MagicMock(), MagicMock())
    job = scheduler.get_job("incremental_volume_sync")
    assert job is not None
    assert str(job.trigger.fields[job.trigger.FIELD_NAMES.index("hour")]) == "*"
