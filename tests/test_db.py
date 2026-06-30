"""Tests for the database layer."""

from pathlib import Path

import pytest

from cvproxy.db import Database, is_past_cutoff


def test_search_volumes_exact_match(tmp_db: Database) -> None:
    results, total = tmp_db.search_volumes("Absolute Batman")
    assert total >= 1
    assert results[0]["id"] == 160294
    assert results[0]["name"] == "Absolute Batman"


def test_search_volumes_fts(tmp_db: Database) -> None:
    results, total = tmp_db.search_volumes("Absolute")
    assert total >= 1
    assert any(r["id"] == 160294 for r in results)


def test_search_volumes_like_fallback(tmp_db: Database) -> None:
    # Single char tokens get filtered from FTS, should fall through to LIKE
    _results, total = tmp_db.search_volumes("Batman")
    assert total >= 1


def test_search_volumes_no_results(tmp_db: Database) -> None:
    results, total = tmp_db.search_volumes("NonExistentComic12345")
    assert total == 0
    assert results == []


def test_search_volumes_like_percent_is_literal(tmp_db: Database) -> None:
    """A query containing '%' must not be treated as a SQL wildcard.

    The query '%Batman' has no exact match and FTS returns nothing for it,
    so the LIKE fallback runs.  Without escaping, the pattern becomes
    '%%Batman%' which degrades to '%Batman%' and matches all Batman volumes.
    With proper escaping the pattern is '%\\%Batman%' which matches no volume.
    """
    results, _total = tmp_db.search_volumes("%Batman")
    assert results == []  # no volume named "%Batman"; must not match all


def test_search_volumes_exact_total_is_accurate(tmp_db: Database) -> None:
    """Total from exact match must reflect real row count, not page size."""
    # There is exactly 1 volume named 'Absolute Batman'
    results, total = tmp_db.search_volumes("Absolute Batman", limit=1)
    assert len(results) == 1
    assert total == 1  # not capped at limit


def test_search_volumes_like_total_is_accurate(tmp_db: Database) -> None:
    """Total from LIKE search must reflect real row count, not page size."""
    # conftest has 2 volumes both containing 'Batman'
    results, total = tmp_db.search_volumes("Batman", limit=1)
    assert len(results) == 1     # page is 1
    assert total == 2            # but total matching rows is 2


def test_get_volume(tmp_db: Database) -> None:
    vol = tmp_db.get_volume(160294)
    assert vol is not None
    assert vol["name"] == "Absolute Batman"
    assert vol["publisher"]["name"] == "DC Comics"
    assert vol["count_of_issues"] == 17


def test_get_volume_null_publisher(tmp_db: Database) -> None:
    """Volumes with no publisher return a stub dict, not None."""
    tmp_db.conn.execute(
        "INSERT INTO cv_volume (id, name, publisher_id) VALUES (999, 'Orphan Vol', NULL)"
    )
    tmp_db.conn.execute(
        "INSERT INTO volume_fts (rowid, name, aliases) VALUES (999, 'Orphan Vol', NULL)"
    )
    tmp_db.conn.commit()
    vol = tmp_db.get_volume(999)
    assert vol is not None
    assert vol["name"] == "Orphan Vol"
    assert vol["publisher"] == {"id": None, "name": None}


def test_get_volume_not_found(tmp_db: Database) -> None:
    assert tmp_db.get_volume(999999) is None


def test_get_volume_issues(tmp_db: Database) -> None:
    issues = tmp_db.get_volume_issues(160294)
    assert len(issues) == 2
    assert issues[0]["issue_number"] == "1"
    assert issues[1]["issue_number"] == "2"


def test_get_issue(tmp_db: Database) -> None:
    issue = tmp_db.get_issue(1073108)
    assert issue is not None
    assert issue["issue_number"] == "1"
    assert issue["cover_date"] == "2024-12-01"
    assert issue["store_date"] == "2024-11-06"
    assert issue["volume"]["name"] == "Absolute Batman"


def test_get_issue_not_found(tmp_db: Database) -> None:
    assert tmp_db.get_issue(999999) is None


def test_search_issues_by_volume(tmp_db: Database) -> None:
    results, total = tmp_db.search_issues(volume_ids=[160294])
    assert len(results) == 2
    assert total == 2


