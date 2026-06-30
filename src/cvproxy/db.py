"""SQLite database access layer for the local ComicVine cache.

This module manages localcv.db, which CVProxy populates itself from upstream
ComicVine API responses (cache-miss write-through).  When an existing DB
created by an external tool (e.g. an older sqlite_cv_updater.py) is mounted
into the container, publisher IDs may not match ComicVine's canonical IDs;
use POST /admin/repair/publishers to correct them.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

try:
    import pysqlite3 as sqlite3  # bundled SQLite with FTS5 support
except ImportError:
    import sqlite3  # type: ignore[no-redef]
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

CVVolume = dict[str, Any]
CVIssue = dict[str, Any]
CVPublisher = dict[str, Any]


def _to_json(val: Any) -> str | None:
    if not val:
        return None
    return json.dumps(val)

# ---------------------------------------------------------------------------
# Column lists — must match the cv_volume / cv_issue / cv_publisher schema below
# ---------------------------------------------------------------------------

_VOLUME_COLS = (
    "v.id, v.name, v.aliases, v.start_year, v.publisher_id, "
    "v.count_of_issues, v.deck, v.description, v.image_url, v.site_detail_url, "
    "v.characters, v.concepts, v.people, v.objects, "
    "v.first_issue, v.last_issue, "
    "v.api_detail_url, v.date_added, v.date_last_updated"
)

_VOLUME_JOIN = """
    FROM cv_volume v
    LEFT JOIN cv_publisher p ON v.publisher_id = p.id
"""

_VOLUME_SELECT = f"SELECT {_VOLUME_COLS}, p.name AS publisher_name {_VOLUME_JOIN}"

_VOLUME_SELECT_FTS = """
    SELECT v.id, v.name, v.aliases, v.start_year, v.publisher_id,
           v.count_of_issues, v.deck, v.description, v.image_url, v.site_detail_url,
           v.characters, v.concepts, v.people, v.objects,
           v.first_issue, v.last_issue,
           v.api_detail_url, v.date_added, v.date_last_updated,
           p.name AS publisher_name
    FROM volume_fts f
    JOIN cv_volume v ON f.rowid = v.id
    LEFT JOIN cv_publisher p ON v.publisher_id = p.id
"""

_ISSUE_COLS = (
    "id, volume_id, name, issue_number, cover_date, store_date, "
    "description, image_url, site_detail_url, "
    "character_credits, person_credits, team_credits, "
    "location_credits, story_arc_credits, associated_images, "
    "aliases, deck, concept_credits, object_credits, "
    "character_died_in, team_disbanded_in, "
    "first_appearance_characters, first_appearance_concepts, "
    "first_appearance_locations, first_appearance_objects, "
    "first_appearance_storyarcs, first_appearance_teams, "
    "has_staff_review, api_detail_url, date_added, date_last_updated"
)

_PUBLISHER_COLS = (
    "id, name, aliases, deck, description, "
    "location_address, location_city, location_state, "
    "image_url, site_detail_url, api_detail_url, "
    "characters, teams, volumes, story_arcs, "
    "date_added, date_last_updated"
)

# ---------------------------------------------------------------------------
# Schema DDL — auto-created on first run if tables are missing
# ---------------------------------------------------------------------------

_SCHEMA_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS cv_publisher (
    id INTEGER PRIMARY KEY,
    name TEXT,
    aliases TEXT,
    deck TEXT,
    description TEXT,
    location_address TEXT,
    location_city TEXT,
    location_state TEXT,
    image_url TEXT,
    site_detail_url TEXT,
    api_detail_url TEXT,
    characters TEXT,
    teams TEXT,
    volumes TEXT,
    story_arcs TEXT,
    date_added TEXT,
    date_last_updated TEXT,
    backfill_missing INTEGER
);

CREATE TABLE IF NOT EXISTS cv_volume (
    id INTEGER PRIMARY KEY,
    name TEXT,
    aliases TEXT,
    start_year TEXT,
    publisher_id INTEGER REFERENCES cv_publisher(id),
    count_of_issues INTEGER,
    deck TEXT,
    description TEXT,
    image_url TEXT,
    site_detail_url TEXT,
    characters TEXT,
    concepts TEXT,
    people TEXT,
    objects TEXT,
    first_issue TEXT,
    last_issue TEXT,
    api_detail_url TEXT,
    date_added TEXT,
    date_last_updated TEXT,
    backfill_missing INTEGER,
    last_accessed TEXT
);

CREATE TABLE IF NOT EXISTS cv_issue (
    id INTEGER PRIMARY KEY,
    volume_id INTEGER REFERENCES cv_volume(id),
    name TEXT,
    issue_number TEXT,
    cover_date TEXT,
    store_date TEXT,
    description TEXT,
    image_url TEXT,
    site_detail_url TEXT,
    character_credits TEXT,
    person_credits TEXT,
    team_credits TEXT,
    location_credits TEXT,
    story_arc_credits TEXT,
    associated_images TEXT,
    aliases TEXT,
    deck TEXT,
    concept_credits TEXT,
    object_credits TEXT,
    character_died_in TEXT,
    team_disbanded_in TEXT,
    first_appearance_characters TEXT,
    first_appearance_concepts TEXT,
    first_appearance_locations TEXT,
    first_appearance_objects TEXT,
    first_appearance_storyarcs TEXT,
    first_appearance_teams TEXT,
    has_staff_review INTEGER,
    api_detail_url TEXT,
    date_added TEXT,
    date_last_updated TEXT,
    backfill_missing INTEGER,
    last_accessed TEXT
);

CREATE TABLE IF NOT EXISTS cv_response_cache (
    resource_type TEXT NOT NULL,
    resource_id   INTEGER,
    params_hash   TEXT,
    data_json     TEXT NOT NULL,
    cached_at     TEXT NOT NULL DEFAULT (datetime('now')),
    query         TEXT
);
"""

# Indexes are created AFTER column migrations run so that new columns (e.g.
# cv_response_cache.query) exist before we attempt to index them.
_SCHEMA_INDEXES_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_resp_cache_single
    ON cv_response_cache(resource_type, resource_id)
    WHERE resource_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_resp_cache_list
    ON cv_response_cache(resource_type, params_hash)
    WHERE params_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_resp_cache_search_query
    ON cv_response_cache(query)
    WHERE resource_type = 'search' AND query IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_issue_store_date ON cv_issue(store_date)
    WHERE store_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_issue_volume_id ON cv_issue(volume_id);
