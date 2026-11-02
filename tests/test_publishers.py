"""Publisher allowlist (what the backfills work on) and blocklist (never served or stored)."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cvproxy.app import create_app
from cvproxy.config import Settings, override_settings
from cvproxy.db import Database
from cvproxy.publishers import SEED_ALLOW, SEED_BLOCK
from cvproxy.scheduler import _publisher_backfill_ids

BLOCKED_PUB = 900
BLOCKED_VOL = 9000


def _add_blocked_publisher_data(db: Database) -> None:
    """Publisher 900 with volume 9000, two issues and a cover hash."""
    db.upsert_volume({"id": BLOCKED_VOL, "name": "Ausland", "publisher": {"id": BLOCKED_PUB,
                                                                          "name": "Verlag"}})
    for iid in (9001, 9002):
        db.upsert_issue({"id": iid, "volume": {"id": BLOCKED_VOL}, "issue_number": "1",
                         "image": {"original_url": f"https://x/{iid}.jpg"}})
    db.insert_cover_hashes([(9001, "1", "2", "https://x/9001.jpg")])


def _ids(db: Database, sql: str) -> set[int]:
    return {row[0] for row in db.conn.execute(sql)}


# -- lists --------------------------------------------------------------------


def test_lists_start_empty_and_seed_only_once(tmp_db: Database) -> None:
    assert tmp_db.publisher_lists() == {"allow": [], "block": []}
    assert tmp_db.seed_publisher_lists({1: "DC Comics"}, {BLOCKED_PUB: "Verlag"})
    tmp_db.set_publisher_list(1, None)
    assert not tmp_db.seed_publisher_lists({1: "DC Comics"}, {BLOCKED_PUB: "Verlag"})
    lists = tmp_db.publisher_lists()
    assert lists["allow"] == []
    assert lists["block"] == [{"id": BLOCKED_PUB, "name": "Verlag"}]


def test_list_entries_show_the_stored_publisher_name(tmp_db: Database) -> None:
    tmp_db.set_publisher_list(2, "allow", name="old name")
    assert tmp_db.publisher_lists()["allow"] == [{"id": 2, "name": "Marvel"}]


def test_blocking_a_publisher_records_its_volumes_and_unblocking_forgets_them(
    tmp_db: Database,
) -> None:
    _add_blocked_publisher_data(tmp_db)
    tmp_db.set_publisher_list(BLOCKED_PUB, "block")
    assert tmp_db.blocked_publishers == {BLOCKED_PUB}
    assert tmp_db.blocked_volumes == {BLOCKED_VOL}
    tmp_db.set_publisher_list(BLOCKED_PUB, "allow")
    assert tmp_db.blocked_publishers == set() and tmp_db.blocked_volumes == set()
    assert tmp_db.allowed_publishers == {BLOCKED_PUB}


def test_bad_list_name_is_rejected(tmp_db: Database) -> None:
    with pytest.raises(ValueError):
        tmp_db.set_publisher_list(1, "maybe")


def test_seed_lists_are_disjoint_and_cover_the_requested_publishers() -> None:
    assert not set(SEED_ALLOW) & set(SEED_BLOCK)
    assert SEED_ALLOW[31] == "Marvel" and SEED_ALLOW[10] == "DC Comics"
    assert "Shueisha" in SEED_BLOCK.values()


# -- nothing of a blocked publisher is stored ----------------------------------


def test_blocked_publishers_volumes_and_issues_are_not_stored(tmp_db: Database) -> None:
    tmp_db.set_publisher_list(BLOCKED_PUB, "block")
    _add_blocked_publisher_data(tmp_db)
    assert BLOCKED_VOL not in _ids(tmp_db, "SELECT id FROM cv_volume")
    assert not _ids(tmp_db, "SELECT id FROM cv_issue WHERE id IN (9001, 9002)")
    # the volume is remembered, so issues that only name it are recognised later
    assert BLOCKED_VOL in tmp_db.blocked_volumes
    tmp_db.upsert_issue({"id": 9003, "volume": {"id": BLOCKED_VOL}})
    assert not _ids(tmp_db, "SELECT id FROM cv_issue WHERE id = 9003")


# -- purge ----------------------------------------------------------------------


def test_purge_deletes_a_blocked_publishers_issues_covers_and_volumes(tmp_db: Database) -> None:
    _add_blocked_publisher_data(tmp_db)
    tmp_db.cache_put_single("volume", BLOCKED_VOL, {"id": BLOCKED_VOL})
    tmp_db.set_publisher_list(BLOCKED_PUB, "block")
    result = tmp_db.purge_blocked(batch=1)
    assert result == {"issues": 2, "covers": 1, "volumes": 1, "cached_responses": 1}
    assert not _ids(tmp_db, "SELECT id FROM cv_issue WHERE id IN (9001, 9002)")
    assert not _ids(tmp_db, "SELECT cvid FROM comic_covers WHERE cvid = 9001")
    assert BLOCKED_VOL not in _ids(tmp_db, "SELECT id FROM cv_volume")
    assert {1073108, 1073109} <= _ids(tmp_db, "SELECT id FROM cv_issue")
    assert BLOCKED_PUB in _ids(tmp_db, "SELECT id FROM cv_publisher")
    assert tmp_db.purge_blocked() == {"issues": 0, "covers": 0, "volumes": 0,
                                      "cached_responses": 0}


async def test_purge_job_only_counts_on_a_dry_run(tmp_db: Database) -> None:
    from cvproxy.scheduler import _purge_blocked

    _add_blocked_publisher_data(tmp_db)
    tmp_db.set_publisher_list(BLOCKED_PUB, "block")
    assert await _purge_blocked(tmp_db, dry_run=True) == {
        "dry_run": True, "issues": 2, "volumes": 1}
    result = await _purge_blocked(tmp_db, dry_run=False)
    assert result["issues"] == 2 and result["volumes"] == 1
    assert (await _purge_blocked(tmp_db, dry_run=False))["stop_reason"] == "nothing_to_do"


# -- backfills only work on allowed publishers ---------------------------------


def _list_only_issue(db: Database, iid: int, volume_id: int) -> None:
    db.upsert_issue({"id": iid, "volume": {"id": volume_id}, "issue_number": "1",
                     "store_date": "2024-01-01", "image": {"original_url": f"https://x/{iid}.jpg"}})


def test_backfills_only_pick_issues_of_allowed_publishers(tmp_db: Database) -> None:
    from cvproxy.scheduler import _issue_backfill_ids

    tmp_db.upsert_volume({"id": 7000, "name": "Other", "publisher": {"id": 2, "name": "Marvel"}})
    _list_only_issue(tmp_db, 7001, 7000)
    _list_only_issue(tmp_db, 7002, 160294)
    assert {7001, 7002} <= set(_issue_backfill_ids(tmp_db))
    tmp_db.set_publisher_list(1, "allow")
    assert 7001 not in _issue_backfill_ids(tmp_db)
    assert 7002 in _issue_backfill_ids(tmp_db)
    assert 7001 not in tmp_db.issue_ids_needing_detail(100)
    assert 7002 in tmp_db.issue_ids_needing_detail(100)
    covers = {cvid for cvid, _ in tmp_db.cover_hash_candidates(100)}
    assert 7001 not in covers and 7002 in covers
    snap = tmp_db.queue_snapshot()
    assert snap["issues_missing_covers_in_window"] == len(covers)
    assert snap["issues_missing_details_in_window"] == len(tmp_db.issue_ids_needing_detail(100))


def test_volume_and_publisher_backfills_only_pick_allowed_publishers(tmp_db: Database) -> None:
    tmp_db.upsert_volume({"id": 7000, "name": "Other", "publisher": {"id": 2, "name": "Marvel"}})
    calendar, old = tmp_db.volume_ids_needing_backfill(100, 100)
    assert 7000 in calendar + old
    tmp_db.set_publisher_list(1, "allow")
    calendar, old = tmp_db.volume_ids_needing_backfill(100, 100)
    assert 7000 not in calendar + old and 160294 in calendar + old
    assert tmp_db.queue_snapshot()["volumes_missing_fields"] == len(calendar + old)
    assert _publisher_backfill_ids(tmp_db) == [1]


def test_without_an_allowlist_backfills_skip_only_blocked_publishers(tmp_db: Database) -> None:
    tmp_db.upsert_volume({"id": 7000, "name": "Other", "publisher": {"id": 2, "name": "Marvel"}})
    tmp_db.set_publisher_list(2, "block")
    calendar, old = tmp_db.volume_ids_needing_backfill(100, 100)
    assert 160294 in calendar + old
    assert 2 not in _publisher_backfill_ids(tmp_db)


# -- export ---------------------------------------------------------------------


def test_export_leaves_out_blocked_publishers(tmp_db: Database, tmp_path: Path) -> None:
    _add_blocked_publisher_data(tmp_db)
    tmp_db.set_publisher_list(BLOCKED_PUB, "block")  # before the purge has run
    counts = tmp_db.export_comicvine_db(tmp_path / "comicvine.db")
    assert counts == {"cv_publisher": 2, "cv_volume": 2, "cv_issue": 2, "comic_covers": 0}


# -- API responses --------------------------------------------------------------


def _blocked_volume(vid: int = BLOCKED_VOL) -> dict[str, Any]:
    return {"id": vid, "name": "Ausland", "publisher": {"id": BLOCKED_PUB, "name": "Verlag"}}


def _upstream(request: httpx.Request) -> httpx.Response:
    """ComicVine answering with one blocked and one allowed result."""
    path = request.url.path
    if "/volumes/" in path or "/search/" in path:
        results: Any = [
            {**_blocked_volume(9100), "resource_type": "volume"},
            {"id": 9200, "name": "Fine", "publisher": {"id": 1, "name": "DC Comics"},
             "resource_type": "volume"},
        ]
    elif "/volume/" in path:
        results = _blocked_volume(9300)
    else:
        return httpx.Response(503)
    return httpx.Response(200, json={
        "status_code": 1, "error": "OK", "number_of_total_results": 2,
        "number_of_page_results": 2, "limit": 100, "offset": 0, "results": results,
    })


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient]:
    override_settings(Settings(
        cv_api_key="test_key", db_path=tmp_path / "test.db",
        image_cache_dir=tmp_path / "images", stats_db_path=tmp_path / "stats.db",
        rate_state_path=tmp_path / "rate_state.json", sync_enabled=False,
    ))
    with TestClient(create_app()) as client:
        client.app.state.cv_client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream)
        )
        db: Database = client.app.state.db
        db.upsert_volume({"id": 160294, "name": "Absolute Batman",
                          "publisher": {"id": 1, "name": "DC Comics"}})
        db.upsert_issue({"id": 1073108, "volume": {"id": 160294}, "issue_number": "1"})
        _add_blocked_publisher_data(db)
        db.set_publisher_list(BLOCKED_PUB, "block")
        yield client


def test_app_start_seeds_the_default_lists(client: TestClient) -> None:
    lists = client.get("/admin/publishers").json()
    assert {p["id"] for p in lists["allow"]} >= set(SEED_ALLOW)
    assert {p["id"] for p in lists["block"]} >= set(SEED_BLOCK) | {BLOCKED_PUB}


def test_cached_blocked_volume_and_issues_are_not_served(client: TestClient) -> None:
    body = client.get(f"/api/volume/4050-{BLOCKED_VOL}/").json()
    assert body["status_code"] == 101 and body["results"] == []
    assert client.get("/api/issue/4000-9001/").json()["status_code"] == 101
    issues = client.get("/api/issues/", params={"filter": f"volume:{BLOCKED_VOL}"}).json()
    assert issues["results"] == []
    assert client.get("/api/publisher/4010-900/").json()["status_code"] == 101


def test_blocked_results_from_comicvine_are_dropped_and_not_stored(client: TestClient) -> None:
    body = client.get("/api/volumes/", params={"filter": "name:zzz"}).json()
    assert [r["id"] for r in body["results"]] == [9200]
    assert body["number_of_page_results"] == 1
    single = client.get("/api/volume/4050-9300/").json()
    assert single["status_code"] == 101
    db: Database = client.app.state.db
    assert not _ids(db, "SELECT id FROM cv_volume WHERE id IN (9100, 9300)")


def test_search_results_drop_blocked_volumes(client: TestClient) -> None:
    body = client.get("/api/search/", params={"query": "ausland", "resources": "volume"}).json()
    assert BLOCKED_VOL not in [r["id"] for r in body["results"]]
    assert 9100 not in [r["id"] for r in body["results"]]


def test_xml_responses_are_filtered_too(client: TestClient) -> None:
    resp = client.get(f"/api/volume/4050-{BLOCKED_VOL}/", params={"format": "xml"})
    assert "Ausland" not in resp.text


def test_publisher_lists_are_edited_from_the_dashboard_api(client: TestClient) -> None:
    resp = client.put("/admin/publishers/2", json={"list": "allow"})
    assert resp.status_code == 200
    assert 2 in {p["id"] for p in resp.json()["allow"]}
    resp = client.put("/admin/publishers/2", json={"list": None})
    assert 2 not in {p["id"] for p in resp.json()["allow"]}
    assert client.put("/admin/publishers/2", json={"list": "maybe"}).status_code == 422
    found = client.get("/admin/publishers/search", params={"q": "verl"}).json()
    assert found[0] == {"id": BLOCKED_PUB, "name": "Verlag", "list": "block"}
