"""Tests for the latest.mcl refresh (missing-comics list published for ComicManager)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cvproxy.mcl import refresh_mcl

LISTING_URL = "https://gitea.example/api/v1/repos/o/r/contents/Update%20Missing?ref=master"
MCL_BODY = b"Missing;2026-10-20\n766;92458,92459;2,4\n769;128,268;1,2\n"


def _transport(listing: list[dict], files: dict[str, bytes]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == LISTING_URL:
            return httpx.Response(200, content=json.dumps(listing).encode())
        if url in files:
            return httpx.Response(200, content=files[url])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _entry(name: str) -> dict:
    return {"name": name, "type": "file", "download_url": f"https://gitea.example/raw/{name}"}


async def test_refresh_mcl_downloads_newest_latest_file(tmp_path: Path) -> None:
    dest = tmp_path / "latest.mcl"
    listing = [_entry("update.sh"), _entry("20261019_latest.mcl"), _entry("20261020_latest.mcl")]
    transport = _transport(listing, {
        "https://gitea.example/raw/20261020_latest.mcl": MCL_BODY,
        "https://gitea.example/raw/20261019_latest.mcl": b"old",
    })

    result = await refresh_mcl(LISTING_URL, dest, transport=transport)

    assert result == {"updated": True, "source": "20261020_latest.mcl", "volumes": 2}
    assert dest.read_bytes() == MCL_BODY
    assert not (tmp_path / "latest.mcl.partial").exists()


async def test_refresh_mcl_leaves_unchanged_file_alone(tmp_path: Path) -> None:
    """Rewriting identical content would change the mtime and make ComicManager reload it."""
    dest = tmp_path / "latest.mcl"
    dest.write_bytes(MCL_BODY)
    mtime = dest.stat().st_mtime_ns
    transport = _transport(
        [_entry("20261020_latest.mcl")],
        {"https://gitea.example/raw/20261020_latest.mcl": MCL_BODY},
    )

    result = await refresh_mcl(LISTING_URL, dest, transport=transport)

    assert result["updated"] is False
    assert dest.stat().st_mtime_ns == mtime


async def test_refresh_mcl_rejects_file_without_volume_rows(tmp_path: Path) -> None:
    dest = tmp_path / "latest.mcl"
    dest.write_bytes(MCL_BODY)
    transport = _transport(
        [_entry("20261021_latest.mcl")],
        {"https://gitea.example/raw/20261021_latest.mcl": b"<html>error page</html>"},
    )

    with pytest.raises(ValueError):
        await refresh_mcl(LISTING_URL, dest, transport=transport)
    assert dest.read_bytes() == MCL_BODY  # the good copy is kept


async def test_refresh_mcl_without_latest_file_in_listing(tmp_path: Path) -> None:
    transport = _transport([_entry("update.sh")], {})

    with pytest.raises(ValueError):
        await refresh_mcl(LISTING_URL, tmp_path / "latest.mcl", transport=transport)


def test_read_mcl_maps_volumes_to_issue_ids(tmp_path: Path) -> None:
    from cvproxy.mcl import read_mcl

    path = tmp_path / "latest.mcl"
    path.write_bytes(b"\xef\xbb\xbfMissing;2026-10-20\n766;92458, 92459;2,4\n769;;\nbad;1;2\n")

    assert read_mcl(path) == {766: {92458, 92459}, 769: set()}