"""

_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS volume_fts
USING fts5(name, aliases, content=cv_volume, content_rowid=id);

CREATE VIRTUAL TABLE IF NOT EXISTS issue_fts
USING fts5(name, aliases, content=cv_issue, content_rowid=id);

CREATE VIRTUAL TABLE IF NOT EXISTS publisher_fts
USING fts5(name, aliases, content=cv_publisher, content_rowid=id);
"""

_FTS_TRIGGERS_DDL = """
CREATE TRIGGER IF NOT EXISTS volume_fts_ai
AFTER INSERT ON cv_volume BEGIN
    INSERT INTO volume_fts(rowid, name, aliases) VALUES (new.id, new.name, new.aliases);
END;
CREATE TRIGGER IF NOT EXISTS volume_fts_au
AFTER UPDATE ON cv_volume BEGIN
    INSERT INTO volume_fts(volume_fts, rowid, name, aliases)
        VALUES ('delete', old.id, old.name, old.aliases);
    INSERT INTO volume_fts(rowid, name, aliases) VALUES (new.id, new.name, new.aliases);
END;
CREATE TRIGGER IF NOT EXISTS volume_fts_ad
AFTER DELETE ON cv_volume BEGIN
    INSERT INTO volume_fts(volume_fts, rowid, name, aliases)
        VALUES ('delete', old.id, old.name, old.aliases);
END;

CREATE TRIGGER IF NOT EXISTS issue_fts_ai
AFTER INSERT ON cv_issue BEGIN
    INSERT INTO issue_fts(rowid, name, aliases) VALUES (new.id, new.name, new.aliases);
END;
CREATE TRIGGER IF NOT EXISTS issue_fts_au
AFTER UPDATE ON cv_issue BEGIN
    INSERT INTO issue_fts(issue_fts, rowid, name, aliases)
        VALUES ('delete', old.id, old.name, old.aliases);
    INSERT INTO issue_fts(rowid, name, aliases) VALUES (new.id, new.name, new.aliases);
END;
CREATE TRIGGER IF NOT EXISTS issue_fts_ad
AFTER DELETE ON cv_issue BEGIN
    INSERT INTO issue_fts(issue_fts, rowid, name, aliases)
        VALUES ('delete', old.id, old.name, old.aliases);
END;

CREATE TRIGGER IF NOT EXISTS publisher_fts_ai
AFTER INSERT ON cv_publisher BEGIN
    INSERT INTO publisher_fts(rowid, name, aliases) VALUES (new.id, new.name, new.aliases);
END;
CREATE TRIGGER IF NOT EXISTS publisher_fts_au
AFTER UPDATE ON cv_publisher BEGIN
    INSERT INTO publisher_fts(publisher_fts, rowid, name, aliases)
        VALUES ('delete', old.id, old.name, old.aliases);
    INSERT INTO publisher_fts(rowid, name, aliases) VALUES (new.id, new.name, new.aliases);
END;
CREATE TRIGGER IF NOT EXISTS publisher_fts_ad
AFTER DELETE ON cv_publisher BEGIN
    INSERT INTO publisher_fts(publisher_fts, rowid, name, aliases)
        VALUES ('delete', old.id, old.name, old.aliases);
END;
"""

# ---------------------------------------------------------------------------
# Auto-migration — add columns to existing databases
# ---------------------------------------------------------------------------

_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "cv_response_cache": [
        ("query", "TEXT"),
    ],
    "cv_publisher": [
        ("aliases", "TEXT"),
        ("deck", "TEXT"),
        ("description", "TEXT"),
        ("location_address", "TEXT"),
        ("location_city", "TEXT"),
        ("location_state", "TEXT"),
        ("image_url", "TEXT"),
        ("site_detail_url", "TEXT"),
        ("api_detail_url", "TEXT"),
        ("characters", "TEXT"),
        ("teams", "TEXT"),
        ("volumes", "TEXT"),
        ("story_arcs", "TEXT"),
        ("date_added", "TEXT"),
        ("date_last_updated", "TEXT"),
        ("backfill_missing", "INTEGER"),
    ],
    "cv_volume": [
        ("characters", "TEXT"),
        ("concepts", "TEXT"),
        ("people", "TEXT"),
        ("objects", "TEXT"),
        ("first_issue", "TEXT"),
        ("last_issue", "TEXT"),
        ("api_detail_url", "TEXT"),
        ("date_added", "TEXT"),
        ("date_last_updated", "TEXT"),
        ("last_accessed", "TEXT"),
        ("backfill_missing", "INTEGER"),
    ],
    "cv_issue": [
        ("aliases", "TEXT"),
        ("deck", "TEXT"),
        ("concept_credits", "TEXT"),
        ("object_credits", "TEXT"),
        ("character_died_in", "TEXT"),
        ("team_disbanded_in", "TEXT"),
        ("first_appearance_characters", "TEXT"),
        ("first_appearance_concepts", "TEXT"),
        ("first_appearance_locations", "TEXT"),
        ("first_appearance_objects", "TEXT"),
        ("first_appearance_storyarcs", "TEXT"),
        ("first_appearance_teams", "TEXT"),
        ("has_staff_review", "INTEGER"),
        ("api_detail_url", "TEXT"),
        ("date_added", "TEXT"),
        ("date_last_updated", "TEXT"),
        ("last_accessed", "TEXT"),
        ("backfill_missing", "INTEGER"),
    ],
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """Add missing columns to existing tables.

    Uses PRAGMA table_info to detect which columns already exist,
    then runs ALTER TABLE ADD COLUMN for any that are missing.
    """
    for table, columns in _MIGRATIONS.items():
        cur = conn.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        for col_name, col_type in columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                logger.info("Migrated: ALTER TABLE %s ADD COLUMN %s %s", table, col_name, col_type)
    conn.commit()

# ---------------------------------------------------------------------------
# Eviction helpers
# ---------------------------------------------------------------------------

#: Tables that support last_accessed tracking and touch operations.
_TOUCHABLE_TABLES: frozenset[str] = frozenset({"cv_volume", "cv_issue"})


