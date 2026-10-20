"""Tests for scheduler backfill pacing logic."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from cvproxy.db import Database
from cvproxy.scheduler import (
    _BACKFILL_HOURLY_BUDGET,
    _BACKFILL_MAX_SLEEP,
    _BACKFILL_MIN_SLEEP,
    _backfill_call_interval,
    create_scheduler,
)


def _mock_client(used: int) -> MagicMock:
    client = MagicMock()
    client.hourly_calls.return_value = used
    return client


def test_interval_is_clamped_above_minimum() -> None:
    """Even with no budget consumed at the start of the hour the interval is >= min."""
    client = _mock_client(used=0)
    result = _backfill_call_interval(client, "issue/")
    assert result >= _BACKFILL_MIN_SLEEP


def test_interval_is_clamped_below_maximum() -> None:
    """When nearly all budget is consumed the interval never exceeds the cap."""
    # Only 1 call left in the budget → secs_left / 1 could be up to 3600.
    client = _mock_client(used=_BACKFILL_HOURLY_BUDGET - 1)
    result = _backfill_call_interval(client, "issue/")
    assert result <= _BACKFILL_MAX_SLEEP


def test_interval_increases_as_budget_is_consumed() -> None:
    """With fewer remaining calls the interval between calls should grow."""
    interval_with_full_budget = _backfill_call_interval(_mock_client(used=0), "issue/")
    half_budget_client = _mock_client(used=_BACKFILL_HOURLY_BUDGET // 2)
    interval_with_half_budget = _backfill_call_interval(half_budget_client, "issue/")
    # Can't assert strict ordering because both might be clamped at min/max,
    # but at least the half-budget interval must be >= full-budget interval.
    assert interval_with_half_budget >= interval_with_full_budget


def test_interval_when_budget_exhausted_returns_max_sleep() -> None:
    """When the full budget is consumed (used >= threshold) remaining_budget clamps to 1."""
    client = _mock_client(used=_BACKFILL_HOURLY_BUDGET)
    result = _backfill_call_interval(client, "issue/")
    # remaining_budget = max(1, budget - budget) = 1 → secs_left / 1 is large → capped at max.
    assert result == _BACKFILL_MAX_SLEEP


def test_eviction_job_registered() -> None:
    """create_scheduler registers an 'evict_stale_data' job."""
    from unittest.mock import MagicMock

    from cvproxy.config import Settings

    settings = Settings(cv_api_key="testkey", sync_enabled=True)
    image_cache = MagicMock()
    db = MagicMock()
    client = MagicMock()

    scheduler = create_scheduler(settings, image_cache, db, client)
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert "evict_stale_data" in job_ids


# ---------------------------------------------------------------------------
# Publisher backfill budget guard
# ---------------------------------------------------------------------------


async def test_publisher_backfill_pauses_when_budget_exceeded() -> None:
    """_backfill_publishers must not call upstream when hourly budget is already exceeded."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from cvproxy.scheduler import _BACKFILL_HOURLY_BUDGET, _backfill_publishers

    db = MagicMock()
    db.pending_waiting.return_value = False
    db.conn.execute.return_value.fetchall.return_value = [(1,), (2,), (3,)]

    client = MagicMock()
    client.hourly_calls.return_value = _BACKFILL_HOURLY_BUDGET + 1
    client.get_publisher = AsyncMock(return_value={"results": {"id": 1, "name": "DC"}})

    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        await _backfill_publishers(client, db)

    client.get_publisher.assert_not_called()