def test_search_issues_by_number(tmp_db: Database) -> None:
    results, total = tmp_db.search_issues(volume_ids=[160294], issue_number="1")
    assert len(results) == 1
    assert results[0]["id"] == 1073108
    assert total == 1


def test_search_issues_by_volume_list_single(tmp_db: Database) -> None:
    """volume_ids=[ID] with a volume that has no issues returns empty."""
    # volume 100001 exists but has no issues seeded
    results, total = tmp_db.search_issues(volume_ids=[100001])
    assert results == []
    assert total == 0


def test_search_issues_by_volume_list_multiple(tmp_db: Database) -> None:
    """volume_ids list with multiple entries returns issues only from matching volumes."""
    # 160294 has 2 issues; 100001 has 0; 999999 doesn't exist
    results, total = tmp_db.search_issues(volume_ids=[160294, 100001, 999999])
    assert total == 2
    assert len(results) == 2
    volume_ids_in_results = {r["volume"]["id"] for r in results}
    assert volume_ids_in_results == {160294}


def test_search_issues_by_volume_list_empty(tmp_db: Database) -> None:
    """Empty volume_ids list triggers safety guard."""
    results, total = tmp_db.search_issues(volume_ids=[])
    assert results == []
    assert total == 0


def test_search_issues_by_store_date_range(tmp_db: Database) -> None:
    # issue 1073108 has store_date='2024-11-06'
    results, total = tmp_db.search_issues(
        store_date_start="2024-11-01", store_date_end="2024-11-30"
    )
    assert len(results) == 1
    assert results[0]["id"] == 1073108
    assert total == 1


def test_search_issues_store_date_range_no_match(tmp_db: Database) -> None:
    results, total = tmp_db.search_issues(
        store_date_start="2026-01-01", store_date_end="2026-01-31"
    )
    assert results == []
    assert total == 0


def test_search_issues_no_filters_returns_empty(tmp_db: Database) -> None:
    """Safety guard: refuse full-table scan when no filters supplied."""
    results, total = tmp_db.search_issues()
    assert results == []
    assert total == 0


def test_get_publisher(tmp_db: Database) -> None:
    pub = tmp_db.get_publisher(1)
    assert pub is not None
    assert pub["name"] == "DC Comics"


def test_get_publisher_not_found(tmp_db: Database) -> None:
    assert tmp_db.get_publisher(999999) is None


def test_get_counts(tmp_db: Database) -> None:
    counts = tmp_db.get_counts()
    assert counts["volume"] == 2
    assert counts["issue"] == 2
    assert counts["publisher"] == 2


def test_get_volumes_by_ids_multiple(tmp_db: Database) -> None:
    results, total = tmp_db.get_volumes_by_ids([160294, 100001])
    assert len(results) == 2
    assert total == 2
    ids = {r["id"] for r in results}
    assert ids == {160294, 100001}


def test_get_volumes_by_ids_subset(tmp_db: Database) -> None:
    results, total = tmp_db.get_volumes_by_ids([160294])
    assert len(results) == 1
    assert results[0]["id"] == 160294
    assert total == 1


def test_get_volumes_by_ids_empty(tmp_db: Database) -> None:
    results, total = tmp_db.get_volumes_by_ids([])
    assert results == []
    assert total == 0


def test_get_volumes_by_ids_no_match(tmp_db: Database) -> None:
    results, total = tmp_db.get_volumes_by_ids([999999, 888888])
    assert results == []
    assert total == 0


# ---------------------------------------------------------------------------
# Response cache
# ---------------------------------------------------------------------------


def test_cache_put_and_get_single_round_trips(tmp_db: Database) -> None:
    tmp_db.cache_put_single("character", 12345, {"id": 12345, "name": "Batman"})
    result = tmp_db.cache_get_single("character", 12345)
    assert result == {"id": 12345, "name": "Batman"}


def test_cache_get_single_returns_none_on_miss(tmp_db: Database) -> None:
    assert tmp_db.cache_get_single("character", 99999) is None


def test_cache_put_single_overwrites_existing(tmp_db: Database) -> None:
    tmp_db.cache_put_single("character", 1, {"name": "Old"})
    tmp_db.cache_put_single("character", 1, {"name": "New"})
    assert tmp_db.cache_get_single("character", 1) == {"name": "New"}