def is_past_cutoff(date_str: str | None, cutoff_years: int) -> bool:
    """Return True if date_str is older than cutoff_years ago.

    Returns False when disabled (cutoff_years=0), date_str is None/empty,
    or the string cannot be parsed as a date.
    """
    if not cutoff_years or not date_str:
        return False
    try:
        cutoff = date.today() - timedelta(days=cutoff_years * 365)
        return date.fromisoformat(str(date_str)[:10]) < cutoff
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Row → dict helpers
# ---------------------------------------------------------------------------


def _row_to_volume(row: sqlite3.Row) -> CVVolume:
    return {
        "id": row["id"],
        "name": row["name"],
        "aliases": row["aliases"],
        "start_year": row["start_year"],
        "publisher": (
            {"id": row["publisher_id"], "name": row["publisher_name"]}
            if row["publisher_id"]
            else {"id": None, "name": None}
        ),
        "count_of_issues": row["count_of_issues"],
        "deck": row["deck"],
        "description": row["description"],
        "image": _image_dict(row["image_url"]),
        "site_detail_url": row["site_detail_url"],
        "characters": _parse_json_col(row, "characters"),
        "concepts": _parse_json_col(row, "concepts"),
        "people": _parse_json_col(row, "people"),
        "objects": _parse_json_col(row, "objects"),
        "first_issue": _parse_json_col(row, "first_issue") or None,
        "last_issue": _parse_json_col(row, "last_issue") or None,
        "api_detail_url": row["api_detail_url"],
        "date_added": row["date_added"],
        "date_last_updated": row["date_last_updated"],
    }


def _row_to_issue(row: sqlite3.Row) -> CVIssue:
    has_review = row["has_staff_review"]
    return {
        "id": row["id"],
        "volume": {"id": row["volume_id"]},
        "name": row["name"],
        "issue_number": row["issue_number"],
        "aliases": row["aliases"],
        "deck": row["deck"],
        "cover_date": row["cover_date"],
        "store_date": row["store_date"],
        "description": row["description"],
        "image": _image_dict(row["image_url"]),
        "site_detail_url": row["site_detail_url"],
        "api_detail_url": row["api_detail_url"],
        "date_added": row["date_added"],
        "date_last_updated": row["date_last_updated"],
        "has_staff_review": bool(has_review) if has_review is not None else None,
        "character_credits": _parse_json_col(row, "character_credits"),
        "person_credits": _parse_json_col(row, "person_credits"),
        "team_credits": _parse_json_col(row, "team_credits"),
        "location_credits": _parse_json_col(row, "location_credits"),
        "story_arc_credits": _parse_json_col(row, "story_arc_credits"),
        "concept_credits": _parse_json_col(row, "concept_credits"),
        "object_credits": _parse_json_col(row, "object_credits"),
        "character_died_in": _parse_json_col(row, "character_died_in"),
        "team_disbanded_in": _parse_json_col(row, "team_disbanded_in"),
        "first_appearance_characters": _parse_json_col(row, "first_appearance_characters"),
        "first_appearance_concepts": _parse_json_col(row, "first_appearance_concepts"),
        "first_appearance_locations": _parse_json_col(row, "first_appearance_locations"),
        "first_appearance_objects": _parse_json_col(row, "first_appearance_objects"),
        "first_appearance_storyarcs": _parse_json_col(row, "first_appearance_storyarcs"),
        "first_appearance_teams": _parse_json_col(row, "first_appearance_teams"),
        "associated_images": _parse_json_col(row, "associated_images"),
    }


def _row_to_publisher(row: sqlite3.Row) -> CVPublisher:
    rd = dict(row)
    return {
        "id": row["id"],
        "name": row["name"],
        "aliases": rd.get("aliases"),
        "deck": rd.get("deck"),
        "description": rd.get("description"),
        "location_address": rd.get("location_address"),
        "location_city": rd.get("location_city"),
        "location_state": rd.get("location_state"),
        "image": _image_dict(rd.get("image_url")),
        "site_detail_url": rd.get("site_detail_url"),
        "api_detail_url": rd.get("api_detail_url"),
        "characters": _parse_json_col(row, "characters"),
        "teams": _parse_json_col(row, "teams"),
        "volumes": _parse_json_col(row, "volumes"),
        "story_arcs": _parse_json_col(row, "story_arcs"),
        "date_added": rd.get("date_added"),
        "date_last_updated": rd.get("date_last_updated"),
    }


def _image_dict(url: str | None) -> dict[str, str] | None:
    if not url:
        return None
    def _sized(size: str) -> str:
        return url.replace("/original/", f"/{size}/")
    return {
        "icon_url": _sized("square_avatar"),
        "medium_url": _sized("scale_medium"),
        "screen_url": _sized("scale_large"),
        "screen_large_url": _sized("screen_kubrick"),
        "small_url": _sized("scale_small"),
        "super_url": _sized("scale_super"),
        "thumb_url": _sized("scale_avatar"),
        "tiny_url": _sized("square_mini"),
        "original_url": url,
        "image_tags": None,
    }


def _parse_json_col(row: sqlite3.Row, col: str) -> list[dict[str, Any]]:
    raw = row[col]
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Database connection manager
# ---------------------------------------------------------------------------


