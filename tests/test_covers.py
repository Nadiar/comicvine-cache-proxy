"""Tests for the cover hash backfill (comic_covers, read by ComicManager)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import imagehash
import pytest
from PIL import Image

from cvproxy.covers import cover_hashes, hash_covers, sized_url
from cvproxy.db import Database

ORIGINAL = "https://comicvine.gamespot.com/a/uploads/original/1/11/{}.jpg"


@pytest.fixture(autouse=True)
def _no_fixture_covers(tmp_db: Database) -> None:
    """The shared fixture issues have example.com images; keep them out of these runs."""
    tmp_db.conn.execute("UPDATE cv_issue SET image_url = NULL")
    tmp_db.conn.commit()


def _jpeg(shade: int) -> bytes:
    img = Image.new("RGB", (64, 96))
    for x in range(64):
        for y in range(96):
            img.putpixel((x, y), ((x * shade) % 256, (y * 3) % 256, (x + y) % 256))
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


def _cdn(images: dict[str, bytes | int]) -> tuple[httpx.MockTransport, list[str]]:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        body = images.get(url, 404)
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler), requested


def _issue(db: Database, iid: int, image: str | None, variants: list[str] | None = None) -> None:
    data = {"id": iid, "volume": {"id": 160294}, "issue_number": str(iid)}
    if image:
        data["image"] = {"original_url": image}
    if variants is not None:
        data["associated_images"] = [{"original_url": url} for url in variants]
    db.upsert_issue(data)


def _covers(db: Database) -> list[tuple]:
    return [tuple(row) for row in db.conn.execute(
        "SELECT cvid, ct_phash, ct_ahash, cv_url FROM comic_covers ORDER BY cvid, cv_url"
    )]


def test_cover_hashes_match_imagehash_as_decimal_strings() -> None:
    data = _jpeg(5)
    img = Image.open(io.BytesIO(data))

    phash, ahash = cover_hashes(data)

    assert phash == str(int(str(imagehash.phash(img)), 16))
    assert ahash == str(int(str(imagehash.average_hash(img)), 16))


def test_sized_url_swaps_original_for_requested_size() -> None:
    assert sized_url(ORIGINAL.format("a"), "scale_medium") == (
        "https://comicvine.gamespot.com/a/uploads/scale_medium/1/11/a.jpg"
    )
    assert sized_url("https://example.com/x.jpg", "scale_medium") == "https://example.com/x.jpg"


async def test_hash_covers_stores_main_and_variant_covers(tmp_db: Database) -> None:
    _issue(tmp_db, 9001, ORIGINAL.format("main"), [ORIGINAL.format("variant")])
    transport, requested = _cdn({
        sized_url(ORIGINAL.format("main"), "scale_medium"): _jpeg(3),
        sized_url(ORIGINAL.format("variant"), "scale_medium"): _jpeg(7),
    })

    result = await hash_covers(tmp_db, limit=10, delay=0, transport=transport)

    assert result == {"hashed": 2, "failed": 0, "issues": 1}
    rows = [r for r in _covers(tmp_db) if r[0] == 9001]
    assert [r[3] for r in rows] == [ORIGINAL.format("main"), ORIGINAL.format("variant")]
    assert all(r[1] and r[2] for r in rows)
    assert all("/scale_medium/" in url for url in requested)


async def test_hash_covers_records_missing_image_so_it_is_not_retried(tmp_db: Database) -> None:
    _issue(tmp_db, 9002, ORIGINAL.format("gone"))
    transport, requested = _cdn({})

    first = await hash_covers(tmp_db, limit=10, delay=0, transport=transport)
    second = await hash_covers(tmp_db, limit=10, delay=0, transport=transport)

    assert first["failed"] == 1
    assert [r for r in _covers(tmp_db) if r[0] == 9002] == [
        (9002, None, None, ORIGINAL.format("gone"))
    ]
    assert second["issues"] == 0
    assert len([u for u in requested if "gone" in u]) == 1


async def test_hash_covers_stops_and_retries_later_when_cdn_is_unhappy(tmp_db: Database) -> None:
    _issue(tmp_db, 9003, ORIGINAL.format("busy"))
    _issue(tmp_db, 9004, ORIGINAL.format("never"))
    transport, requested = _cdn({
        sized_url(ORIGINAL.format("never"), "scale_medium"): 503,
        sized_url(ORIGINAL.format("busy"), "scale_medium"): 503,
    })

    result = await hash_covers(tmp_db, limit=10, delay=0, transport=transport)

    assert result["hashed"] == 0
    assert len(requested) == 1  # stopped at the first 5xx
    assert [r for r in _covers(tmp_db) if r[0] in (9003, 9004)] == []


async def test_hash_covers_newest_issues_first_and_respects_limit(tmp_db: Database) -> None:
    for iid in (9010, 9012, 9011):
        _issue(tmp_db, iid, ORIGINAL.format(iid))
    transport, _ = _cdn({
        sized_url(ORIGINAL.format(iid), "scale_medium"): _jpeg(iid % 7 + 1)
        for iid in (9010, 9011, 9012)
    })

    await hash_covers(tmp_db, limit=2, delay=0, transport=transport)

    assert sorted({r[0] for r in _covers(tmp_db)} & {9010, 9011, 9012}) == [9011, 9012]


def test_cover_hash_candidates_skip_issues_with_covers_or_no_image(tmp_db: Database) -> None:
    _issue(tmp_db, 9020, ORIGINAL.format("a"), [ORIGINAL.format("a"), ORIGINAL.format("b")])
    _issue(tmp_db, 9021, None)
    _issue(tmp_db, 9022, ORIGINAL.format("c"))
    tmp_db.insert_cover_hashes([(9022, "1", "2", ORIGINAL.format("c"))])

    candidates = dict(tmp_db.cover_hash_candidates(limit=50))

    assert candidates[9020] == [ORIGINAL.format("a"), ORIGINAL.format("b")]  # deduplicated
    assert 9021 not in candidates
    assert 9022 not in candidates


def test_comic_covers_table_exists_on_fresh_database(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db")
    db.connect()
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(comic_covers)")]
    db.close()
    assert cols == ["id", "cvid", "ct_phash", "ct_ahash", "cv_url"]


def test_associated_images_json_is_read(tmp_db: Database) -> None:
    """Variants come from the stored associated_images JSON of detailed issues."""
    _issue(tmp_db, 9030, ORIGINAL.format("m"), [ORIGINAL.format("v1")])
    stored = tmp_db.conn.execute(
        "SELECT associated_images FROM cv_issue WHERE id = 9030"
    ).fetchone()[0]
    assert json.loads(stored)[0]["original_url"] == ORIGINAL.format("v1")


async def test_hash_covers_skips_comicvine_placeholder_image(tmp_db: Database) -> None:
    """Hashing the "no cover" placeholder would match any blank-looking local cover."""
    placeholder = "https://comicvine.gamespot.com/a/uploads/original/11122/111222211/6373148-blank.png"
    real_blank_title = ORIGINAL.format("point-blank")
    _issue(tmp_db, 9040, placeholder)
    _issue(tmp_db, 9041, real_blank_title)
    transport, requested = _cdn({sized_url(real_blank_title, "scale_medium"): _jpeg(2)})

    await hash_covers(tmp_db, limit=10, delay=0, transport=transport)

    rows = {r[0]: r for r in _covers(tmp_db)}
    assert rows[9040] == (9040, None, None, placeholder)
    assert rows[9041][1] is not None
    assert not any("6373148-blank" in url for url in requested)


# ---------------------------------------------------------------------------
# Hardening: download cap, allowed hosts, hostile images
# ---------------------------------------------------------------------------


async def test_hash_covers_does_not_start_an_issue_that_would_exceed_the_cap(
    tmp_db: Database,
) -> None:
    _issue(tmp_db, 9051, ORIGINAL.format("a"))
    _issue(tmp_db, 9050, ORIGINAL.format("b"), [ORIGINAL.format("c"), ORIGINAL.format("d")])
    transport, requested = _cdn({
        sized_url(ORIGINAL.format(x), "scale_medium"): _jpeg(3) for x in "abcd"
    })

    result = await hash_covers(tmp_db, limit=2, delay=0, transport=transport)

    assert result["issues"] == 1
    assert len(requested) == 1  # 9050's three covers would have overshot the cap of 2


async def test_hash_covers_only_fetches_comicvine_https(tmp_db: Database) -> None:
    internal = "http://169.254.169.254/latest/original/meta.jpg"
    _issue(tmp_db, 9060, internal)
    transport, requested = _cdn({})

    await hash_covers(tmp_db, limit=10, delay=0, transport=transport)

    assert requested == []
    assert [r for r in _covers(tmp_db) if r[0] == 9060] == [(9060, None, None, internal)]


async def test_hash_covers_does_not_follow_redirects_off_comicvine(tmp_db: Database) -> None:
    _issue(tmp_db, 9061, ORIGINAL.format("moved"))
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if "comicvine" in request.url.host:
            return httpx.Response(302, headers={"Location": "http://10.0.0.5/internal.jpg"})
        return httpx.Response(200, content=_jpeg(4))

    await hash_covers(tmp_db, limit=10, delay=0, transport=httpx.MockTransport(handler))

    assert not any("10.0.0.5" in url for url in requested)
    assert [r[1] for r in _covers(tmp_db) if r[0] == 9061] == [None]


async def test_hash_covers_records_oversized_image_instead_of_failing_the_run(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decompression bomb must not abort every hourly run on the same issue."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)  # our 64x96 test JPEG is "too big"
    _issue(tmp_db, 9071, ORIGINAL.format("bomb"))
    _issue(tmp_db, 9070, ORIGINAL.format("next"))
    transport, _ = _cdn({
        sized_url(ORIGINAL.format("bomb"), "scale_medium"): _jpeg(5),
        sized_url(ORIGINAL.format("next"), "scale_medium"): _jpeg(6),
    })

    result = await hash_covers(tmp_db, limit=10, delay=0, transport=transport)

    assert result["failed"] == 2 and result["issues"] == 2
    assert {r[0] for r in _covers(tmp_db)} >= {9070, 9071}


async def test_hash_covers_rejects_oversized_download(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cvproxy.covers as covers

    monkeypatch.setattr(covers, "_MAX_IMAGE_BYTES", 100)
    _issue(tmp_db, 9080, ORIGINAL.format("huge"))
    transport, _ = _cdn({sized_url(ORIGINAL.format("huge"), "scale_medium"): _jpeg(5)})

    result = await hash_covers(tmp_db, limit=10, delay=0, transport=transport)

    assert result["failed"] == 1
    assert [r[1] for r in _covers(tmp_db) if r[0] == 9080] == [None]
