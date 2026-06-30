"""Tests for scheduler backfill pacing logic."""

from __future__ import annotations

from unittest.mock import MagicMock

from cvproxy.scheduler import (
    _BACKFILL_HOURLY_BUDGET,
    _BACKFILL_MAX_SLEEP,
    _BACKFILL_MIN_SLEEP,
    _backfill_call_interval,
    create_scheduler,
)


def _mock_client(used: int) -> MagicMock:
    client = MagicMock()
    client.total_hourly_calls.return_value = used
    return client


def test_interval_is_clamped_above_minimum() -> None:
    """Even with no budget consumed at the start of the hour the interval is >= min."""
    client = _mock_client(used=0)
    result = _backfill_call_interval(client)
    assert result >= _BACKFILL_MIN_SLEEP


def test_interval_is_clamped_below_maximum() -> None:
    """When nearly all budget is consumed the interval never exceeds the cap."""
    # Only 1 call left in the budget → secs_left / 1 could be up to 3600.
    client = _mock_client(used=_BACKFILL_HOURLY_BUDGET - 1)
    result = _backfill_call_interval(client)
    assert result <= _BACKFILL_MAX_SLEEP


def test_interval_increases_as_budget_is_consumed() -> None:
    """With fewer remaining calls the interval between calls should grow."""
    interval_with_full_budget = _backfill_call_interval(_mock_client(used=0))
    interval_with_half_budget = _backfill_call_interval(_mock_client(used=_BACKFILL_HOURLY_BUDGET // 2))
    # Can't assert strict ordering because both might be clamped at min/max,
    # but at least the half-budget interval must be >= full-budget interval.
    assert interval_with_half_budget >= interval_with_full_budget


def test_interval_when_budget_exhausted_returns_max_sleep() -> None:
    """When the full budget is consumed (used >= threshold) remaining_budget clamps to 1."""
    client = _mock_client(used=_BACKFILL_HOURLY_BUDGET)
    result = _backfill_call_interval(client)
    # remaining_budget = max(1, 149-149) = 1 → secs_left / 1 is large → capped at max.
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
    db.conn.execute.return_value.fetchall.return_value = [(1,), (2,), (3,)]

    client = MagicMock()
    client.total_hourly_calls.return_value = _BACKFILL_HOURLY_BUDGET + 1
    client.get_publisher = AsyncMock(return_value={"results": {"id": 1, "name": "DC"}})

    with patch("cvproxy.scheduler.asyncio.sleep", new_callable=AsyncMock):
        await _backfill_publishers(client, db)

    client.get_publisher.assert_not_called()


async def test_publisher_backfill_calls_upstream_within_budget() -> None:
    """_backfill_publishers fetches publishers and sleeps between calls."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from cvproxy.scheduler import _backfill_publishers

    db = MagicMock()
    db.conn.execute.return_value.fetchall.return_value = [(42,)]

    client = MagicMock()
    client.total_hourly_calls.return_value = 0
    client.get_publisher = AsyncMock(return_value={"results": {"id": 42, "name": "Marvel"}})

    sleep_mock = AsyncMock()
    with patch("cvproxy.scheduler.asyncio.sleep", sleep_mock):
        await _backfill_publishers(client, db)

    client.get_publisher.assert_called_once_with(42)
    db.upsert_publisher.assert_called_once()
    sleep_mock.assert_called_once()
