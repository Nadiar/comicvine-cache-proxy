"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from cvproxy.config import Settings, override_settings
from cvproxy.db import Database
from cvproxy.image_cache import ImageCache


@pytest.fixture
def tmp_db(tmp_path: Path) -> Generator[Database]:
    """Temp database seeded via Database.connect() — always in sync with the live schema."""
    db = Database(tmp_path / "test.db")
    db.connect()

    db.upsert_publisher({"id": 1, "name": "DC Comics"})
    db.upsert_publisher({"id": 2, "name": "Marvel"})
    db.upsert_volume({
        "id": 160294,
        "name": "Absolute Batman",
        "start_year": "2024",
        "publisher": {"id": 1, "name": "DC Comics"},
        "count_of_issues": 17,
        "description": "Scott Snyder reinvents Batman",
        "image": {"original_url": "https://example.com/img.jpg"},
        "site_detail_url": "https://comicvine.gamespot.com/absolute-batman/4050-160294/",
    })
    db.upsert_volume({
        "id": 100001,
        "name": "Batman",
        "aliases": "The Batman",
        "start_year": "2016",
        "publisher": {"id": 1, "name": "DC Comics"},
        "count_of_issues": 150,
        "description": "Tom King Batman run",
    })
    db.upsert_issue({
        "id": 1073108,
        "volume": {"id": 160294},
        "name": "Issue 1",
        "issue_number": "1",
        "cover_date": "2024-12-01",
        "store_date": "2024-11-06",
        "description": "First issue",
        "image": {"original_url": "https://example.com/issue1.jpg"},
        "site_detail_url": "https://comicvine.gamespot.com/absolute-batman-1/4000-1073108/",
        "character_credits": [],
        "person_credits": [],
        "team_credits": [],
        "location_credits": [],
        "story_arc_credits": [],
        "associated_images": [],
    })
    db.upsert_issue({
        "id": 1073109,
        "volume": {"id": 160294},
        "name": "Issue 2",
        "issue_number": "2",
        "cover_date": "2025-01-01",
        "store_date": "2024-12-04",
        "description": "Second issue",
        "character_credits": [],
        "person_credits": [],
        "team_credits": [],
        "location_credits": [],
        "story_arc_credits": [],
        "associated_images": [],
    })

    yield db
    db.close()


@pytest.fixture
def image_cache(tmp_path: Path) -> Generator[ImageCache]:
    """Create a temporary image cache."""
    cache_dir = tmp_path / "images"
    cache = ImageCache(cache_dir=cache_dir, ttl_days=14)
    cache.open()
    yield cache
    cache.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Create test settings."""
    s = Settings(
        cv_api_key="test_key_123",
        db_path=tmp_path / "test.db",
        image_cache_dir=tmp_path / "images",
        stats_db_path=tmp_path / "stats.db",
        sync_enabled=False,
    )
    override_settings(s)
    return s