def test_cache_put_and_get_list_round_trips(tmp_db: Database) -> None:
    items = [{"id": 1, "name": "Foo"}, {"id": 2, "name": "Bar"}]
    tmp_db.cache_put_list("characters", "abc123", items)
    assert tmp_db.cache_get_list("characters", "abc123") == items


def test_cache_get_list_returns_none_on_miss(tmp_db: Database) -> None:
    assert tmp_db.cache_get_list("characters", "nohash") is None


# ---------------------------------------------------------------------------
# Upsert methods
# ---------------------------------------------------------------------------


def test_upsert_publisher_creates_new_entry(tmp_db: Database) -> None:
    tmp_db.upsert_publisher({"id": 9999, "name": "Test Publisher"})
    pub = tmp_db.get_publisher(9999)
    assert pub is not None
    assert pub["name"] == "Test Publisher"


def test_upsert_volume_creates_new_entry(tmp_db: Database) -> None:
    data = {
        "id": 99999,
        "name": "Test Series",
        "start_year": "2024",
        "publisher": {"id": 1, "name": "DC Comics"},
        "count_of_issues": 5,
        "image": {"original_url": "https://example.com/img.jpg"},
        "site_detail_url": "https://comicvine.gamespot.com/test/",
    }
    tmp_db.upsert_volume(data)
    vol = tmp_db.get_volume(99999)
    assert vol is not None
    assert vol["name"] == "Test Series"
    assert vol["publisher"]["id"] == 1


def test_upsert_volume_replaces_existing_entry(tmp_db: Database) -> None:
    # Volume 160294 is pre-seeded as "Absolute Batman" in fixtures
    data = {"id": 160294, "name": "Updated Batman", "publisher": {}, "image": {}}
    tmp_db.upsert_volume(data)
    vol = tmp_db.get_volume(160294)
    assert vol is not None
    assert vol["name"] == "Updated Batman"


def test_upsert_issue_creates_new_entry(tmp_db: Database) -> None:
    data = {
        "id": 99001,
        "volume": {"id": 160294},
        "issue_number": "99",
        "name": "Test Issue",
        "cover_date": "2024-12-01",
        "store_date": "2024-12-04",
        "image": {"original_url": "https://example.com/cover.jpg"},
        "site_detail_url": "https://comicvine.gamespot.com/issue/99/",
    }
    tmp_db.upsert_issue(data)
    issue = tmp_db.get_issue(99001)
    assert issue is not None
    assert issue["issue_number"] == "99"
    assert issue["volume"]["id"] == 160294


# ---------------------------------------------------------------------------
# New fields round-trip tests
# ---------------------------------------------------------------------------


def test_upsert_issue_stores_new_fields(tmp_db: Database) -> None:
    """New cv_issue fields round-trip through upsert → get."""
    data = {
        "id": 88001,
        "volume": {"id": 160294},
        "issue_number": "10",
        "name": "New Fields Issue",
        "aliases": "Alias One\nAlias Two",
        "deck": "A short deck",
        "concept_credits": [{"id": 1, "name": "Time Travel"}],
        "object_credits": [{"id": 2, "name": "Infinity Gauntlet"}],
        "character_died_in": [{"id": 3, "name": "Robin"}],
        "team_disbanded_in": [{"id": 4, "name": "Justice League"}],
        "first_appearance_characters": [{"id": 5, "name": "New Hero"}],
        "first_appearance_concepts": [],
        "first_appearance_locations": [{"id": 6, "name": "Gotham"}],
        "first_appearance_objects": [],
        "first_appearance_storyarcs": [{"id": 7, "name": "Crisis"}],
        "first_appearance_teams": [],
        "has_staff_review": True,
        "api_detail_url": "https://comicvine.gamespot.com/api/issue/4000-88001/",
        "date_added": "2024-01-15 10:00:00",
        "date_last_updated": "2024-06-20 14:30:00",
        "image": {},
    }
    tmp_db.upsert_issue(data)
    issue = tmp_db.get_issue(88001)
    assert issue is not None

    # Scalars
    assert issue["aliases"] == "Alias One\nAlias Two"
    assert issue["deck"] == "A short deck"
    assert issue["api_detail_url"] == "https://comicvine.gamespot.com/api/issue/4000-88001/"
    assert issue["date_added"] == "2024-01-15 10:00:00"
    assert issue["date_last_updated"] == "2024-06-20 14:30:00"

    # Boolean
    assert issue["has_staff_review"] is True

    # JSON arrays
    assert issue["concept_credits"] == [{"id": 1, "name": "Time Travel"}]
    assert issue["object_credits"] == [{"id": 2, "name": "Infinity Gauntlet"}]
    assert issue["character_died_in"] == [{"id": 3, "name": "Robin"}]
    assert issue["team_disbanded_in"] == [{"id": 4, "name": "Justice League"}]
    assert issue["first_appearance_characters"] == [{"id": 5, "name": "New Hero"}]
    assert issue["first_appearance_concepts"] == []
    assert issue["first_appearance_locations"] == [{"id": 6, "name": "Gotham"}]
    assert issue["first_appearance_storyarcs"] == [{"id": 7, "name": "Crisis"}]
    assert issue["first_appearance_teams"] == []


