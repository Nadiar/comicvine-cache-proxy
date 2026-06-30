"""Tests for the StatsTracker module."""

from __future__ import annotations

from pathlib import Path

import pytest

from cvproxy.stats import StatsTracker


@pytest.fixture
def tracker(tmp_path: Path) -> StatsTracker:
    t = StatsTracker(tmp_path / "stats.db")
    t.open()
    yield t
    t.close()


class TestRecord:
    def test_record_cache_hit(self, tracker: StatsTracker) -> None:
        tracker.record(
            client_ip="192.168.1.10",
            forwarded=None,
            endpoint="search",
            source="cache",
            latency_ms=2.5,
            query_url="/api/search/?query=Batman&resources=volume",
        )
        summary = tracker.summary()
        assert summary["totals"]["total_requests"] == 1
        assert summary["totals"]["cache_hits"] == 1
        assert summary["totals"]["upstream_calls"] == 0
        assert summary["recent_requests"][0]["query_url"] == "/api/search/?query=Batman&resources=volume"

    def test_record_upstream_call(self, tracker: StatsTracker) -> None:
        tracker.record(
            client_ip="10.0.0.1",
            forwarded="203.0.113.50",
            endpoint="volume",
            source="upstream",
            latency_ms=150.0,
        )
        summary = tracker.summary()
        assert summary["totals"]["upstream_calls"] == 1
        assert summary["totals"]["cache_hits"] == 0

    def test_record_miss(self, tracker: StatsTracker) -> None:
        tracker.record(
            client_ip="10.0.0.1",
            forwarded=None,
            endpoint="issue",
            source="miss",
            latency_ms=200.0,
        )
        summary = tracker.summary()
        assert summary["totals"]["misses"] == 1


class TestSummary:
    def _seed(self, tracker: StatsTracker) -> None:
        """Insert a mix of requests from different IPs."""
        for i in range(5):
            tracker.record(
                client_ip="192.168.1.10",
                forwarded=None,
                endpoint="search",
                source="cache",
                latency_ms=1.0 + i,
            )
        for i in range(3):
            tracker.record(
                client_ip="192.168.1.10",
                forwarded=None,
                endpoint="volume",
                source="upstream",
                latency_ms=100.0 + i,
            )
        for _i in range(2):
            tracker.record(
                client_ip="10.0.0.5",
                forwarded="203.0.113.99",
                endpoint="search",
                source="cache",
                latency_ms=2.0,
            )

    def test_totals(self, tracker: StatsTracker) -> None:
        self._seed(tracker)
        totals = tracker.summary()["totals"]
        assert totals["total_requests"] == 10
        assert totals["cache_hits"] == 7
        assert totals["upstream_calls"] == 3
        assert totals["cache_hit_rate"] == 70.0

    def test_by_client(self, tracker: StatsTracker) -> None:
        self._seed(tracker)
        clients = tracker.summary()["by_client"]
        assert len(clients) == 2
        # Sorted by total DESC — 192.168.1.10 has 8 requests
        top = clients[0]
        assert top["client_ip"] == "192.168.1.10"
        assert top["total_requests"] == 8
        assert top["cache_hits"] == 5
        assert top["upstream_calls"] == 3
        assert top["forwarded_for"] is None

        second = clients[1]
        assert second["client_ip"] == "10.0.0.5"
        assert second["forwarded_for"] == "203.0.113.99"
        assert second["total_requests"] == 2

    def test_by_endpoint(self, tracker: StatsTracker) -> None:
        self._seed(tracker)
        endpoints = tracker.summary()["by_endpoint"]
        names = {e["endpoint"] for e in endpoints}
        assert names == {"search", "volume"}

        search_ep = next(e for e in endpoints if e["endpoint"] == "search")
        assert search_ep["total_requests"] == 7
        assert search_ep["cache_hits"] == 7

    def test_recent(self, tracker: StatsTracker) -> None:
        self._seed(tracker)
        recent = tracker.summary()["recent_requests"]
        assert len(recent) == 10
        # Most recent first
        assert recent[0]["timestamp"] >= recent[-1]["timestamp"]
        # query_url key is present (None when not supplied)
        assert "query_url" in recent[0]

    def test_since_hours_filter(self, tracker: StatsTracker) -> None:
        self._seed(tracker)
        # All requests were just inserted, so since_hours=1 should include all
        summary = tracker.summary(since_hours=1)
        assert summary["totals"]["total_requests"] == 10
        assert summary["period_hours"] == 1

    def test_empty_db(self, tracker: StatsTracker) -> None:
        summary = tracker.summary()
        assert summary["totals"]["total_requests"] == 0
        assert summary["totals"]["cache_hit_rate"] == 0.0
        assert summary["by_client"] == []
        assert summary["by_endpoint"] == []
        assert summary["recent_requests"] == []

    def test_cache_hit_rate_calculation(self, tracker: StatsTracker) -> None:
        tracker.record(
            client_ip="1.2.3.4", forwarded=None, endpoint="search",
            source="cache", latency_ms=1.0,
        )
        tracker.record(
            client_ip="1.2.3.4", forwarded=None, endpoint="search",
            source="upstream", latency_ms=100.0,
        )
        tracker.record(
            client_ip="1.2.3.4", forwarded=None, endpoint="search",
            source="cache", latency_ms=1.0,
        )
        totals = tracker.summary()["totals"]
        assert totals["cache_hit_rate"] == 66.7  # 2/3


class TestLifecycle:
    def test_close_and_reopen_persists(self, tmp_path: Path) -> None:
        db_path = tmp_path / "stats.db"
        t = StatsTracker(db_path)
        t.open()
        t.record(
            client_ip="1.1.1.1", forwarded=None,
            endpoint="test", source="cache", latency_ms=1.0,
        )
        t.close()

        t2 = StatsTracker(db_path)
        t2.open()
        assert t2.summary()["totals"]["total_requests"] == 1
        t2.close()

    def test_not_opened_raises(self) -> None:
        t = StatsTracker(Path("/nonexistent/stats.db"))
        with pytest.raises(RuntimeError, match="not opened"):
            t.summary()
