"""Tests for CVClient rate limiting."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from cvproxy.cv_client import RATE_LIMITED, CVClient


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
    resp = httpx.Response(
        200, content=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )
    resp.request = httpx.Request("GET", "https://comicvine.gamespot.com/api/test/")
    return resp


async def test_get_returns_rate_limited_on_http_429() -> None:
    """CVClient.get() should return RATE_LIMITED when the upstream responds 429."""
    client = CVClient(api_key="test")
    mock_resp = _make_response(429)
    mock_resp.request = httpx.Request("GET", "https://example.com")

    error = httpx.HTTPStatusError("429", request=mock_resp.request, response=mock_resp)
    with patch.object(client._client, "get", new=AsyncMock(side_effect=error)):
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
    from datetime import datetime

    reset_dt = datetime.fromisoformat(vol["sliding_reset_at"])
    assert reset_dt > datetime.now(UTC), "reset_at should be in the future"
    # clock_hour_reset_at must also be a valid future UTC ISO timestamp
    clock_dt = datetime.fromisoformat(vol["clock_hour_reset_at"])
    assert clock_dt > datetime.now(UTC)
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
    from datetime import datetime

    result = CVClient._next_clock_hour_utc()
    dt = datetime.fromisoformat(result)
    assert dt > datetime.now(UTC)
    assert dt.minute == 0
    assert dt.second == 0


# ---------------------------------------------------------------------------
# HTTP 420, cooldown, retry_after
# ---------------------------------------------------------------------------


def _status_error(status_code: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    resp = httpx.Response(status_code, headers=headers or {})
    resp.request = httpx.Request("GET", "https://comicvine.gamespot.com/api/issues/")
    return httpx.HTTPStatusError(str(status_code), request=resp.request, response=resp)


async def test_get_returns_rate_limited_on_http_420() -> None:
    """ComicVine signals rate limiting with HTTP 420, not only 429."""
    client = CVClient(api_key="test")
    with patch.object(client._client, "get", new=AsyncMock(side_effect=_status_error(420))):
        result = await client.get("issues/")

    assert result is RATE_LIMITED
    await client.close()


async def test_rate_limit_starts_cooldown_without_further_upstream_calls() -> None:
    """After a 420 the client refuses upstream calls until the cooldown ends."""
    client = CVClient(api_key="test", rate_limit_per_minute=6000, rate_limit_cooldown_seconds=300)
    upstream = AsyncMock(side_effect=_status_error(420))
    with patch.object(client._client, "get", new=upstream):
        await client.get("issues/")
        second = await client.get("issue/4000-1/")

    assert second is RATE_LIMITED
    assert upstream.call_count == 1
    assert 290 < client.retry_after("issue/4000-1/") <= 300
    await client.close()


async def test_rate_limit_cooldown_honours_upstream_retry_after_header() -> None:
    client = CVClient(api_key="test", rate_limit_cooldown_seconds=300)
    error = _status_error(420, headers={"Retry-After": "1200"})
    with patch.object(client._client, "get", new=AsyncMock(side_effect=error)):
        await client.get("issues/")

    assert 1190 < client.retry_after("issues/") <= 1200
    await client.close()


async def test_retry_after_is_zero_when_budget_available() -> None:
    client = CVClient(api_key="test")
    assert client.retry_after("issues/") == 0
    await client.close()


async def test_retry_after_when_endpoint_hourly_window_full() -> None:
    """A full per-endpoint window reports when its oldest slot frees up."""
    from collections import deque

    client = CVClient(api_key="test", rate_limit_per_hour_per_endpoint=2)
    now = time.time()
    client._hourly_windows["issues"] = deque([now - 3000, now - 10])

    assert 590 < client.retry_after("issues/") <= 600
    assert client.retry_after("volumes/") == 0
    await client.close()


async def test_hourly_calls_counts_one_endpoint() -> None:
    from collections import deque

    client = CVClient(api_key="test")
    now = time.time()
    client._hourly_windows["issue"] = deque([now - 3700, now - 3000, now - 10])
    client._hourly_windows["issues"] = deque([now - 5])

    assert client.hourly_calls("issue/4000-1/") == 2  # stale entry excluded
    assert client.hourly_calls("issues/") == 1
    assert client.hourly_calls("search/") == 0
    await client.close()


async def test_cooldown_rechecked_after_pacing_wait() -> None:
    """Requests queued behind the pacer must not go out once a 420 has landed."""
    client = CVClient(api_key="test", rate_limit_cooldown_seconds=300)
    upstream = AsyncMock()

    async def _420_arrives_while_waiting() -> None:
        client._start_cooldown()

    with (
        patch.object(client, "_wait_for_rate_limit", new=_420_arrives_while_waiting),
        patch.object(client._client, "get", new=upstream),
    ):
        result = await client.get("issues/")

    assert result is RATE_LIMITED
    upstream.assert_not_called()
    await client.close()


# ---------------------------------------------------------------------------
# Rate state persists across restarts
# ---------------------------------------------------------------------------


async def test_rate_state_survives_restart(tmp_path: Path) -> None:
    state = tmp_path / "rate_state.json"
    first = CVClient(api_key="test", rate_limit_per_minute=6000, state_path=state)
    ok = {"status_code": 1, "error": "OK", "results": {"id": 1}}
    with patch.object(first._client, "get", new=AsyncMock(return_value=_json_response(ok))):
        await first.get("issue/4000-1/")
    with patch.object(first._client, "get", new=AsyncMock(side_effect=_status_error(420))):
        await first.get("search/")
    await first.close()

    second = CVClient(api_key="test", state_path=state)

    assert second.hourly_calls("issue/") == 1
    assert second.hourly_calls("search/") == 1
    assert second.retry_after("volume/") > 500  # the cooldown came back too
    await second.close()


async def test_rate_state_drops_calls_older_than_an_hour(tmp_path: Path) -> None:
    state = tmp_path / "rate_state.json"
    now = time.time()
    state.write_text(json.dumps({
        "windows": {"issue": [now - 7200, now - 60]},
        "cooldown_until": now - 5,
    }))

    client = CVClient(api_key="test", state_path=state)

    assert client.hourly_calls("issue/") == 1
    assert client.retry_after("issue/") == 0
    await client.close()


async def test_rate_state_unreadable_file_starts_empty(tmp_path: Path) -> None:
    state = tmp_path / "rate_state.json"
    state.write_text("{not json")

    client = CVClient(api_key="test", state_path=state)

    assert client.total_hourly_calls() == 0
    await client.close()