def test_upsert_volume_stores_new_fields(tmp_db: Database) -> None:
    """New cv_volume fields round-trip through upsert → get."""
    data = {
        "id": 88002,
        "name": "New Fields Volume",
        "publisher": {"id": 1, "name": "DC Comics"},
        "characters": [{"id": 10, "name": "Batman"}],
        "concepts": [{"id": 11, "name": "Multiverse"}],
        "people": [{"id": 12, "name": "Scott Snyder"}],
        "objects": [{"id": 13, "name": "Batarang"}],
        "first_issue": {"id": 100, "name": "Issue 1"},
        "last_issue": {"id": 200, "name": "Issue 50"},
        "api_detail_url": "https://comicvine.gamespot.com/api/volume/4050-88002/",
        "date_added": "2023-05-01 08:00:00",
        "date_last_updated": "2024-11-15 12:00:00",
        "image": {},
    }
    tmp_db.upsert_volume(data)
    vol = tmp_db.get_volume(88002)
    assert vol is not None

    # Scalars
    assert vol["api_detail_url"] == "https://comicvine.gamespot.com/api/volume/4050-88002/"
    assert vol["date_added"] == "2023-05-01 08:00:00"
    assert vol["date_last_updated"] == "2024-11-15 12:00:00"

    # JSON arrays
    assert vol["characters"] == [{"id": 10, "name": "Batman"}]
    assert vol["concepts"] == [{"id": 11, "name": "Multiverse"}]
    assert vol["people"] == [{"id": 12, "name": "Scott Snyder"}]
    assert vol["objects"] == [{"id": 13, "name": "Batarang"}]

    # JSON objects (first_issue / last_issue)
    assert vol["first_issue"] == {"id": 100, "name": "Issue 1"}
    assert vol["last_issue"] == {"id": 200, "name": "Issue 50"}


def test_upsert_publisher_stores_new_fields(tmp_db: Database) -> None:
    """New cv_publisher fields round-trip through upsert → get."""
    data = {
        "id": 88003,
        "name": "New Publisher",
        "aliases": "NP\nNew Pub",
        "deck": "A great publisher",
        "description": "<p>Long description</p>",
        "location_address": "123 Main St",
        "location_city": "Metropolis",
        "location_state": "NY",
        "image": {"original_url": "https://example.com/pub.jpg"},
        "site_detail_url": "https://comicvine.gamespot.com/new-publisher/4010-88003/",
        "api_detail_url": "https://comicvine.gamespot.com/api/publisher/4010-88003/",
        "characters": [{"id": 20, "name": "Superman"}],
        "teams": [{"id": 21, "name": "Justice League"}],
        "volumes": [{"id": 22, "name": "Action Comics"}],
        "story_arcs": [{"id": 23, "name": "Crisis on Infinite Earths"}],
        "date_added": "2020-01-01 00:00:00",
        "date_last_updated": "2024-12-01 09:00:00",
    }
    tmp_db.upsert_publisher(data)
    pub = tmp_db.get_publisher(88003)
    assert pub is not None

    # Scalars
    assert pub["name"] == "New Publisher"
    assert pub["aliases"] == "NP\nNew Pub"
    assert pub["deck"] == "A great publisher"
    assert pub["description"] == "<p>Long description</p>"
    assert pub["location_address"] == "123 Main St"
    assert pub["location_city"] == "Metropolis"
    assert pub["location_state"] == "NY"
    assert pub["site_detail_url"] == "https://comicvine.gamespot.com/new-publisher/4010-88003/"
    assert pub["api_detail_url"] == "https://comicvine.gamespot.com/api/publisher/4010-88003/"
    assert pub["date_added"] == "2020-01-01 00:00:00"
    assert pub["date_last_updated"] == "2024-12-01 09:00:00"

    # Image
    assert pub["image"] is not None
    assert pub["image"]["original_url"] == "https://example.com/pub.jpg"

    # JSON arrays
    assert pub["characters"] == [{"id": 20, "name": "Superman"}]
    assert pub["teams"] == [{"id": 21, "name": "Justice League"}]
    assert pub["volumes"] == [{"id": 22, "name": "Action Comics"}]
    assert pub["story_arcs"] == [{"id": 23, "name": "Crisis on Infinite Earths"}]


