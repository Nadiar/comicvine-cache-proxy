"""Request statistics tracker for the CVProxy dashboard.

Tracks cache hits vs upstream API calls, broken down by client IP address
and endpoint.  All data is stored in a SQLite database so stats survive
restarts.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,          -- unix timestamp
    client_ip  TEXT    NOT NULL,
    forwarded  TEXT,                      -- X-Forwarded-For value (if any)
    endpoint   TEXT    NOT NULL,
    source     TEXT    NOT NULL CHECK(source IN ('cache', 'upstream', 'miss')),
    latency_ms REAL,
    query_url  TEXT                       -- request path+query (no api_key)
);

CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log(ts);
CREATE INDEX IF NOT EXISTS idx_request_log_client ON request_log(client_ip);
CREATE INDEX IF NOT EXISTS idx_request_log_source ON request_log(source);
"""

_MIGRATE_QUERY_URL = (
    "ALTER TABLE request_log ADD COLUMN query_url TEXT"
)


class StatsTracker:
    """Thread-safe request stats collector backed by SQLite."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        # Migrate: add query_url column if missing
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(request_log)").fetchall()}
        if "query_url" not in cols:
            self._conn.execute(_MIGRATE_QUERY_URL)
            self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("StatsTracker not opened")
        return self._conn

    # -- recording -----------------------------------------------------------

    def record(
        self,
        *,
        client_ip: str,
        forwarded: str | None,
        endpoint: str,
        source: str,
        latency_ms: float | None = None,
        query_url: str | None = None,
    ) -> None:
        """Record a single request event."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO request_log (ts, client_ip, forwarded, endpoint, source, latency_ms, query_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), client_ip, forwarded, endpoint, source, latency_ms, query_url),
            )
            self.conn.commit()

    # -- queries -------------------------------------------------------------

    def summary(self, *, since_hours: float | None = None) -> dict[str, Any]:
        """Return aggregate stats, optionally filtered to recent hours."""
        where, params = self._time_filter(since_hours)

        totals = self._totals(where, params)
        by_client = self._by_client(where, params)
        by_endpoint = self._by_endpoint(where, params)
        recent = self._recent(where, params, limit=50)

        return {
            "period_hours": since_hours,
            "totals": totals,
            "by_client": by_client,
            "by_endpoint": by_endpoint,
            "recent_requests": recent,
        }

    # -- internal helpers ----------------------------------------------------

    def _time_filter(
        self, since_hours: float | None
    ) -> tuple[str, list[float]]:
        if since_hours is None:
            return "1=1", []
        cutoff = time.time() - since_hours * 3600
        return "ts >= ?", [cutoff]

    def _totals(self, where: str, params: list[float]) -> dict[str, int]:
        cur = self.conn.execute(
            f"SELECT source, COUNT(*) AS cnt FROM request_log WHERE {where} GROUP BY source",
            params,
        )
        rows = {r["source"]: r["cnt"] for r in cur.fetchall()}
        total = sum(rows.values())
        return {
            "total_requests": total,
            "cache_hits": rows.get("cache", 0),
            "upstream_calls": rows.get("upstream", 0),
            "misses": rows.get("miss", 0),
            "cache_hit_rate": (
                round(rows.get("cache", 0) / total * 100, 1) if total else 0.0
            ),
        }

    def _by_client(self, where: str, params: list[float]) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            f"""
            SELECT
                client_ip,
                forwarded,
                COUNT(*) AS total,
                SUM(CASE WHEN source = 'cache' THEN 1 ELSE 0 END) AS cache_hits,
                SUM(CASE WHEN source = 'upstream' THEN 1 ELSE 0 END) AS upstream_calls,
                SUM(CASE WHEN source = 'miss' THEN 1 ELSE 0 END) AS misses,
                ROUND(AVG(latency_ms), 1) AS avg_latency_ms,
                MAX(ts) AS last_seen
            FROM request_log
            WHERE {where}
            GROUP BY client_ip, forwarded
            ORDER BY total DESC
            """,
            params,
        )
        return [
            {
                "client_ip": r["client_ip"],
                "forwarded_for": r["forwarded"],
                "total_requests": r["total"],
                "cache_hits": r["cache_hits"],
                "upstream_calls": r["upstream_calls"],
                "misses": r["misses"],
                "cache_hit_rate": (
                    round(r["cache_hits"] / r["total"] * 100, 1) if r["total"] else 0.0
                ),
                "avg_latency_ms": r["avg_latency_ms"],
                "last_seen": r["last_seen"],
            }
            for r in cur.fetchall()
        ]

    def _by_endpoint(self, where: str, params: list[float]) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            f"""
            SELECT
                endpoint,
                COUNT(*) AS total,
                SUM(CASE WHEN source = 'cache' THEN 1 ELSE 0 END) AS cache_hits,
                SUM(CASE WHEN source = 'upstream' THEN 1 ELSE 0 END) AS upstream_calls,
                ROUND(AVG(latency_ms), 1) AS avg_latency_ms
            FROM request_log
            WHERE {where}
            GROUP BY endpoint
            ORDER BY total DESC
            """,
            params,
        )
        return [
            {
                "endpoint": r["endpoint"],
                "total_requests": r["total"],
                "cache_hits": r["cache_hits"],
                "upstream_calls": r["upstream_calls"],
                "cache_hit_rate": (
                    round(r["cache_hits"] / r["total"] * 100, 1) if r["total"] else 0.0
                ),
                "avg_latency_ms": r["avg_latency_ms"],
            }
            for r in cur.fetchall()
        ]

    def _recent(
        self, where: str, params: list[float], *, limit: int = 50
    ) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            f"""
            SELECT ts, client_ip, forwarded, endpoint, source, latency_ms, query_url
            FROM request_log
            WHERE {where}
            ORDER BY ts DESC
            LIMIT ?
            """,
            [*params, limit],
        )
        return [
            {
                "timestamp": r["ts"],
                "client_ip": r["client_ip"],
                "forwarded_for": r["forwarded"],
                "endpoint": r["endpoint"],
                "source": r["source"],
                "latency_ms": r["latency_ms"],
                "query_url": r["query_url"],
            }
            for r in cur.fetchall()
        ]
