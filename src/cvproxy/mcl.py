"""Keep a local copy of the community "missing comics list" (latest.mcl).

The file maps every ComicVine volume to its complete list of issue IDs and
numbers.  ComicManager reads it from /cv_db/latest.mcl as a fallback source
when comicvine.db is unavailable.  It is published in a Gitea repository
under a dated name (e.g. ``20261020_latest.mcl``), so the directory listing
is used to find the current file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SUFFIX = "_latest.mcl"


def _count_volume_rows(data: bytes) -> int:
    """Rows of the form ``volume_id;issue_ids;issue_numbers`` (header rows skipped)."""
    text = data.decode("utf-8-sig", errors="replace")
    return sum(
        1
        for line in text.splitlines()
        if line.count(";") >= 2 and line.split(";", 1)[0].strip().isdigit()
    )


def read_mcl(path: Path) -> dict[int, set[int]]:
    """Parse an MCL file into {volume_id: {issue_id, ...}} (header rows skipped)."""
    volumes: dict[int, set[int]] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for line in fh:
            parts = line.split(";")
            if len(parts) < 3 or not parts[0].strip().isdigit():
                continue
            volumes[int(parts[0])] = {
                int(x) for x in parts[1].split(",") if x.strip().isdigit()
            }
    return volumes


async def refresh_mcl(
    listing_url: str,
    dest: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Download the newest ``*_latest.mcl`` from *listing_url* into *dest*.

    The file is only replaced when its content changed, and only after it
    parses: ComicManager reloads it whenever the mtime changes.

    Raises ValueError when the listing has no MCL file or the download isn't one.
    """
    async with httpx.AsyncClient(
        transport=transport, timeout=60.0, follow_redirects=True
    ) as client:
        listing_resp = await client.get(listing_url)
        listing_resp.raise_for_status()
        candidates = [
            entry for entry in listing_resp.json()
            if entry.get("name", "").endswith(_SUFFIX) and entry.get("download_url")
        ]
        if not candidates:
            raise ValueError(f"No *{_SUFFIX} file listed at {listing_url}")
        newest = max(candidates, key=lambda entry: entry["name"])
        file_resp = await client.get(newest["download_url"])
        file_resp.raise_for_status()
        data = file_resp.content

    volumes = _count_volume_rows(data)
    if volumes == 0:
        raise ValueError(f"{newest['name']} has no volume rows; keeping the current file")
    if dest.exists() and dest.read_bytes() == data:
        return {"updated": False, "source": newest["name"], "volumes": volumes}

    partial = dest.with_name(dest.name + ".partial")
    partial.write_bytes(data)
    partial.replace(dest)
    logger.info("latest.mcl updated from %s (%d volumes)", newest["name"], volumes)
    return {"updated": True, "source": newest["name"], "volumes": volumes}