class Database:
    """Thin wrapper around a SQLite connection to the localcv.db file."""

    def __init__(self, db_path: Path, cutoff_year: int = 0) -> None:
        self._path = db_path
        self._cutoff_year = cutoff_year
        self._conn: sqlite3.Connection | None = None

    def _is_before_cutoff(self, date_str: str | None) -> bool:
        """Return True when date_str's year is strictly before the configured cutoff."""
        if not self._cutoff_year or not date_str:
            return False
        try:
            return int(str(date_str)[:4]) < self._cutoff_year
        except (ValueError, TypeError):
            return False

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-131072")
        self._conn.execute("PRAGMA mmap_size=1073741824")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables, migrate columns, then create indexes.

        Order matters: indexes that reference migrated columns (e.g.
        idx_resp_cache_search_query on cv_response_cache.query) must be created
        *after* the migration step adds those columns to existing databases.
        """
        self.conn.executescript(_SCHEMA_TABLES_DDL)
        _migrate_columns(self.conn)
        self.conn.executescript(_SCHEMA_INDEXES_DDL)
        try:
            self._setup_fts()
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 setup failed — full-text search unavailable: %s", exc)

    def _setup_fts(self) -> None:
        """Create the FTS virtual table and sync triggers.

        Handles a common failure mode where the FTS5 shadow tables
        (volume_fts_data, volume_fts_idx, etc.) survived a previous partial or
        interrupted setup but the virtual-table row is absent from sqlite_master.
        In that case SQLite raises "table 'volume_fts_data' already exists" when
        we try to recreate the virtual table.  We detect this, drop the orphaned
        shadow tables, and let SQLite build everything from scratch.
        """
        _FTS_SHADOW_TABLES = (
            "volume_fts_data", "volume_fts_idx", "volume_fts_content",
            "volume_fts_docsize", "volume_fts_config",
            "issue_fts_data", "issue_fts_idx", "issue_fts_content",
            "issue_fts_docsize", "issue_fts_config",
            "publisher_fts_data", "publisher_fts_idx", "publisher_fts_content",
            "publisher_fts_docsize", "publisher_fts_config",
        )
        needs_rebuild = False
        try:
            self.conn.executescript(_FTS_DDL)
        except sqlite3.OperationalError as exc:
            if "already exists" not in str(exc):
                raise
            logger.warning(
                "Stale FTS shadow tables detected (%s) — dropping and recreating", exc
            )
            for tbl in _FTS_SHADOW_TABLES:
                self.conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            self.conn.commit()
            self.conn.executescript(_FTS_DDL)
            needs_rebuild = True

        triggers_before = self._fts_trigger_count()
        self.conn.executescript(_FTS_TRIGGERS_DDL)
        if needs_rebuild or self._fts_trigger_count() > triggers_before:
            logger.info("Rebuilding FTS indexes (volume_fts, issue_fts, publisher_fts)")
            self.conn.execute("INSERT INTO volume_fts(volume_fts) VALUES ('rebuild')")
            self.conn.execute("INSERT INTO issue_fts(issue_fts) VALUES ('rebuild')")
            self.conn.execute("INSERT INTO publisher_fts(publisher_fts) VALUES ('rebuild')")
            self.conn.commit()
            logger.info("FTS index rebuild complete")

    def _fts_trigger_count(self) -> int:
        """Return the number of FTS sync triggers that currently exist across all tables."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN ("
            "'volume_fts_ai', 'volume_fts_au', 'volume_fts_ad',"
            "'issue_fts_ai', 'issue_fts_au', 'issue_fts_ad',"
            "'publisher_fts_ai', 'publisher_fts_au', 'publisher_fts_ad')"
        )
        return cur.fetchone()[0]

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected — call connect() first")
        return self._conn

    # -- eviction ------------------------------------------------------------

    def touch_last_accessed(self, table: str, entity_id: int) -> None:
        """Update last_accessed to now for the given entity. Used by eviction system."""
        if table not in _TOUCHABLE_TABLES:
            raise ValueError(f"touch_last_accessed: unsupported table {table!r}")
        self.conn.execute(
            f"UPDATE {table} SET last_accessed = datetime('now') WHERE id = ?",  # noqa: S608
            [entity_id],
        )
        self.conn.commit()

    def evict_stale_issues(self, cutoff_date: str, access_expiry: str) -> int:
        """Delete issues older than cutoff_date that haven't been accessed since access_expiry.

        Skips issues with NULL cover_date. Returns number of rows deleted.
        """
        cur = self.conn.execute(
            """DELETE FROM cv_issue
               WHERE cover_date IS NOT NULL
                 AND cover_date < ?
                 AND (last_accessed IS NULL OR last_accessed < ?)""",
            [cutoff_date, access_expiry],
        )
        self.conn.commit()
        return cur.rowcount

    def evict_orphaned_volumes(self, access_expiry: str) -> int:
        """Delete volumes with no remaining issues that haven't been recently accessed.

        Run after evict_stale_issues to cascade-clean orphaned volume rows.
        Returns number of rows deleted.
        """
        cur = self.conn.execute(
            """DELETE FROM cv_volume
               WHERE id NOT IN (
                   SELECT DISTINCT volume_id FROM cv_issue WHERE volume_id IS NOT NULL
               )
               AND (last_accessed IS NULL OR last_accessed < ?)""",
            [access_expiry],
        )
        self.conn.commit()
        return cur.rowcount

    def evict_response_cache(self, cutoff_date: str, search_cutoff_date: str | None = None) -> int:
        """Delete cv_response_cache rows older than cutoff_date.

        Search entries use search_cutoff_date if provided (typically a longer TTL).
        Returns rows deleted.
        """
        if search_cutoff_date:
            cur = self.conn.execute(
                """DELETE FROM cv_response_cache WHERE
                   (resource_type != 'search' AND cached_at < ?)
                   OR (resource_type = 'search' AND cached_at < ?)""",
                [cutoff_date, search_cutoff_date],
            )
        else:
            cur = self.conn.execute(
                "DELETE FROM cv_response_cache WHERE cached_at < ?", [cutoff_date]
            )
        self.conn.commit()
        return cur.rowcount

    def count_eviction_candidates(self, cutoff_date: str, access_expiry: str) -> dict[str, int]:
        """Return counts of rows that would be removed by eviction (for dry-run preview)."""
        issue_cur = self.conn.execute(
            """SELECT COUNT(*) FROM cv_issue
               WHERE cover_date IS NOT NULL
                 AND cover_date < ?
                 AND (last_accessed IS NULL OR last_accessed < ?)""",
            [cutoff_date, access_expiry],
        )
        vol_cur = self.conn.execute(
            """SELECT COUNT(*) FROM cv_volume
               WHERE id NOT IN (
                   SELECT DISTINCT volume_id FROM cv_issue WHERE volume_id IS NOT NULL
               )
               AND (last_accessed IS NULL OR last_accessed < ?)""",
            [access_expiry],
        )
        return {"issues": issue_cur.fetchone()[0], "volumes": vol_cur.fetchone()[0]}

    # -- volumes -------------------------------------------------------------

    def search_volumes(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[CVVolume], int]:
        """Search volumes by name. Returns (results, total_count)."""
        # Try exact match first
        cur = self.conn.execute(
            _VOLUME_SELECT + " WHERE v.name = ? ORDER BY v.start_year DESC LIMIT ? OFFSET ?",
            [query, limit, offset],
        )
        rows = cur.fetchall()
        # Escape query for LIKE (used by both exact-match and LIKE COUNT below)
        safe = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if rows:
            # Total = all volumes whose name contains the query (LIKE semantics),
            # so paginating clients see the full result set size.
            count_cur = self.conn.execute(
                "SELECT COUNT(*) FROM cv_volume WHERE name LIKE ? ESCAPE '\\'",
                [f"%{safe}%"],
            )
            total: int = count_cur.fetchone()[0]
            return [_row_to_volume(r) for r in rows], total

        # Try FTS5 with a COUNT query for accurate total
        rows = self._search_volumes_fts(query, limit, offset)
        if rows:
            fts_total = self._count_volumes_fts(query)
            return [_row_to_volume(r) for r in rows], fts_total

        # LIKE fallback with accurate COUNT
        rows = self._search_volumes_like(query, limit, offset)
        if not rows:
            return [], 0
        count_cur = self.conn.execute(
            "SELECT COUNT(*) FROM cv_volume WHERE name LIKE ? ESCAPE '\\'",
            [f"%{safe}%"],
        )
        total = count_cur.fetchone()[0]
        return [_row_to_volume(r) for r in rows], total

    def count_volumes_fts(self, query: str) -> int:
        """Public wrapper for FTS volume count — used by the search route stale check."""
        return self._count_volumes_fts(query)

    def _search_volumes_fts(self, query: str, limit: int, offset: int) -> list[sqlite3.Row]:
        tokens = query.split()
        # FTS5 requires tokens > 1 char
        fts_tokens = [t for t in tokens if len(t) > 1]
        if not fts_tokens:
            return []
        fts_query = " ".join(fts_tokens)
        try:
            cur = self.conn.execute(
                _VOLUME_SELECT_FTS + " WHERE f.volume_fts MATCH ? LIMIT ? OFFSET ?",
                [fts_query, limit, offset],
            )
            return cur.fetchall()
        except sqlite3.OperationalError:
            return []

    def _count_volumes_fts(self, query: str) -> int:
        """Return the total number of volumes matching an FTS5 query."""
        tokens = query.split()
        fts_tokens = [t for t in tokens if len(t) > 1]
        if not fts_tokens:
            return 0
        fts_query = " ".join(fts_tokens)
        try:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM volume_fts WHERE volume_fts MATCH ?",
                [fts_query],
            )
            return cur.fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    def _search_volumes_like(self, query: str, limit: int, offset: int) -> list[sqlite3.Row]:
        # Escape SQL LIKE special characters in the user query
        safe = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{safe}%"
        cur = self.conn.execute(
            _VOLUME_SELECT + " WHERE v.name LIKE ? ESCAPE '\\' ORDER BY v.start_year DESC LIMIT ? OFFSET ?",
            [pattern, limit, offset],
        )
        return cur.fetchall()

    def get_volume(self, volume_id: int) -> CVVolume | None:
        """Get a single volume by ID."""
        cur = self.conn.execute(_VOLUME_SELECT + " WHERE v.id = ?", [volume_id])
        row = cur.fetchone()
        return _row_to_volume(row) if row else None

    def get_volumes_by_ids(self, ids: list[int]) -> tuple[list[CVVolume], int]:
        """Fetch multiple volumes by their IDs. Returns (results, total).

        Returns all matching rows without pagination — callers should ensure
        *ids* is reasonably sized (≤100) to avoid large result sets.
        """
        if not ids:
            return [], 0
        placeholders = ",".join("?" * len(ids))
        cur = self.conn.execute(
            _VOLUME_SELECT + f" WHERE v.id IN ({placeholders})",
            ids,
        )
        rows = cur.fetchall()
        return [_row_to_volume(r) for r in rows], len(rows)

    def get_volume_issues(
        self,
        volume_id: int,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[CVIssue]:
        """Get all issues for a volume."""
        cur = self.conn.execute(
            f"SELECT {_ISSUE_COLS} FROM cv_issue "
            "WHERE volume_id = ? ORDER BY CAST(issue_number AS INTEGER) "
            "LIMIT ? OFFSET ?",
            [volume_id, limit, offset],
        )
        return [_row_to_issue(r) for r in cur.fetchall()]

    # -- issues --------------------------------------------------------------

    def _search_entity_by_name(
        self,
        query: str,
        *,
        table: str,
        cols: str,
        fts_table: str | None,
        row_fn: Any,
        limit: int,
        offset: int,
    ) -> tuple[list[Any], int]:
        """Exact → FTS5 → LIKE search over any entity table.

        *table*, *cols*, and *fts_table* are hardcoded at every call site —
        never derived from user input — so f-string interpolation is safe.
        """
        safe = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        cur = self.conn.execute(  # noqa: S608
            f"SELECT {cols} FROM {table} WHERE name = ? LIMIT ? OFFSET ?",
            [query, limit, offset],
        )
        rows = cur.fetchall()
        if rows:
            count_cur = self.conn.execute(  # noqa: S608
                f"SELECT COUNT(*) FROM {table} WHERE name LIKE ? ESCAPE '\\'",
                [f"%{safe}%"],
            )
            return [row_fn(r) for r in rows], count_cur.fetchone()[0]

        if fts_table:
            fts_tokens = [t for t in query.split() if len(t) > 1]
            if fts_tokens:
                fts_query = " ".join(fts_tokens)
                try:
                    cur = self.conn.execute(  # noqa: S608
                        f"SELECT t.* FROM {fts_table} f "
                        f"JOIN {table} t ON f.rowid = t.id "
                        f"WHERE f.{fts_table} MATCH ? LIMIT ? OFFSET ?",
                        [fts_query, limit, offset],
                    )
                    rows = cur.fetchall()
                    if rows:
                        count_cur = self.conn.execute(  # noqa: S608
                            f"SELECT COUNT(*) FROM {fts_table} WHERE {fts_table} MATCH ?",
                            [fts_query],
                        )
                        return [row_fn(r) for r in rows], count_cur.fetchone()[0]
                except sqlite3.OperationalError:
                    pass

        cur = self.conn.execute(  # noqa: S608
            f"SELECT {cols} FROM {table} WHERE name LIKE ? ESCAPE '\\' LIMIT ? OFFSET ?",
            [f"%{safe}%", limit, offset],
        )
        rows = cur.fetchall()
        if not rows:
            return [], 0
        count_cur = self.conn.execute(  # noqa: S608
            f"SELECT COUNT(*) FROM {table} WHERE name LIKE ? ESCAPE '\\'",
            [f"%{safe}%"],
        )
        return [row_fn(r) for r in rows], count_cur.fetchone()[0]

    def search_issues_by_name(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[CVIssue], int]:
        """Search issues by name using FTS5 with LIKE fallback."""
        return self._search_entity_by_name(  # type: ignore[return-value]
            query,
            table="cv_issue",
            cols=_ISSUE_COLS,
            fts_table="issue_fts",
            row_fn=_row_to_issue,
            limit=limit,
            offset=offset,
        )

    def get_issue(self, issue_id: int) -> CVIssue | None:
        """Get a single issue by ID."""
        cur = self.conn.execute(f"SELECT {_ISSUE_COLS} FROM cv_issue WHERE id = ?", [issue_id])
        row = cur.fetchone()
        if not row:
            return None
        issue = _row_to_issue(row)
        # Attach volume info
        vol = self.get_volume(row["volume_id"])
        if vol:
            issue["volume"] = vol
        return issue

    def search_issues(
        self,
        *,
        volume_ids: list[int] | None = None,
        issue_number: str | None = None,
        store_date_start: str | None = None,
        store_date_end: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[CVIssue], int]:
        """Filter issues by volume(s), number, and/or store_date range.

        Returns (results, total_matching_count).
        Returns ([], 0) immediately when no filter is provided — refuses full-table scan.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if volume_ids:
            placeholders = ",".join("?" * len(volume_ids))
            clauses.append(f"volume_id IN ({placeholders})")
            params.extend(volume_ids)
        if issue_number is not None:
            clauses.append("issue_number = ?")
            params.append(issue_number)
        if store_date_start is not None:
            clauses.append("store_date >= ?")
            params.append(store_date_start)
        if store_date_end is not None:
            clauses.append("store_date <= ?")
            params.append(store_date_end)

        # Safety guard: refuse full-table scan
        if not clauses:
            return [], 0

        where = " AND ".join(clauses)

        total_cur = self.conn.execute(
            f"SELECT COUNT(*) FROM cv_issue WHERE {where}",
            params,
        )
        total: int = total_cur.fetchone()[0]

        cur = self.conn.execute(
            f"SELECT {_ISSUE_COLS} FROM cv_issue WHERE {where} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return [_row_to_issue(r) for r in cur.fetchall()], total

    # -- publishers ----------------------------------------------------------

    def search_publishers_by_name(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[CVPublisher], int]:
        """Search publishers by name using FTS5 with LIKE fallback."""
        return self._search_entity_by_name(  # type: ignore[return-value]
            query,
            table="cv_publisher",
            cols=_PUBLISHER_COLS,
            fts_table="publisher_fts",
            row_fn=_row_to_publisher,
            limit=limit,
            offset=offset,
        )

    def get_publisher(self, publisher_id: int) -> CVPublisher | None:
        """Get a single publisher by ID."""
        cur = self.conn.execute(
            f"SELECT {_PUBLISHER_COLS} FROM cv_publisher WHERE id = ?", [publisher_id]
        )
        row = cur.fetchone()
        return _row_to_publisher(row) if row else None

    def list_publishers(
        self,
        *,
        name: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[CVPublisher], int]:
        """List publishers with optional name filter and pagination.

        Returns (results, total_count).
        """
        if name:
            safe = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            count_cur = self.conn.execute(
                "SELECT COUNT(*) FROM cv_publisher WHERE name LIKE ? ESCAPE '\\'",
                [f"%{safe}%"],
            )
            total: int = count_cur.fetchone()[0]
            cur = self.conn.execute(
                f"SELECT {_PUBLISHER_COLS} FROM cv_publisher"
                " WHERE name LIKE ? ESCAPE '\\' LIMIT ? OFFSET ?",
                [f"%{safe}%", limit, offset],
            )
        else:
            count_cur = self.conn.execute("SELECT COUNT(*) FROM cv_publisher")
            total = count_cur.fetchone()[0]
            cur = self.conn.execute(
                f"SELECT {_PUBLISHER_COLS} FROM cv_publisher LIMIT ? OFFSET ?",
                [limit, offset],
            )
        rows = cur.fetchall()
        return [_row_to_publisher(r) for r in rows], total

    # -- response cache ------------------------------------------------------

    def cache_get_single(self, resource_type: str, resource_id: int) -> dict[str, Any] | None:
        """Return cached single-resource data, or None on miss."""
        cur = self.conn.execute(
            "SELECT data_json FROM cv_response_cache WHERE resource_type=? AND resource_id=?",
            [resource_type, resource_id],
        )
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def cache_put_single(self, resource_type: str, resource_id: int, data: dict[str, Any]) -> None:
        """Store a single resource result in the generic response cache."""
        self.conn.execute(
            """INSERT OR REPLACE INTO cv_response_cache
                   (resource_type, resource_id, data_json, cached_at)
               VALUES (?, ?, ?, datetime('now'))""",
            [resource_type, resource_id, json.dumps(data)],
        )
        self.conn.commit()

    def cache_get_list(self, resource_type: str, params_hash: str) -> list[dict[str, Any]] | None:
        """Return cached list of results for a resource type + params hash, or None on miss."""
        cur = self.conn.execute(
            "SELECT data_json FROM cv_response_cache WHERE resource_type=? AND params_hash=?",
            [resource_type, params_hash],
        )
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def cache_put_list(
        self, resource_type: str, params_hash: str, results: list[dict[str, Any]]
    ) -> None:
        """Store a list of results in the generic response cache."""
        self.conn.execute(
            """INSERT OR REPLACE INTO cv_response_cache
                   (resource_type, params_hash, data_json, cached_at)
               VALUES (?, ?, ?, datetime('now'))""",
            [resource_type, params_hash, json.dumps(results)],
        )
        self.conn.commit()

    def get_search_cache_missing_volume_ids(self, limit: int = 200) -> list[int]:
        """Return volume IDs referenced in search caches that are absent from cv_volume.

        Scans all search cache entries, collects every ``id`` field from the
        stored results list, then returns IDs not yet present in cv_volume.
        Used by the backfill scheduler to ensure volumes found via search are
        eventually fully indexed.
        """
        cur = self.conn.execute(
            "SELECT data_json FROM cv_response_cache WHERE resource_type='search'"
        )
        seen: set[int] = set()
        for (data_json,) in cur:
            try:
                payload = json.loads(data_json)
                for item in payload.get("results") or []:
                    if item.get("resource_type") == "volume":
                        vid = item.get("id")
                        if vid:
                            seen.add(int(vid))
            except Exception:
                continue

        if not seen:
            return []

        # Filter to IDs not yet in cv_volume
        placeholders = ",".join("?" * len(seen))
        cur2 = self.conn.execute(
            f"SELECT id FROM cv_volume WHERE id IN ({placeholders})",
            list(seen),
        )
        present = {row[0] for row in cur2}
        missing = [vid for vid in seen if vid not in present]
        missing.sort(reverse=True)  # newest IDs first
        return missing[:limit]

    def cache_get_search(self, params_hash: str) -> dict[str, Any] | None:
        """Return cached search data as {results: [...], total: int, query: str}, or None on miss."""
        cur = self.conn.execute(
            "SELECT data_json FROM cv_response_cache WHERE resource_type='search' AND params_hash=?",
            [params_hash],
        )
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def cache_find_superset_search(
        self, normalized_query: str, resources: str
    ) -> dict[str, Any] | None:
        """Return filtered results from a broader cached search that is a superset of this query.

        Uses a two-pass approach: first queries only the indexed ``query`` column
        to find candidates without loading any ``data_json``, then loads the JSON
        payload only for matching rows.  This avoids deserializing every stored
        search result on every cache miss.

        A candidate is eligible when:
        - Its stored query is a non-empty proper substring of *normalized_query*
          (meaning the candidate is a broader search).
        - Its cache is *complete*: ``len(results) >= total`` — all upstream pages
          were fetched.

        Example: "batman" cache complete → serves "absolute batman" by filtering
        results whose name contains "absolute batman".
        """
        # Pass 1: find candidates via indexed query column — no data_json loaded.
        # Exclude stored queries containing LIKE metacharacters (% _) to prevent
        # them from acting as wildcards in the pattern.
        cur = self.conn.execute(
            """SELECT params_hash, query FROM cv_response_cache
               WHERE resource_type = 'search'
                 AND query IS NOT NULL
                 AND query != ?
                 AND query NOT LIKE '%\%%' ESCAPE '\'
                 AND query NOT LIKE '%\_%' ESCAPE '\'
                 AND ? LIKE '%' || query || '%'""",
            [normalized_query, normalized_query],
        )
        candidates = cur.fetchall()
        if not candidates:
            return None

        # Pass 2: load data_json only for matching candidates.
        for params_hash, _cached_query in candidates:
            row = self.conn.execute(
                "SELECT data_json FROM cv_response_cache"
                " WHERE resource_type = 'search' AND params_hash = ?",
                [params_hash],
            ).fetchone()
            if not row:
                continue
            try:
                payload = json.loads(row[0])
                if payload.get("resources", "") != resources:
                    continue
                results = payload.get("results") or []
                total = payload.get("total", 0)
                if len(results) < total:
                    continue
                filtered = [
                    r for r in results
                    if normalized_query in (r.get("name") or "").lower()
                ]
                return {"results": filtered, "total": len(filtered), "from_superset": True}
            except Exception:
                continue
        return None

    def cache_put_search(
        self,
        params_hash: str,
        results: list[dict[str, Any]],
        total: int,
        *,
        query: str = "",
        resources: str = "",
    ) -> None:
        """Store a search result list and total count. TTL is controlled by search_cache_ttl_days.

        *query* should be the normalized query string; it is stored in the payload
        so that :meth:`cache_find_superset_search` can locate broader caches.
        """
        payload: dict[str, Any] = {"results": results, "total": total}
        if query:
            payload["query"] = query
        if resources:
            payload["resources"] = resources
        self.conn.execute(
            """INSERT OR REPLACE INTO cv_response_cache
                   (resource_type, params_hash, data_json, cached_at, query)
               VALUES ('search', ?, ?, datetime('now'), ?)""",
            [params_hash, json.dumps(payload), query or None],
        )
        self.conn.commit()

    # -- upserts (write-through) -------------------------------------------------

    def upsert_publisher(self, data: dict[str, Any], commit: bool = True) -> None:
        """Insert or replace a publisher from an upstream CV API dict."""
        if not data.get("id"):
            return

        image = data.get("image") or {}
        image_url = (image.get("original_url") or image.get("medium_url")) if image else None

        self.conn.execute(
            """INSERT OR REPLACE INTO cv_publisher
                   (id, name, aliases, deck, description,
                    location_address, location_city, location_state,
                    image_url, site_detail_url, api_detail_url,
                    characters, teams, volumes, story_arcs,
                    date_added, date_last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                data["id"],
                data.get("name"),
                data.get("aliases"),
                data.get("deck"),
                data.get("description"),
                data.get("location_address"),
                data.get("location_city"),
                data.get("location_state"),
                image_url,
                data.get("site_detail_url"),
                data.get("api_detail_url"),
                _to_json(data.get("characters")),
                _to_json(data.get("teams")),
                _to_json(data.get("volumes")),
                _to_json(data.get("story_arcs")),
                data.get("date_added"),
                data.get("date_last_updated"),
            ],
        )
        if commit:
            self.conn.commit()

    def mark_backfill_missing(self, table: str, entity_id: int) -> None:
        """Mark an entity as confirmed absent from the CV API.

        Rows with backfill_missing = 1 are excluded from future backfill
        SELECT queries so the hourly budget is not wasted re-fetching IDs
        that CV has already returned Object Not Found for.
        Does NOT commit — caller is responsible for committing the batch.
        """
        self.conn.execute(
            f"UPDATE {table} SET backfill_missing = 1 WHERE id = ?",  # noqa: S608
            [entity_id],
        )

    # CV uses IDs >= this value as sentinel placeholders (e.g. 999999999 means
    # "no issue").  We discard them so scrapers never try to fetch them.
    _CV_SENTINEL_ID_THRESHOLD = 900_000_000

    def upsert_volume(self, data: dict[str, Any], commit: bool = True) -> None:
        """Insert or replace a volume from an upstream CV API results dict.

        Also upserts the publisher if present. FTS index is NOT updated here —
        callers must trigger a rebuild or use triggers to keep it in sync.
        """
        if not data.get("id"):
            return
        if self._cutoff_year:
            last_issue = data.get("last_issue") or {}
            if isinstance(last_issue, str):
                try:
                    last_issue = json.loads(last_issue)
                except (json.JSONDecodeError, TypeError):
                    last_issue = {}
            if isinstance(last_issue, dict) and self._is_before_cutoff(last_issue.get("cover_date")):
                return
        pub = data.get("publisher") or {}
        if pub.get("id"):
            self.upsert_publisher(pub, commit=False)

        aliases = data.get("aliases")
        if isinstance(aliases, list):
            aliases = "\n".join(aliases)

        image = data.get("image") or {}
        image_url = (image.get("original_url") or image.get("medium_url")) if image else None

        def _sentinel(obj: Any) -> bool:
            if not isinstance(obj, dict):
                return False
            try:
                return int(obj.get("id", 0)) >= self._CV_SENTINEL_ID_THRESHOLD
            except (TypeError, ValueError):
                return False

        self.conn.execute(
            """INSERT OR REPLACE INTO cv_volume
                   (id, name, aliases, start_year, publisher_id, count_of_issues,
                    description, image_url, site_detail_url, deck,
                    characters, concepts, people, objects,
                    first_issue, last_issue,
                    api_detail_url, date_added, date_last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                data.get("id"),
                data.get("name"),
                aliases,
                data.get("start_year"),
                pub.get("id"),
                data.get("count_of_issues"),
                data.get("description"),
                image_url,
                data.get("site_detail_url"),
                data.get("deck"),
                _to_json(data.get("characters")),
                _to_json(data.get("concepts")),
                _to_json(data.get("people")),
                _to_json(data.get("objects")),
                _to_json(None if _sentinel(data.get("first_issue")) else data.get("first_issue")),
                _to_json(None if _sentinel(data.get("last_issue")) else data.get("last_issue")),
                data.get("api_detail_url"),
                data.get("date_added"),
                data.get("date_last_updated"),
            ],
        )
        if commit:
            self.conn.commit()

    def upsert_issue(self, data: dict[str, Any], commit: bool = True) -> None:
        """Insert or replace an issue from an upstream CV API results dict."""
        if not data.get("id"):
            return
        if self._cutoff_year:
            date_str = data.get("store_date") or data.get("cover_date")
            if self._is_before_cutoff(date_str):
                return
        volume = data.get("volume") or {}
        image = data.get("image") or {}
        image_url = (image.get("original_url") or image.get("medium_url")) if image else None

        aliases = data.get("aliases")
        if isinstance(aliases, list):
            aliases = "\n".join(aliases)

        self.conn.execute(
            """INSERT OR REPLACE INTO cv_issue
                   (id, volume_id, name, issue_number, cover_date, store_date,
                    description, image_url, site_detail_url,
                    character_credits, person_credits, team_credits,
                    location_credits, story_arc_credits, associated_images,
                    aliases, deck, concept_credits, object_credits,
                    character_died_in, team_disbanded_in,
                    first_appearance_characters, first_appearance_concepts,
                    first_appearance_locations, first_appearance_objects,
                    first_appearance_storyarcs, first_appearance_teams,
                    has_staff_review, api_detail_url, date_added, date_last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                data.get("id"),
                volume.get("id"),
                data.get("name"),
                data.get("issue_number"),
                data.get("cover_date"),
                data.get("store_date"),
                data.get("description"),
                image_url,
                data.get("site_detail_url"),
                _to_json(data.get("character_credits")),
                _to_json(data.get("person_credits")),
                _to_json(data.get("team_credits")),
                _to_json(data.get("location_credits")),
                _to_json(data.get("story_arc_credits")),
                _to_json(data.get("associated_images")),
                aliases,
                data.get("deck"),
                _to_json(data.get("concept_credits")),
                _to_json(data.get("object_credits")),
                _to_json(data.get("character_died_in")),
                _to_json(data.get("team_disbanded_in")),
                _to_json(data.get("first_appearance_characters")),
                _to_json(data.get("first_appearance_concepts")),
                _to_json(data.get("first_appearance_locations")),
                _to_json(data.get("first_appearance_objects")),
                _to_json(data.get("first_appearance_storyarcs")),
                _to_json(data.get("first_appearance_teams")),
                int(data.get("has_staff_review") or 0),
                data.get("api_detail_url"),
                data.get("date_added"),
                data.get("date_last_updated"),
            ],
        )
        if commit:
            self.conn.commit()

    # -- repair helpers ------------------------------------------------------

    def get_all_volume_ids(self) -> list[int]:
        """Return all volume IDs currently stored in the local cache."""
        cur = self.conn.execute("SELECT id FROM cv_volume ORDER BY id")
        return [row[0] for row in cur.fetchall()]

    def get_calendar_volume_ids(self) -> list[int]:
        """Return IDs of volumes that have at least one issue with a store_date.

        This is the subset of volumes referenced by new-release calendar
        queries — typically far smaller than the full cv_volume table.
        """
        cur = self.conn.execute(
            """
            SELECT DISTINCT i.volume_id
            FROM cv_issue i
            JOIN cv_volume v ON v.id = i.volume_id
            WHERE i.store_date IS NOT NULL
            ORDER BY i.volume_id
            """
        )
        return [row[0] for row in cur.fetchall()]

    def update_volume_publisher(self, volume_id: int, publisher_id: int) -> None:
        """Overwrite the publisher_id for a single volume (used by repair job)."""
        self.conn.execute(
            "UPDATE cv_volume SET publisher_id = ? WHERE id = ?",
            [publisher_id, volume_id],
        )
        self.conn.commit()

    # -- stats ---------------------------------------------------------------

    def count_response_cache_before(self, cutoff: str) -> int:
        """Return number of cv_response_cache rows with cached_at older than cutoff."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM cv_response_cache WHERE cached_at < ?",
            [cutoff],
        )
        return cur.fetchone()[0]

    def get_counts(self) -> dict[str, int]:
        """Return row counts for health/stats endpoint."""
        counts: dict[str, int] = {}
        for table in ("cv_volume", "cv_issue", "cv_publisher"):
            cur = self.conn.execute(f"SELECT COUNT(*) FROM {table}")
            row = cur.fetchone()
            counts[table.removeprefix("cv_")] = row[0] if row else 0
        return counts
