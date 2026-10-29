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
import time
from urllib.parse import urlsplit

import httpx
import imagehash
from PIL import Image

from cvproxy.db import Database
from cvproxy.jobs import report_progress

logger = logging.getLogger(__name__)

#: Image size downloaded for hashing (hashes match the original's).
_HASH_SIZE = "scale_medium"
#: ComicVine's "no cover yet" image; its hash would match any blank-looking cover.
_PLACEHOLDER_SUFFIX = "/6373148-blank.png"
#: Images are only fetched from ComicVine's CDN, including after redirects.
_ALLOWED_HOST = "comicvine.gamespot.com"
#: A scale_medium cover is a few hundred KB; anything far larger isn't a cover.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

_Row = tuple[int, str | None, str | None, str]


class _DisallowedURL(Exception):
    """A URL (or redirect target) outside ComicVine's image CDN."""


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != _ALLOWED_HOST:
        raise _DisallowedURL(url)


async def _check_request(request: httpx.Request) -> None:
    _check_url(str(request.url))  # also runs for every redirect hop


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


async def _download(http: httpx.AsyncClient, url: str) -> bytes:
    """Fetch an image body, refusing anything over _MAX_IMAGE_BYTES.

    Raises httpx.HTTPStatusError for non-200 answers.
    """
    async with http.stream("GET", url) as resp:
        resp.raise_for_status()
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_IMAGE_BYTES:
                raise ValueError(f"larger than {_MAX_IMAGE_BYTES} bytes")
    return bytes(body)


async def hash_covers(
    db: Database,
    *,
    limit: int,
    delay: float = 1.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, int | float | str]:
    """Hash the covers of issues that have none in comic_covers, newest first.

    Downloads at most *limit* images, *delay* seconds apart; an issue whose
    covers would exceed the limit waits for the next run, so issues are never
    left half-hashed.  A missing, refused or unreadable image is stored with
    NULL hashes (ComicManager skips those) so it isn't retried every run; a
    429, 5xx or network error stops the run (stop_reason "cdn_error") and the
    issue is retried next time.  ``scan_s`` is how long finding candidates took.
    """
    hashed = failed = issues = 0
    t0 = time.monotonic()
    candidates = await asyncio.to_thread(db.cover_hash_candidates, limit)
    scan_s = round(time.monotonic() - t0, 1)
    if not candidates:
        logger.info("Cover hash backfill: no issues without cover hashes (scan %.1fs)", scan_s)
        return {"hashed": 0, "failed": 0, "issues": 0, "scan_s": scan_s,
                "stop_reason": "nothing_to_do"}

    def _result(stop: str | None = None) -> dict[str, int | float | str]:
        out: dict[str, int | float | str] = {
            "hashed": hashed, "failed": failed, "issues": issues, "scan_s": scan_s,
        }
        if stop:
            out["stop_reason"] = stop
        return out

    async with httpx.AsyncClient(
        transport=transport,
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "CVProxy/0.1"},
        event_hooks={"request": [_check_request]},
    ) as http:
        for cvid, urls in candidates:
            done = hashed + failed
            if done and done + len(urls) > limit:
                break
            rows: list[_Row] = []
            for url in urls:
                if url.endswith(_PLACEHOLDER_SUFFIX):
                    rows.append((cvid, None, None, url))
                    failed += 1
                    continue
                try:
                    _check_url(url)
                    data = await _download(http, sized_url(url, _HASH_SIZE))
                    phash, ahash = await asyncio.to_thread(cover_hashes, data)
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if code == 429 or code >= 500:
                        logger.warning(
                            "Cover hash backfill: CDN answered %d; stopping this run", code
                        )
                        return _result("cdn_error")
                    logger.info("Cover hash backfill: no image at %s (HTTP %d)", url, code)
                    rows.append((cvid, None, None, url))
                    failed += 1
                except httpx.RequestError as exc:
                    logger.warning("Cover hash backfill: %s (%s); stopping this run", exc, url)
                    return _result("cdn_error")
                except Exception as exc:  # noqa: BLE001 - bad URL, oversized or undecodable image
                    logger.info("Cover hash backfill: no hash for %s: %r", url, exc)
                    rows.append((cvid, None, None, url))
                    failed += 1
                else:
                    rows.append((cvid, phash, ahash, url))
                    hashed += 1
                if delay:
                    await asyncio.sleep(delay)
            db.insert_cover_hashes(rows)
            issues += 1
            report_progress(hashed + failed, limit, f"{issues} issues")
    logger.info(
        "Cover hash backfill: %d hashed, %d without image, %d issues (scan %.1fs)",
        hashed, failed, issues, scan_s,
    )
    return _result()
