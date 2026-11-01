"""Settings edited on the dashboard, stored in jobs.db on top of the environment.

Only the settings in EDITABLE can be overridden: ones that are read whenever
they are used, so a change applies from the next request or job run.  Secrets,
paths and anything read once at start-up stay environment-only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cvproxy.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Editable:
    label: str
    help: str
    minimum: int = 0


EDITABLE: dict[str, Editable] = {
    "backfill_min_year": Editable(
        "Backfill floor year",
        "Backfills and cover hashing only go after issues dated this year or later, plus "
        "undated ones. 0 = no floor."),
    "cover_hash_per_hour": Editable(
        "Cover hashes per run",
        "Cover images hashed per hourly run, from the image CDN (no API quota). "
        "0 = cover hashing off."),
    "backup_keep": Editable(
        "Backups kept",
        "Database snapshots kept in the backup folder. 0 = backups off."),
    "evict_older_than_years": Editable(
        "Evict issues older than (years)",
        "Issues older than this become eligible for eviction. 0 = issue eviction off."),
    "evict_unaccessed_days": Editable(
        "Evict after unaccessed (days)",
        "An eligible issue is evicted once no client asked for it for this long.", 1),
    "response_cache_ttl_days": Editable(
        "Response cache TTL (days)",
        "Cached API responses older than this are deleted. 0 = keep forever."),
    "search_cache_ttl_days": Editable(
        "Search cache TTL (days)",
        "Cached search results older than this are deleted. 0 = keep forever."),
    "search_max_pages": Editable(
        "Search pages per query",
        "Upstream result pages (100 results each) fetched per search query.", 1),
}


class InvalidOverride(ValueError):
    """An override names a setting that can't be edited, or has a bad value."""


def _check(name: str, value: Any) -> None:
    field = EDITABLE.get(name)
    if field is None:
        raise InvalidOverride(f"{name.upper()} can't be changed on the dashboard")
    if type(value) is not int:
        raise InvalidOverride(f"{name.upper()} must be a whole number")
    if value < field.minimum:
        raise InvalidOverride(f"{name.upper()} must be at least {field.minimum}")


def apply_overrides(base: Settings, overrides: dict[str, Any]) -> Settings:
    """*base* with *overrides* applied; raises InvalidOverride on any bad entry."""
    for name, value in overrides.items():
        _check(name, value)
    return Settings.model_validate({**base.model_dump(), **overrides})


def usable_overrides(stored: dict[str, Any]) -> dict[str, Any]:
    """Stored overrides that are still valid; the rest are logged and dropped."""
    usable = {}
    for name, value in stored.items():
        try:
            _check(name, value)
        except InvalidOverride as exc:
            logger.warning("Ignoring stored setting override: %s", exc)
        else:
            usable[name] = value
    return usable


def describe_settings(
    base: Settings, current: Settings, overrides: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every editable setting with its value, default and where the value comes from."""
    out = []
    for name, field in EDITABLE.items():
        if name in overrides:
            source = "override"
        elif name in base.model_fields_set:
            source = "env"
        else:
            source = "default"
        out.append({
            "name": name,
            "env": name.upper(),
            "label": field.label,
            "help": field.help,
            "min": field.minimum,
            "value": getattr(current, name),
            "base": getattr(base, name),
            "default": Settings.model_fields[name].default,
            "source": source,
        })
    return out


_DDL = """
CREATE TABLE IF NOT EXISTS setting_overrides (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class OverrideStore:
    """The setting_overrides table in jobs.db, on a connection of its own."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_DDL)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def load(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT name, value FROM setting_overrides").fetchall()
        return {name: json.loads(value) for name, value in rows}

    def save(self, overrides: dict[str, Any]) -> None:
        """Replace every stored override with *overrides*."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM setting_overrides")
            self._conn.executemany(
                "INSERT INTO setting_overrides (name, value) VALUES (?, ?)",
                [(name, json.dumps(value)) for name, value in overrides.items()],
            )