def test_migration_adds_missing_columns(tmp_path: Path) -> None:
    """Auto-migration adds new columns to an old-schema database."""
    import sqlite3 as _sqlite3

    db_path = tmp_path / "old.db"
    conn = _sqlite3.connect(str(db_path))
    # Create old schema with only original columns
    conn.executescript("""
        CREATE TABLE cv_publisher (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE cv_volume (
            id INTEGER PRIMARY KEY, name TEXT, aliases TEXT, start_year TEXT,
            publisher_id INTEGER, count_of_issues INTEGER, deck TEXT,
            description TEXT, image_url TEXT, site_detail_url TEXT
        );
        CREATE TABLE cv_issue (
            id INTEGER PRIMARY KEY, volume_id INTEGER, name TEXT,
            issue_number TEXT, cover_date TEXT, store_date TEXT,
            description TEXT, image_url TEXT, site_detail_url TEXT,
            character_credits TEXT, person_credits TEXT, team_credits TEXT,
            location_credits TEXT, story_arc_credits TEXT, associated_images TEXT
        );
    """)
    conn.close()

    # Now open via Database which triggers _ensure_schema + _migrate_columns
    db = Database(db_path)
    db.connect()

    # Verify new columns exist
    for table, expected_cols in [
        ("cv_publisher", ["aliases", "deck", "description", "characters", "date_added"]),
        ("cv_volume", ["characters", "concepts", "first_issue", "date_added"]),
        ("cv_issue", ["aliases", "deck", "concept_credits", "has_staff_review", "date_added"]),
    ]:
        cur = db.conn.execute(f"PRAGMA table_info({table})")
        col_names = {row[1] for row in cur.fetchall()}
        for expected in expected_cols:
            assert expected in col_names, f"{expected} missing from {table}"

    db.close()


# ---------------------------------------------------------------------------
# last_accessed migration (Task 2)
# ---------------------------------------------------------------------------


