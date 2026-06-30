"""Rate-limited async HTTP client for the upstream ComicVine API.

Used when the local DB doesn't have the data (cache miss) and we need to
forward the request upstream.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)


class _RateLimited:
    """Sentinel returned by :meth:`CVClient.get` when the upstream rate limit is hit.

    Falsy (like ``None``) but distinguishable via ``is RATE_LIMITED`` so
    callers can return a proper rate-limit error instead of a generic miss.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "RATE_LIMITED"


#: Returned instead of ``None`` when the upstream CV API explicitly refuses
#: the request due to rate limiting (HTTP 429 or JSON status_code 107).
RATE_LIMITED: Final[_RateLimited] = _RateLimited()


class _NotFound:
    """Sentinel returned by :meth:`CVClient.get` when CV reports the resource does not exist.

    Falsy (like ``None``) but distinguishable via ``is NOT_FOUND`` so the
    backfill can permanently skip dead IDs instead of retrying every hour.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "NOT_FOUND"


#: Returned when the CV API responds with status_code 101 (Object Not Found).
NOT_FOUND: Final[_NotFound] = _NotFound()


class CVClient:
    """Async ComicVine API client with rate limiting."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://comicvine.gamespot.com/api",
        rate_limit_per_minute: int = 100,
        rate_limit_per_hour_per_endpoint: int = 180,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._min_interval = 60.0 / rate_limit_per_minute
        self._last_request: float = 0.0
        self._lock = asyncio.Lock()
        # Per-endpoint hourly sliding window.  CV enforces 200 req/hr per path.
        self._hourly_limit = rate_limit_per_hour_per_endpoint
        self._hourly_windows: dict[str, deque[float]] = {}
        self._hourly_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": "CVProxy/0.1",
            },
            follow_redirects=True,
        )

    @property
    def hourly_limit(self) -> int:
        return self._hourly_limit

    def next_clock_hour_reset(self) -> str:
        return self._next_clock_hour_utc()

    async def close(self) -> None:
        await self._client.aclose()

    # -- rate limiters -------------------------------------------------------

    async def _wait_for_rate_limit(self) -> None:
        """Global per-minute limiter — evenly spaces all outgoing requests."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            wait = max(0.0, self._min_interval - elapsed)
            # Reserve this slot before releasing the lock so the next caller
            # schedules itself into the subsequent slot.
            self._last_request = now + wait
        if wait:
            await asyncio.sleep(wait)

    @staticmethod
    def _endpoint_key(endpoint: str) -> str:
        """Extract the base path segment used by CV's per-endpoint hourly quota.

        Examples::

            "volumes/"          → "volumes"
            "volume/4050-123/"  → "volume"
            "issues/"           → "issues"
        """
        return endpoint.strip("/").split("/")[0]

    async def _wait_for_hourly_limit(self, endpoint_key: str) -> None:
        """Per-endpoint sliding-window hourly limiter.

        Tracks the wall-clock timestamp (``time.time()``) of every request made
        against *endpoint_key* in the last 60 minutes.  When the count reaches
        the configured hourly limit, sleeps until the oldest request in the
        window falls outside the 1-hour window, then records the new slot.
        Using wall-clock time (rather than monotonic) means the timestamps can
        be converted to UTC datetimes for display.

        The lock is released before sleeping so other coroutines (including
        live proxy requests) are not blocked during a potentially hours-long
        quota-reset wait.
        """
        while True:
            async with self._hourly_lock:
                now = time.time()
                window = self._hourly_windows.setdefault(endpoint_key, deque())

                # Prune requests older than 1 hour.
                cutoff = now - 3600.0
                while window and window[0] <= cutoff:
                    window.popleft()

                if len(window) < self._hourly_limit:
                    window.append(time.time())
                    return

                # Over limit — compute wait and release lock before sleeping.
                wait = window[0] + 3600.0 - now

            logger.warning(
                "Hourly limit reached for endpoint '/%s' (%d/%d). "
                "Sleeping %.0fs until quota resets.",
                endpoint_key,
                len(window),
                self._hourly_limit,
                wait,
            )
            await asyncio.sleep(max(wait, 0.0))

    # -- public API ----------------------------------------------------------

    async def get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> "dict[str, Any] | _RateLimited | None":
        """Make a GET request to the CV API.

        Returns the parsed JSON response body, ``RATE_LIMITED`` when the
        upstream explicitly refuses the request due to rate limiting, or
        ``None`` on any other error.
        """
        endpoint_key = self._endpoint_key(endpoint)
        await self._wait_for_hourly_limit(endpoint_key)
        await self._wait_for_rate_limit()

        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        req_params: dict[str, Any] = {
            "api_key": self._api_key,
            "format": "json",
            **(params or {}),
        }

        try:
            resp = await self._client.get(url, params=req_params)
            resp.raise_for_status()
            data = resp.json()

            status = data.get("status_code", 0)
            if status == 107:
                logger.warning("CV API rate limit (status 107): %s", endpoint)
                return RATE_LIMITED
            if status == 101:
                logger.debug("CV API not found (status 101): %s", endpoint)
                return NOT_FOUND
            if status != 1:
                error = data.get("error", "Unknown error")
                logger.warning("CV API error %d: %s (endpoint=%s)", status, error, endpoint)
                return None

            return data
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("CV API rate limit (HTTP 429): %s", endpoint)
                return RATE_LIMITED
            logger.error("CV API HTTP %d: %s", exc.response.status_code, endpoint)
            return None
        except httpx.RequestError as exc:
            logger.error("CV API request error: %s (%s)", exc, endpoint)
            return None

    @staticmethod
    def _next_clock_hour_utc() -> str:
        """Return the ISO 8601 UTC timestamp of the next top-of-the-hour boundary.

        ComicVine's rate limit may reset on fixed clock hours (e.g. 14:00, 15:00)
        rather than on a rolling 60-minute window.  This timestamp lets callers
        display the worst-case reset time under that assumption.
        """
        now_utc = datetime.now(timezone.utc)
        next_hour = now_utc.replace(minute=0, second=0, microsecond=0)
        # Advance by one full hour to get the *next* boundary.
        next_hour = next_hour.replace(hour=(now_utc.hour + 1) % 24)
        # Handle midnight rollover: if next_hour < now it crossed midnight.
        if next_hour <= now_utc:
            next_hour = next_hour + timedelta(days=1)
        return next_hour.isoformat()

    def get_hourly_usage(self) -> dict[str, Any]:
        """Return current sliding-window usage counts and reset times per endpoint.

        Each endpoint entry contains:

        - ``used`` — calls tracked in the current 1-hour sliding window
        - ``limit`` — configured maximum per hour
        - ``sliding_reset_at`` — UTC ISO timestamp when the *oldest* tracked call
          rolls off the sliding window (i.e. when one more slot opens up).
          ``null`` when the window is empty.
        - ``clock_hour_reset_at`` — UTC ISO timestamp of the next top-of-the-hour
          boundary.  If ComicVine enforces a fixed hourly quota (rather than a
          rolling 60-minute window) this is the time the entire quota resets.

        Example::

            {
              "volumes": {
                "used": 42, "limit": 180,
                "sliding_reset_at": "2026-03-26T15:12:00+00:00",
                "clock_hour_reset_at": "2026-03-26T15:00:00+00:00"
              }
            }
        """
        now = time.time()
        cutoff = now - 3600.0
        clock_hour = self._next_clock_hour_utc()
        result: dict[str, Any] = {}
        for key, window in self._hourly_windows.items():
            active = [ts for ts in window if ts > cutoff]
            oldest_ts = min(active) if active else None
            sliding_reset_at = (
                datetime.fromtimestamp(oldest_ts + 3600.0, tz=timezone.utc).isoformat()
                if oldest_ts is not None
                else None
            )
            result[key] = {
                "used": len(active),
                "limit": self._hourly_limit,
                "sliding_reset_at": sliding_reset_at,
                "clock_hour_reset_at": clock_hour,
            }
        return result

    def total_hourly_calls(self) -> int:
        """Return the total upstream API calls made in the sliding 1-hour window.

        Sums all per-endpoint windows so the scheduler and route handlers can
        cheaply check against a single global threshold without locking.
        """
        now = time.time()
        cutoff = now - 3600.0
        return sum(
            sum(1 for ts in window if ts > cutoff)
            for window in self._hourly_windows.values()
        )

    async def get_image(self, url: str) -> tuple[bytes, str] | None:
        """Fetch a raw image from any URL (typically a CV image CDN URL).

        Returns (content_bytes, content_type) or None.
        """
        await self._wait_for_rate_limit()

        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg")
            return resp.content, content_type
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.error("Image fetch failed: %s (%s)", exc, url)
            return None

    # -- convenience wrappers ------------------------------------------------

    async def search(
        self,
        query: str,
        resources: str = "volume",
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        return await self.get(
            "search/",
            {
                "query": query,
                "resources": resources,
                "limit": limit,
                "offset": offset,
            },
        )

    async def get_volume(self, volume_id: int) -> dict[str, Any] | None:
        return await self.get(
            f"volume/4050-{volume_id}/",
            {
                "field_list": "id,name,aliases,start_year,publisher,count_of_issues,"
                "deck,description,image,site_detail_url,issues,"
                "characters,concepts,people,objects,first_issue,last_issue,"
                "api_detail_url,date_added,date_last_updated"
            },
        )

    async def get_issue(self, issue_id: int) -> dict[str, Any] | None:
        return await self.get(f"issue/4000-{issue_id}/")

    async def get_publisher(self, publisher_id: int) -> dict[str, Any] | None:
        return await self.get(f"publisher/4010-{publisher_id}/")
