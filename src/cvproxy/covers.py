"""Cover image hashes for comic_covers, the table ComicManager matches covers against.

ComicManager hashes a comic's first page with imagehash (64-bit pHash and
average hash) and compares it with ``comic_covers.ct_phash`` / ``ct_ahash``
(decimal strings) by Hamming distance.  The original localcv.db shipped
those hashes; this module computes them for issues added since, using the
same library.  Images come from ComicVine's image CDN, not the API, so this
doesn't touch the API quota.  The ``scale_medium`` size gives the same hashes
as the full-size original at a fraction of the download.
"""

from __future__ import annotations

import asyncio
import io
import logging

import httpx
import imagehash
from PIL import Image

from cvproxy.db import Database

logger = logging.getLogger(__name__)

#: Image size downloaded for hashing (hashes match the original's).
_HASH_SIZE = "scale_medium"
#: ComicVine's "no cover yet" image; its hash would match any blank-looking cover.
_PLACEHOLDER_SUFFIX = "/6373148-blank.png"


def sized_url(url: str, size: str) -> str:
    """ComicVine image URL for another size (``.../original/...`` → ``.../<size>/...``)."""
    return url.replace("/original/", f"/{size}/", 1)


def cover_hashes(data: bytes) -> tuple[str, str]:
    """(pHash, average hash) of an image, as the decimal strings comic_covers stores."""
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        phash = imagehash.phash(img)
        ahash = imagehash.average_hash(img)
    return str(int(str(phash), 16)), str(int(str(ahash), 16))


async def hash_covers(
    db: Database,
    *,
    limit: int,
    delay: float = 1.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, int]:
    """Hash the covers of issues that have none in comic_covers, newest first.

    Downloads at most about *limit* images, *delay* seconds apart.  A missing
    or unreadable image is stored with NULL hashes (ComicManager skips those)
    so it isn't retried every run; a 429, 5xx or network error stops the run
    and the issue is tried again next time.
    """
    hashed = failed = issues = 0
    async with httpx.AsyncClient(
        transport=transport,
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "CVProxy/0.1"},
    ) as http:
        for cvid, urls in await asyncio.to_thread(db.cover_hash_candidates, limit):
            if hashed + failed >= limit:
                break
            rows: list[tuple[int, str | None, str | None, str]] = []
            for url in urls:
                if url.endswith(_PLACEHOLDER_SUFFIX):
                    rows.append((cvid, None, None, url))
                    failed += 1
                    continue
                try:
                    resp = await http.get(sized_url(url, _HASH_SIZE))
                except httpx.RequestError as exc:
                    logger.warning("Cover hash backfill: %s (%s); stopping this run", exc, url)
                    return {"hashed": hashed, "failed": failed, "issues": issues}
                if resp.status_code == 429 or resp.status_code >= 500:
                    logger.warning(
                        "Cover hash backfill: CDN answered %d; stopping this run",
                        resp.status_code,
                    )
                    return {"hashed": hashed, "failed": failed, "issues": issues}
                try:
                    if resp.status_code != 200:
                        raise ValueError(f"HTTP {resp.status_code}")
                    phash, ahash = await asyncio.to_thread(cover_hashes, resp.content)
                except (OSError, ValueError) as exc:
                    logger.info("Cover hash backfill: no hash for %s: %s", url, exc)
                    rows.append((cvid, None, None, url))
                    failed += 1
                else:
                    rows.append((cvid, phash, ahash, url))
                    hashed += 1
                if delay:
                    await asyncio.sleep(delay)
            db.insert_cover_hashes(rows)
            issues += 1
    if hashed or failed:
        logger.info(
            "Cover hash backfill: %d hashed, %d without image, %d issues", hashed, failed, issues
        )
    return {"hashed": hashed, "failed": failed, "issues": issues}