def test_last_accessed_column_added_by_migration(tmp_path: Path) -> None:
    """Migration adds last_accessed to cv_volume and cv_issue on a pre-existing DB."""
    import sqlite3 as _sqlite3

    db_path = tmp_path / "old_no_la.db"
    conn = _sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE cv_publisher (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE cv_volume (
            id INTEGER PRIMARY KEY, name TEXT, date_last_updated TEXT
        );
        CREATE TABLE cv_issue (
            id INTEGER PRIMARY KEY, volume_id INTEGER, name TEXT,
            cover_date TEXT, store_date TEXT, date_last_updated TEXT
        );
        CREATE TABLE cv_response_cache (
            resource_type TEXT NOT NULL, resource_id INTEGER, params_hash TEXT,
            data_json TEXT NOT NULL, cached_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.close()

    db = Database(db_path)
    db.connect()

    for table in ("cv_volume", "cv_issue"):
        cur = db.conn.execute(f"PRAGMA table_info({table})")
        col_names = {row[1] for row in cur.fetchall()}
        assert "last_accessed" in col_names, f"last_accessed missing from {table}"

    db.close()


def test_touch_last_accessed_sets_timestamp(tmp_db: Database) -> None:
    """touch_last_accessed sets a non-null ISO timestamp on the given row."""
    tmp_db.touch_last_accessed("cv_issue", 1073108)
    row = tmp_db.conn.execute(
        "SELECT last_accessed FROM cv_issue WHERE id = 1073108"
    ).fetchone()
    assert row is not None
    assert row[0] is not None


def test_touch_last_accessed_volume_sets_timestamp(tmp_db: Database) -> None:
    """touch_last_accessed works for cv_volume rows too."""
    tmp_db.touch_last_accessed("cv_volume", 160294)
    row = tmp_db.conn.execute(
        "SELECT last_accessed FROM cv_volume WHERE id = 160294"
    ).fetchone()
    assert row is not None
    assert row[0] is not None


def test_touch_last_accessed_invalid_table_raises(tmp_db: Database) -> None:
    """touch_last_accessed raises ValueError for unsupported table names."""
    with pytest.raises(ValueError, match="unsupported table"):
        tmp_db.touch_last_accessed("cv_publisher", 1)


# ---------------------------------------------------------------------------
# is_past_cutoff helper (Task 3)
# ---------------------------------------------------------------------------


def test_is_past_cutoff_old_date_returns_true() -> None:
    assert is_past_cutoff("1990-01-01", 5) is True


def test_is_past_cutoff_recent_date_returns_false() -> None:
    assert is_past_cutoff("2025-01-01", 5) is False


def test_is_past_cutoff_disabled_returns_false() -> None:
    assert is_past_cutoff("1990-01-01", 0) is False


def test_is_past_cutoff_null_returns_false() -> None:
    assert is_past_cutoff(None, 5) is False


def test_is_past_cutoff_empty_string_returns_false() -> None:
    assert is_past_cutoff("", 5) is False


def test_is_past_cutoff_invalid_string_returns_false() -> None:
    assert is_past_cutoff("not-a-date", 5) is False


# ---------------------------------------------------------------------------
# DB eviction methods (Task 3)
# ---------------------------------------------------------------------------


def test_evict_stale_issues_removes_old_unaccessed(tmp_db: Database) -> None:
    """Issues with old cover_date and no last_accessed are deleted."""
    tmp_db.conn.execute(
        "INSERT INTO cv_issue (id, volume_id, name, issue_number, cover_date) "
        "VALUES (9001, 160294, 'Old Issue', '99', '1990-01-01')"
    )
    tmp_db.conn.commit()

    deleted = tmp_db.evict_stale_issues("2000-01-01", "2000-01-01 00:00:00")
    assert deleted >= 1

    row = tmp_db.conn.execute("SELECT id FROM cv_issue WHERE id = 9001").fetchone()
    assert row is None


def test_evict_stale_issues_keeps_recent(tmp_db: Database) -> None:
    """Issues with a recent cover_date are retained."""
    tmp_db.conn.execute(
        "INSERT INTO cv_issue (id, volume_id, name, issue_number, cover_date) "
        "VALUES (9002, 160294, 'Recent Issue', '100', '2024-01-01')"
    )
    tmp_db.conn.commit()

    tmp_db.evict_stale_issues("2000-01-01", "2000-01-01 00:00:00")

    row = tmp_db.conn.execute("SELECT id FROM cv_issue WHERE id = 9002").fetchone()
    assert row is not None


def test_evict_stale_issues_keeps_recently_accessed_old(tmp_db: Database) -> None:
    """Old issues that were recently accessed are retained."""
    tmp_db.conn.execute(
        "INSERT INTO cv_issue (id, volume_id, name, issue_number, cover_date, last_accessed) "
        "VALUES (9003, 160294, 'Old But Accessed', '101', '1990-01-01', datetime('now'))"
    )
    tmp_db.conn.commit()

    # access_expiry = 1 year ago – the issue was just accessed so it should survive
    past_expiry = "2000-01-01 00:00:00"
    tmp_db.evict_stale_issues("2000-01-01", past_expiry)

    row = tmp_db.conn.execute("SELECT id FROM cv_issue WHERE id = 9003").fetchone()
    assert row is not None


def test_evict_stale_issues_skips_null_cover_date(tmp_db: Database) -> None:
    """Issues with NULL cover_date are always retained (no date to compare)."""
    tmp_db.conn.execute(
        "INSERT INTO cv_issue (id, volume_id, name, issue_number, cover_date) "
        "VALUES (9004, 160294, 'No Date Issue', '102', NULL)"
    )
    tmp_db.conn.commit()

    tmp_db.evict_stale_issues("2000-01-01", "2000-01-01 00:00:00")

    row = tmp_db.conn.execute("SELECT id FROM cv_issue WHERE id = 9004").fetchone()
    assert row is not None


def test_evict_orphaned_volumes_removes_no_issue_volume(tmp_db: Database) -> None:
    """Volumes with no remaining issues and no recent access are deleted."""
    # Insert a volume with no issues
    tmp_db.conn.execute(
        "INSERT INTO cv_volume (id, name) VALUES (9901, 'Orphan Volume')"
    )
    tmp_db.conn.commit()

    deleted = tmp_db.evict_orphaned_volumes("2000-01-01 00:00:00")
    assert deleted >= 1

    row = tmp_db.conn.execute("SELECT id FROM cv_volume WHERE id = 9901").fetchone()
    assert row is None


def test_evict_orphaned_volumes_keeps_volume_with_issues(tmp_db: Database) -> None:
    """Volumes that still have issues are not evicted."""
    # volume 160294 has 2 issues in the fixture
    deleted_before = tmp_db.conn.execute(
        "SELECT COUNT(*) FROM cv_volume WHERE id = 160294"
    ).fetchone()[0]
    assert deleted_before == 1

    tmp_db.evict_orphaned_volumes("2000-01-01 00:00:00")

    row = tmp_db.conn.execute("SELECT id FROM cv_volume WHERE id = 160294").fetchone()
    assert row is not None


def test_evict_orphaned_volumes_keeps_recently_accessed(tmp_db: Database) -> None:
    """Volumes with no issues but a recent last_accessed are retained."""
    tmp_db.conn.execute(
        "INSERT INTO cv_volume (id, name, last_accessed) "
        "VALUES (9902, 'Recently Seen Orphan', datetime('now'))"
    )
    tmp_db.conn.commit()

    # access_expiry = 1 year ago → volume was just accessed → keep it
    tmp_db.evict_orphaned_volumes("2000-01-01 00:00:00")

    row = tmp_db.conn.execute("SELECT id FROM cv_volume WHERE id = 9902").fetchone()
    assert row is not None


def test_evict_response_cache_removes_old(tmp_db: Database) -> None:
    """Response cache rows older than cutoff are deleted."""
    tmp_db.conn.execute(
        "INSERT INTO cv_response_cache (resource_type, resource_id, data_json, cached_at) "
        "VALUES ('character', 5000, '{}', '2020-01-01 00:00:00')"
    )
    tmp_db.conn.commit()

    deleted = tmp_db.evict_response_cache("2021-01-01 00:00:00")
    assert deleted >= 1

    row = tmp_db.conn.execute(
        "SELECT 1 FROM cv_response_cache WHERE resource_id = 5000"
    ).fetchone()
    assert row is None


def test_evict_response_cache_keeps_recent(tmp_db: Database) -> None:
    """Response cache rows within TTL are retained."""
    tmp_db.conn.execute(
        "INSERT INTO cv_response_cache (resource_type, resource_id, data_json, cached_at) "
        "VALUES ('character', 5001, '{}', datetime('now'))"
    )
    tmp_db.conn.commit()

    tmp_db.evict_response_cache("2020-01-01 00:00:00")

    row = tmp_db.conn.execute(
        "SELECT 1 FROM cv_response_cache WHERE resource_id = 5001"
    ).fetchone()
    assert row is not None


def test_count_eviction_candidates_returns_correct_counts(tmp_db: Database) -> None:
    """count_eviction_candidates returns counts matching what evict_stale_issues would delete."""
    # Seed an old unaccessed issue
    tmp_db.conn.execute(
        "INSERT INTO cv_issue (id, volume_id, name, issue_number, cover_date) "
        "VALUES (9010, 160294, 'Old Count Issue', '200', '1990-01-01')"
    )
    tmp_db.conn.commit()

    cutoff = "2000-01-01"
    access_expiry = "2000-01-01 00:00:00"

    counts = tmp_db.count_eviction_candidates(cutoff, access_expiry)

    # Must include our seeded issue
    assert counts["issues"] >= 1
    assert isinstance(counts["volumes"], int)


# ---------------------------------------------------------------------------
# Task 1: cv_issue(volume_id) index
# ---------------------------------------------------------------------------


def test_issue_volume_id_index_exists(tmp_db: Database) -> None:
    """cv_issue must have an index on volume_id for efficient bulk lookup."""
    cur = tmp_db.conn.execute("PRAGMA index_list(cv_issue)")
    index_names = {row[1] for row in cur.fetchall()}
    assert "idx_issue_volume_id" in index_names


# ---------------------------------------------------------------------------
# Task 2a: query column migration for cv_response_cache
# ---------------------------------------------------------------------------


def test_query_column_added_by_migration(tmp_path: Path) -> None:
    """Migration adds the query column to a pre-existing cv_response_cache table."""
    import sqlite3 as _sqlite3

    db_path = tmp_path / "old_no_query.db"
    conn = _sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE cv_publisher (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE cv_volume (id INTEGER PRIMARY KEY, name TEXT, date_last_updated TEXT);
        CREATE TABLE cv_issue (
            id INTEGER PRIMARY KEY, volume_id INTEGER, name TEXT,
            cover_date TEXT, store_date TEXT, date_last_updated TEXT
        );
        CREATE TABLE cv_response_cache (
            resource_type TEXT NOT NULL, resource_id INTEGER, params_hash TEXT,
            data_json TEXT NOT NULL, cached_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.close()

    db = Database(db_path)
    db.connect()
    cur = db.conn.execute("PRAGMA table_info(cv_response_cache)")
    col_names = {row[1] for row in cur.fetchall()}
    assert "query" in col_names
    db.close()


# ---------------------------------------------------------------------------
# Task 2b: cache_put_search populates query column
# ---------------------------------------------------------------------------


def test_cache_put_search_stores_query_in_column(tmp_db: Database) -> None:
    """cache_put_search must write the query string to the dedicated column."""
    tmp_db.cache_put_search(
        "abc123", [{"id": 1, "name": "Batman"}], total=1, query="batman", resources="volume"
    )
    row = tmp_db.conn.execute(
        "SELECT query FROM cv_response_cache WHERE resource_type='search' AND params_hash='abc123'"
    ).fetchone()
    assert row is not None
    assert row[0] == "batman"


# ---------------------------------------------------------------------------
# Task 2c: cache_find_superset_search behaviour
# ---------------------------------------------------------------------------


def test_cache_find_superset_search_finds_broader_query(tmp_db: Database) -> None:
    """A complete 'batman' cache must satisfy 'absolute batman' searches."""
    batman_results = [
        {"id": 160294, "name": "Absolute Batman", "resource_type": "volume"},
        {"id": 100001, "name": "Batman", "resource_type": "volume"},
    ]
    tmp_db.cache_put_search(
        "hash_batman", batman_results, total=2, query="batman", resources="volume"
    )
    result = tmp_db.cache_find_superset_search("absolute batman", "volume")
    assert result is not None
    assert any(r["id"] == 160294 for r in result["results"])
    assert all("absolute batman" in (r.get("name") or "").lower() for r in result["results"])


def test_cache_find_superset_search_returns_none_when_no_match(tmp_db: Database) -> None:
    """No superset candidate → None."""
    tmp_db.cache_put_search(
        "hash_xmen", [{"id": 999, "name": "X-Men"}], total=1, query="xmen", resources="volume"
    )
    result = tmp_db.cache_find_superset_search("absolute batman", "volume")
    assert result is None


def test_cache_find_superset_search_skips_incomplete_cache(tmp_db: Database) -> None:
    """An incomplete cache (len(results) < total) must not be used as superset."""
    tmp_db.cache_put_search(
        "hash_bat_incomplete",
        [{"id": 160294, "name": "Absolute Batman"}],
        total=500,  # only 1 result stored but says 500 exist → incomplete
        query="batman",
        resources="volume",
    )
    result = tmp_db.cache_find_superset_search("absolute batman", "volume")
    assert result is None


def test_cache_find_superset_search_skips_different_resources(tmp_db: Database) -> None:
    """Superset cache for a different resource type must not be used."""
    tmp_db.cache_put_search(
        "hash_bat_issue", [{"id": 1, "name": "Batman #1"}], total=1, query="batman", resources="issue"
    )
    result = tmp_db.cache_find_superset_search("absolute batman", "volume")
    assert result is None


def test_cache_find_superset_search_skips_exact_match(tmp_db: Database) -> None:
    """Exact query match must not be returned as a superset entry."""
    tmp_db.cache_put_search(
        "hash_abs_bat",
        [{"id": 160294, "name": "Absolute Batman"}],
        total=1,
        query="absolute batman",
        resources="volume",
    )
    result = tmp_db.cache_find_superset_search("absolute batman", "volume")
    assert result is None
