"""On-demand image cache with 14-day sliding TTL.

Images are fetched from upstream on first request and stored on disk.
Each access refreshes the TTL.  A daily cleanup job removes stale entries.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from cvproxy.cv_client import CVClient

logger = logging.getLogger(__name__)


class CachedImage(NamedTuple):
    data: bytes
    content_type: str


class ImageCache:
    """Disk-backed image cache with SQLite metadata index."""

    def __init__(
        self,
        cache_dir: Path,
        ttl_days: int = 14,
    ) -> None:
        self._cache_dir = cache_dir
        self._ttl_days = ttl_days
        self._db_path = cache_dir / "_cache.db"
        self._conn: sqlite3.Connection | None = None
        self._in_flight: dict[str, asyncio.Event] = {}

    # -- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS image_cache (
                url_hash      TEXT PRIMARY KEY,
                original_url  TEXT NOT NULL,
                file_path     TEXT NOT NULL,
                file_size     INTEGER,
                content_type  TEXT,
                created_at    TEXT NOT NULL,
                last_accessed TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_image_cache_lru
                ON image_cache(last_accessed);
        """)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ImageCache not opened")
        return self._conn

    # -- public API ----------------------------------------------------------

    def lookup(self, url: str) -> CachedImage | None:
        """Check disk cache for *url*. Returns bytes + content_type, or None."""
        url_hash = self._hash(url)
        cur = self.conn.execute(
            "SELECT file_path, content_type FROM image_cache WHERE url_hash = ?",
            [url_hash],
        )
        row = cur.fetchone()
        if not row:
            return None

        file_path = self._cache_dir / row["file_path"]
        if not file_path.exists():
            # File deleted externally — remove stale row
            self.conn.execute("DELETE FROM image_cache WHERE url_hash = ?", [url_hash])
            self.conn.commit()
            return None

        # Touch last_accessed (sliding TTL)
        now = _now_iso()
        self.conn.execute(
            "UPDATE image_cache SET last_accessed = ? WHERE url_hash = ?",
            [now, url_hash],
        )
        self.conn.commit()

        return CachedImage(data=file_path.read_bytes(), content_type=row["content_type"])

    def store(self, url: str, data: bytes, content_type: str) -> None:
        """Write *data* to disk and record in the index."""
        url_hash = self._hash(url)

        # Shard into 2-level hex dirs (e.g. ab/cd/abcdef…)
        rel = Path(url_hash[:2]) / url_hash[2:4] / url_hash
        full = self._cache_dir / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)

        now = _now_iso()
        self.conn.execute(
            "INSERT OR REPLACE INTO image_cache "
            "(url_hash, original_url, file_path, file_size, content_type, "
            " created_at, last_accessed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [url_hash, url, str(rel), len(data), content_type, now, now],
        )
        self.conn.commit()
        logger.debug("Cached image %s (%d bytes)", url_hash[:12], len(data))

    async def get_or_fetch(self, url: str, client: CVClient) -> CachedImage | None:
        """Return from cache or fetch upstream, with in-flight deduplication.

        If N concurrent callers request the same uncached URL, only the first
        fires an upstream fetch; the rest wait and then read from disk cache.
        """
        cached = self.lookup(url)
        if cached:
            return cached

        # Another coroutine is already fetching this URL — wait for it, then read cache.
        existing_event = self._in_flight.get(url)
        if existing_event is not None:
            await existing_event.wait()
            return self.lookup(url)

        # We are first — register the in-flight marker.
        event = asyncio.Event()
        self._in_flight[url] = event
        try:
            result = await client.get_image(url)
            if result is None:
                return None
            data, content_type = result
            self.store(url, data, content_type)
            return CachedImage(data=data, content_type=content_type)
        finally:
            self._in_flight.pop(url, None)
            event.set()

    # -- cleanup -------------------------------------------------------------

    def cleanup_expired(self) -> int:
        """Delete cache entries older than TTL.  Returns count removed."""
        cutoff_ts = time.time() - (self._ttl_days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=UTC).isoformat()

        cur = self.conn.execute(
            "SELECT url_hash, file_path FROM image_cache WHERE last_accessed < ?",
            [cutoff_iso],
        )
        rows = cur.fetchall()

        removed = 0
        for row in rows:
            file_path = self._cache_dir / row["file_path"]
            with contextlib.suppress(OSError):
                file_path.unlink(missing_ok=True)
            removed += 1

        if removed:
            self.conn.execute("DELETE FROM image_cache WHERE last_accessed < ?", [cutoff_iso])
            self.conn.commit()
            logger.info("Image cache cleanup: removed %d expired entries", removed)

        return removed

    def stats(self) -> dict[str, int]:
        """Cache statistics for the health endpoint."""
        cur = self.conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(file_size), 0) AS total FROM image_cache"
        )
        row = cur.fetchone()
        if row is None:
            return {"images_cached": 0, "cache_size_bytes": 0}
        return {"images_cached": row["cnt"], "cache_size_bytes": row["total"]}

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _hash(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
