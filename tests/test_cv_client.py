"""Tests for CVClient rate limiting."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cvproxy.cv_client import CVClient, RATE_LIMITED


async def test_rate_limiter_releases_lock_before_sleeping() -> None:
    """Verify the rate limiter releases the lock before sleeping.

    Strategy: start a rate-limited waiter (which must sleep), then
    immediately try to acquire the internal lock from a second coroutine.
    With the bug the lock is held for the full sleep duration — the second
    coroutine blocks until the sleeper wakes.  With the fix the lock is
    released nearly instantly, so the second coroutine acquires it well
    before the sleep interval expires.
    """
    min_interval = 0.3
    client = CVClient(api_key="test", rate_limit_per_minute=200)
    client._min_interval = min_interval

    # Pre-warm so the first waiter must sleep the full interval.
    client._last_request = time.monotonic()

    lock_acquired_after: list[float] = []

    async def probe_lock() -> None:
        """Try to acquire the rate-limiter lock and record how long it took."""
        # Give the waiter a head-start so it enters the lock first.
        await asyncio.sleep(0.01)
        t = time.monotonic()
        async with client._lock:
            lock_acquired_after.append(time.monotonic() - t)

    await asyncio.gather(
        client._wait_for_rate_limit(),
        probe_lock(),
    )
    await client.close()

    wait_for_lock = lock_acquired_after[0]

    # With the fix: lock released after slot reservation (microseconds).
    # The probe acquires the lock well before min_interval elapses.
    # With the bug: lock held for the full sleep (~0.3 s).
    # The probe blocks until the sleeper wakes, so wait ≈ min_interval.
    assert wait_for_lock < min_interval * 0.5, (
        f"Lock was held for {wait_for_lock:.3f}s — "
        f"expected < {min_interval * 0.5:.3f}s (half of min_interval={min_interval}s). "
        "Lock appears to be held during sleep."
    )


# ---------------------------------------------------------------------------
# RATE_LIMITED sentinel
# ---------------------------------------------------------------------------


def test_rate_limited_sentinel_is_falsy() -> None:
    assert not RATE_LIMITED
    assert RATE_LIMITED is RATE_LIMITED  # identity check works


def _make_response(status_code: int, body: bytes | None = None) -> httpx.Response:
    """Build a minimal httpx.Response for mocking."""
    return httpx.Response(status_code, content=body or b"")


def _json_response(payload: dict) -> httpx.Response:
    resp = httpx.Response(200, content=json.dumps(payload).encode(), headers={"content-type": "application/json"})
    resp.request = httpx.Request("GET", "https://comicvine.gamespot.com/api/test/")
    return resp


async def test_get_returns_rate_limited_on_http_429() -> None:
    """CVClient.get() should return RATE_LIMITED when the upstream responds 429."""
    client = CVClient(api_key="test")
    mock_resp = _make_response(429)
    mock_resp.request = httpx.Request("GET", "https://example.com")

    with patch.object(client._client, "get", new=AsyncMock(side_effect=httpx.HTTPStatusError("429", request=mock_resp.request, response=mock_resp))):
        result = await client.get("volumes/")

    assert result is RATE_LIMITED
    await client.close()


async def test_get_returns_rate_limited_on_cv_status_107() -> None:
    """CVClient.get() should return RATE_LIMITED when CV JSON status_code is 107."""
    client = CVClient(api_key="test")
    body = {"status_code": 107, "error": "Rate Limit Exceeded", "results": []}

    with patch.object(client._client, "get", new=AsyncMock(return_value=_json_response(body))):
        result = await client.get("volumes/")

    assert result is RATE_LIMITED
    await client.close()


async def test_get_returns_none_on_other_cv_error() -> None:
    """Non-107 CV error codes should still return None (not RATE_LIMITED)."""
    client = CVClient(api_key="test")
    body = {"status_code": 100, "error": "Invalid API Key", "results": []}

    with patch.object(client._client, "get", new=AsyncMock(return_value=_json_response(body))):
        result = await client.get("volumes/")

    assert result is None
    await client.close()


# ---------------------------------------------------------------------------
# total_hourly_calls
# ---------------------------------------------------------------------------


async def test_total_hourly_calls_empty() -> None:
    client = CVClient(api_key="test")
    assert client.total_hourly_calls() == 0
    await client.close()


async def test_total_hourly_calls_counts_across_endpoints() -> None:
    client = CVClient(api_key="test")
    now = time.time()
    from collections import deque

    client._hourly_windows["volumes"] = deque([now - 10, now - 20])
    client._hourly_windows["issues"] = deque([now - 5])
    # Add one that is outside the 1-hour window — should not be counted.
    client._hourly_windows["search"] = deque([now - 3700])

    assert client.total_hourly_calls() == 3  # 2 + 1; the stale one is excluded
    await client.close()


# ---------------------------------------------------------------------------
# get_hourly_usage reset timestamps
# ---------------------------------------------------------------------------


async def test_get_hourly_usage_includes_reset_fields() -> None:
    """get_hourly_usage() should include sliding_reset_at and clock_hour_reset_at."""
    client = CVClient(api_key="test")
    now = time.time()
    from collections import deque

    # Seed a window with two calls: one 10 s ago, one 20 s ago.
    client._hourly_windows["volumes"] = deque([now - 20, now - 10])

    usage = client.get_hourly_usage()
    vol = usage["volumes"]

    assert vol["used"] == 2
    assert vol["limit"] == client._hourly_limit
    # sliding_reset_at = oldest call + 3600 s ≈ now + 3580 s
    assert vol["sliding_reset_at"] is not None
    from datetime import datetime, timezone

    reset_dt = datetime.fromisoformat(vol["sliding_reset_at"])
    assert reset_dt > datetime.now(timezone.utc), "reset_at should be in the future"
    # clock_hour_reset_at must also be a valid future UTC ISO timestamp
    clock_dt = datetime.fromisoformat(vol["clock_hour_reset_at"])
    assert clock_dt > datetime.now(timezone.utc)
    await client.close()


async def test_get_hourly_usage_empty_window_sliding_reset_is_null() -> None:
    """When no calls have been made, sliding_reset_at should be None."""
    client = CVClient(api_key="test")
    from collections import deque

    client._hourly_windows["issues"] = deque()
    usage = client.get_hourly_usage()
    assert usage["issues"]["sliding_reset_at"] is None
    await client.close()


def test_next_clock_hour_utc_is_always_in_future() -> None:
    """_next_clock_hour_utc() should always return a future top-of-the-hour."""
    from datetime import datetime, timezone

    result = CVClient._next_clock_hour_utc()
    dt = datetime.fromisoformat(result)
    assert dt > datetime.now(timezone.utc)
    assert dt.minute == 0
    assert dt.second == 0
