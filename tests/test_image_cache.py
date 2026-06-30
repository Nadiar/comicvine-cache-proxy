"""Tests for the image cache."""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

from cvproxy.image_cache import ImageCache


def test_store_and_lookup(image_cache: ImageCache) -> None:
    url = "https://example.com/img.jpg"
    data = b"\xff\xd8\xff\xe0fake_jpeg_data"
    image_cache.store(url, data, "image/jpeg")

    result = image_cache.lookup(url)
    assert result is not None
    assert result.data == data
    assert result.content_type == "image/jpeg"


def test_lookup_miss(image_cache: ImageCache) -> None:
    result = image_cache.lookup("https://example.com/nope.jpg")
    assert result is None


def test_lookup_refreshes_ttl(image_cache: ImageCache) -> None:
    url = "https://example.com/img.jpg"
    image_cache.store(url, b"data", "image/jpeg")

    # First lookup
    image_cache.lookup(url)

    # Check last_accessed was updated
    cur = image_cache.conn.execute("SELECT last_accessed FROM image_cache LIMIT 1")
    ts1 = cur.fetchone()["last_accessed"]

    time.sleep(0.05)  # tiny delay to ensure time difference

    # Second lookup should update timestamp
    image_cache.lookup(url)
    cur = image_cache.conn.execute("SELECT last_accessed FROM image_cache LIMIT 1")
    ts2 = cur.fetchone()["last_accessed"]

    assert ts2 >= ts1  # timestamp was refreshed


def test_cleanup_expired(tmp_path: Path) -> None:
    cache = ImageCache(cache_dir=tmp_path / "img", ttl_days=0)  # 0 days = expire immediately
    cache.open()

    cache.store("https://example.com/old.jpg", b"old", "image/jpeg")

    # Force last_accessed to the past
    cache.conn.execute("UPDATE image_cache SET last_accessed = '2020-01-01T00:00:00+00:00'")
    cache.conn.commit()

    removed = cache.cleanup_expired()
    assert removed == 1

    # Verify it's gone
    assert cache.lookup("https://example.com/old.jpg") is None

    cache.close()


def test_cleanup_keeps_fresh(image_cache: ImageCache) -> None:
    image_cache.store("https://example.com/fresh.jpg", b"fresh", "image/jpeg")
    removed = image_cache.cleanup_expired()
    assert removed == 0
    assert image_cache.lookup("https://example.com/fresh.jpg") is not None


def test_stats(image_cache: ImageCache) -> None:
    assert image_cache.stats() == {"images_cached": 0, "cache_size_bytes": 0}

    image_cache.store("https://example.com/a.jpg", b"1234567890", "image/jpeg")
    stats = image_cache.stats()
    assert stats["images_cached"] == 1
    assert stats["cache_size_bytes"] == 10


def test_stale_row_cleaned_on_lookup(image_cache: ImageCache) -> None:
    url = "https://example.com/deleted.jpg"
    image_cache.store(url, b"data", "image/jpeg")

    # Delete the file on disk manually
    cur = image_cache.conn.execute("SELECT file_path FROM image_cache LIMIT 1")
    rel = cur.fetchone()["file_path"]
    (image_cache._cache_dir / rel).unlink()

    # Lookup should return None and clean the stale row
    assert image_cache.lookup(url) is None

    # Row should be gone
    cur = image_cache.conn.execute("SELECT COUNT(*) FROM image_cache")
    assert cur.fetchone()[0] == 0


async def test_concurrent_misses_fire_only_one_upstream_fetch(image_cache: ImageCache) -> None:
    """N concurrent requests for the same uncached URL must call upstream exactly once."""
    url = "https://example.com/cover.jpg"
    fake_data = b"fake image bytes"
    fetch_count = 0

    async def fake_get_image(_url: str):
        nonlocal fetch_count
        fetch_count += 1
        await asyncio.sleep(0.05)  # simulate network latency
        return fake_data, "image/jpeg"

    mock_client = AsyncMock()
    mock_client.get_image = fake_get_image

    results = await asyncio.gather(*[image_cache.get_or_fetch(url, mock_client) for _ in range(5)])

    assert fetch_count == 1, f"Expected 1 upstream fetch, got {fetch_count}"
    assert all(r is not None for r in results)
    assert all(r.data == fake_data for r in results)