async def test_publisher_backfill_calls_upstream_within_budget() -> None:
    """_backfill_publishers fetches publishers and sleeps between calls."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from cvproxy.scheduler import _backfill_publishers

    db = MagicMock()
    db.pending_waiting.return_value = False
    db.conn.execute.return_value.fetchall.return_value = [(42,)]

    client = MagicMock()
    client.hourly_calls.return_value = 0
    client.get_publisher = AsyncMock(return_value={"results": {"id": 42, "name": "Marvel"}})

    sleep_mock = AsyncMock()
    with patch("cvproxy.scheduler.asyncio.sleep", sleep_mock):
        await _backfill_publishers(client, db)

    client.get_publisher.assert_called_once_with(42)
    db.upsert_publisher.assert_called_once()
    sleep_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Issue detail backfill
# ---------------------------------------------------------------------------


def _thin_issue(iid: int) -> dict:
    return {"id": iid, "volume": {"id": 1}, "issue_number": str(iid), "name": f"#{iid}"}


def _detail_payload(iid: int) -> dict:
    return {
        "status_code": 1, "error": "OK",
        "results": {
            "id": iid, "volume": {"id": 1}, "issue_number": str(iid), "name": f"#{iid}",
            "person_credits": [{"id": 9, "name": "Artist"}], "character_credits": [],
            "team_credits": [], "location_credits": [], "story_arc_credits": [],
        },
    }


def _detail_client(responses: dict) -> MagicMock:
    from unittest.mock import AsyncMock

    client = MagicMock()
    client.hourly_calls.return_value = 0
    client.get_issue = AsyncMock(side_effect=lambda iid: responses[iid])
    return client


async def test_issue_detail_backfill_fills_list_only_rows(tmp_db: Database) -> None:
    from unittest.mock import AsyncMock, patch

    from cvproxy.cv_client import NOT_FOUND
    from cvproxy.scheduler import _backfill_issue_details

    for iid in (8001, 8002):
        tmp_db.upsert_issue(_thin_issue(iid))
    client = _detail_client({8002: _detail_payload(8002), 8001: NOT_FOUND})

    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        result = await _backfill_issue_details(client, tmp_db)

    assert result == {"updated": 1, "queued": 2}
    assert tmp_db.issue_needs_detail(8002) is False
    issue = tmp_db.get_issue(8002)
    assert issue is not None
    assert issue["person_credits"] == [{"id": 9, "name": "Artist"}]
    # Not found upstream: skipped on later runs
    assert 8001 not in tmp_db.issue_ids_needing_detail(limit=10)


async def test_issue_detail_backfill_stops_when_rate_limited(tmp_db: Database) -> None:
    from unittest.mock import AsyncMock, patch

    from cvproxy.cv_client import RATE_LIMITED
    from cvproxy.scheduler import _backfill_issue_details

    for iid in (8101, 8102):
        tmp_db.upsert_issue(_thin_issue(iid))
    client = _detail_client({8102: RATE_LIMITED, 8101: _detail_payload(8101)})

    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        await _backfill_issue_details(client, tmp_db)

    client.get_issue.assert_called_once_with(8102)
    assert tmp_db.issue_needs_detail(8101) is True


async def test_issue_detail_backfill_pauses_when_budget_exceeded(tmp_db: Database) -> None:
    from unittest.mock import AsyncMock, patch

    from cvproxy.scheduler import _backfill_issue_details

    tmp_db.upsert_issue(_thin_issue(8201))
    client = _detail_client({})
    client.hourly_calls.return_value = _BACKFILL_HOURLY_BUDGET + 1

    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        await _backfill_issue_details(client, tmp_db)

    client.get_issue.assert_not_called()


def test_issue_detail_backfill_job_registered() -> None:
    from cvproxy.config import Settings

    settings = Settings(cv_api_key="testkey", sync_enabled=True)
    scheduler = create_scheduler(settings, MagicMock(), MagicMock(), MagicMock())
    assert "issue_detail_backfill" in {job.id for job in scheduler.get_jobs()}


# ---------------------------------------------------------------------------
# Database backup job
# ---------------------------------------------------------------------------


async def test_backup_job_writes_snapshot_and_keeps_newest(
    tmp_db: Database, tmp_path: Path
) -> None:
    import os

    from cvproxy.config import Settings
    from cvproxy.scheduler import _backup_database

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for day in range(1, 5):
        old = backup_dir / f"localcv-2026010{day}-030000.db"
        old.write_bytes(b"old")
        os.utime(old, (day * 86400, day * 86400))
    unrelated = backup_dir / "notes.txt"
    unrelated.write_text("keep me")
    settings = Settings(cv_api_key="k", backup_dir=backup_dir, backup_keep=3)

    result = await _backup_database(tmp_db, settings)

    kept = sorted(p.name for p in backup_dir.glob("localcv-*.db"))
    assert len(kept) == 3
    assert result["backup"] in kept
    assert "localcv-20260104-030000.db" in kept  # newest old one survives
    assert "localcv-20260101-030000.db" not in kept
    assert unrelated.exists()
    assert result["removed"] == 2


async def test_backup_job_disabled_when_keep_is_zero(tmp_db: Database, tmp_path: Path) -> None:
    from cvproxy.config import Settings
    from cvproxy.scheduler import _backup_database

    settings = Settings(cv_api_key="k", backup_dir=tmp_path / "backups", backup_keep=0)

    result = await _backup_database(tmp_db, settings)

    assert result == {"backup": None, "removed": 0}
    assert not (tmp_path / "backups").exists()


def test_backup_job_registered() -> None:
    from cvproxy.config import Settings

    settings = Settings(cv_api_key="testkey", sync_enabled=True)
    scheduler = create_scheduler(settings, MagicMock(), MagicMock(), MagicMock())
    assert "database_backup" in {job.id for job in scheduler.get_jobs()}


async def test_issue_detail_backfill_ignores_other_endpoints_usage(tmp_db: Database) -> None:
    """Busy /search/ or /volume/ budgets must not stop the /issue/ backfill."""
    from unittest.mock import AsyncMock, patch

    from cvproxy.scheduler import _backfill_issue_details

    tmp_db.upsert_issue(_thin_issue(8301))
    client = _detail_client({8301: _detail_payload(8301)})
    client.hourly_calls.side_effect = lambda endpoint: 0 if endpoint.startswith("issue") else 999
    client.total_hourly_calls.return_value = 999

    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        await _backfill_issue_details(client, tmp_db)

    client.get_issue.assert_called_once_with(8301)
    client.hourly_calls.assert_any_call("issue/")


# ---------------------------------------------------------------------------
# Pending fetches come before backfill
# ---------------------------------------------------------------------------


async def test_backfill_waits_while_requests_are_queued_for_its_endpoint(
    tmp_db: Database,
) -> None:
    from unittest.mock import AsyncMock, patch

    from cvproxy.scheduler import _backfill_issue_details

    tmp_db.upsert_issue(_thin_issue(8401))
    tmp_db.pending_add("issue", "999", {"id": 999})
    client = _detail_client({8401: _detail_payload(8401)})

    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        await _backfill_issue_details(client, tmp_db)

    client.get_issue.assert_not_called()


def test_pending_fetch_job_registered() -> None:
    from cvproxy.config import Settings

    settings = Settings(cv_api_key="testkey", sync_enabled=True)
    scheduler = create_scheduler(settings, MagicMock(), MagicMock(), MagicMock())
    assert "pending_fetch" in {job.id for job in scheduler.get_jobs()}


# ---------------------------------------------------------------------------
# comicvine.db export job
# ---------------------------------------------------------------------------


async def test_export_job_writes_file(tmp_db: Database, tmp_path: Path) -> None:
    from cvproxy.config import Settings
    from cvproxy.scheduler import _export_comicvine_db

    dest = tmp_path / "export" / "comicvine.db"
    settings = Settings(cv_api_key="k", cv_db_export_path=dest)

    result = await _export_comicvine_db(tmp_db, settings)

    assert dest.exists()
    assert result["path"] == str(dest)
    assert result["cv_issue"] == 2


async def test_export_job_disabled_without_path(tmp_db: Database) -> None:
    from cvproxy.config import Settings
    from cvproxy.scheduler import _export_comicvine_db

    result = await _export_comicvine_db(tmp_db, Settings(cv_api_key="k"))

    assert result == {"path": None}


def test_export_job_registered_only_when_configured(tmp_path: Path) -> None:
    from cvproxy.config import Settings

    plain = create_scheduler(
        Settings(cv_api_key="k", sync_enabled=True), MagicMock(), MagicMock(), MagicMock()
    )
    exporting = create_scheduler(
        Settings(cv_api_key="k", sync_enabled=True, cv_db_export_path=tmp_path / "cv.db"),
        MagicMock(), MagicMock(), MagicMock(),
    )
    assert "comicvine_db_export" not in {job.id for job in plain.get_jobs()}
    assert "comicvine_db_export" in {job.id for job in exporting.get_jobs()}


async def test_export_job_builds_on_local_disk_next_to_database(
    tmp_db: Database, tmp_path: Path
) -> None:
    from unittest.mock import patch

    from cvproxy.config import Settings
    from cvproxy.scheduler import _export_comicvine_db

    dest = tmp_path / "export" / "comicvine.db"
    settings = Settings(cv_api_key="k", cv_db_export_path=dest, db_path=tmp_path / "test.db")

    with patch.object(tmp_db, "export_comicvine_db", wraps=tmp_db.export_comicvine_db) as spy:
        await _export_comicvine_db(tmp_db, settings)

    assert spy.call_args.kwargs["staging_dir"] == tmp_path


def test_mcl_refresh_job_registered_only_when_configured(tmp_path: Path) -> None:
    from cvproxy.config import Settings

    plain = create_scheduler(
        Settings(cv_api_key="k", sync_enabled=True), MagicMock(), MagicMock(), MagicMock()
    )
    refreshing = create_scheduler(
        Settings(cv_api_key="k", sync_enabled=True, mcl_export_path=tmp_path / "latest.mcl"),
        MagicMock(), MagicMock(), MagicMock(),
    )
    assert "mcl_refresh" not in {job.id for job in plain.get_jobs()}
    assert "mcl_refresh" in {job.id for job in refreshing.get_jobs()}


# ---------------------------------------------------------------------------
# MCL gap backfill
# ---------------------------------------------------------------------------


def _mcl_settings(tmp_path: Path, body: str) -> Any:
    from cvproxy.config import Settings

    path = tmp_path / "latest.mcl"
    path.write_text(body)
    return Settings(cv_api_key="k", mcl_export_path=path)


def _gap_client(responses: dict[str, Any]) -> MagicMock:
    from unittest.mock import AsyncMock

    client = MagicMock()
    client.hourly_calls.return_value = 0
    client.get = AsyncMock(side_effect=lambda endpoint, params: responses[endpoint])
    return client


async def test_mcl_gap_backfill_fetches_missing_volumes_then_issues(
    tmp_db: Database, tmp_path: Path
) -> None:
    from unittest.mock import AsyncMock, patch

    from cvproxy.scheduler import _backfill_from_mcl

    # 160294 is local (1073108 stored, 999001 not); 555555 isn't local at all
    settings = _mcl_settings(
        tmp_path, "Missing;2026-10-20\n160294;1073108,999001;1,7\n555555;999002;1\n"
    )
    client = _gap_client({
        "volumes/": {"status_code": 1, "results": [{"id": 555555, "name": "New Volume"}]},
        "issues/": {"status_code": 1, "results": [
            {"id": 999001, "volume": {"id": 160294}, "issue_number": "7"},
            {"id": 999002, "volume": {"id": 555555}, "issue_number": "1"},
        ]},
    })

    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        result = await _backfill_from_mcl(client, tmp_db, settings)

    assert result == {"volumes_missing": 1, "volumes_added": 1,
                      "issues_missing": 2, "issues_added": 2}
    endpoints = [c.args[0] for c in client.get.call_args_list]
    assert endpoints == ["volumes/", "issues/"]
    assert client.get.call_args_list[0].args[1]["filter"] == "id:555555"
    assert client.get.call_args_list[1].args[1]["filter"] == "id:999002|999001"
    assert tmp_db.get_volume(555555) is not None
    assert tmp_db.get_issue(999001) is not None


async def test_mcl_gap_backfill_batches_100_ids_per_call(
    tmp_db: Database, tmp_path: Path
) -> None:
    from unittest.mock import AsyncMock, patch

    from cvproxy.scheduler import _backfill_from_mcl

    ids = ",".join(str(900_000 + i) for i in range(150))
    settings = _mcl_settings(tmp_path, f"160294;{ids};{','.join(['1'] * 150)}\n")
    client = _gap_client({"issues/": {"status_code": 1, "results": []}})

    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        await _backfill_from_mcl(client, tmp_db, settings)

    sizes = [len(c.args[1]["filter"].removeprefix("id:").split("|"))
             for c in client.get.call_args_list]
    assert sizes == [100, 50]


async def test_mcl_gap_backfill_stops_when_rate_limited(tmp_db: Database, tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, patch

    from cvproxy.cv_client import RATE_LIMITED
    from cvproxy.scheduler import _backfill_from_mcl

    settings = _mcl_settings(tmp_path, "555555;999002;1\n555556;999003;1\n")
    client = _gap_client({"volumes/": RATE_LIMITED, "issues/": {"status_code": 1, "results": []}})

    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        await _backfill_from_mcl(client, tmp_db, settings)

    assert [c.args[0] for c in client.get.call_args_list] == ["volumes/"]


async def test_mcl_gap_backfill_waits_for_queued_client_requests(
    tmp_db: Database, tmp_path: Path
) -> None:
    from unittest.mock import AsyncMock, patch

    from cvproxy.scheduler import _backfill_from_mcl

    settings = _mcl_settings(tmp_path, "555555;999002;1\n")
    tmp_db.pending_add("issues_filter", "k", {"filter": "volume:1", "negative_key": None})
    client = _gap_client({"volumes/": {"status_code": 1, "results": []}})

    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        await _backfill_from_mcl(client, tmp_db, settings)

    assert [c.args[0] for c in client.get.call_args_list] == ["volumes/"]


async def test_mcl_gap_backfill_without_file_does_nothing(tmp_db: Database) -> None:
    from cvproxy.config import Settings
    from cvproxy.scheduler import _backfill_from_mcl

    client = _gap_client({})
    result = await _backfill_from_mcl(client, tmp_db, Settings(cv_api_key="k"))

    assert result == {"volumes_missing": 0, "volumes_added": 0,
                      "issues_missing": 0, "issues_added": 0}
    client.get.assert_not_called()


def test_cover_hash_job_registered_only_when_enabled() -> None:
    from cvproxy.config import Settings

    off = create_scheduler(
        Settings(cv_api_key="k", sync_enabled=True, cover_hash_per_hour=0),
        MagicMock(), MagicMock(), MagicMock(),
    )
    on = create_scheduler(
        Settings(cv_api_key="k", sync_enabled=True, cover_hash_per_hour=600),
        MagicMock(), MagicMock(), MagicMock(),
    )
    assert "cover_hash_backfill" not in {job.id for job in off.get_jobs()}
    assert "cover_hash_backfill" in {job.id for job in on.get_jobs()}
