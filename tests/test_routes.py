"""Tests for the API routes using the FastAPI test client."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from cvproxy.app import create_app
from cvproxy.config import Settings, override_settings
from cvproxy.db import Database
from cvproxy.stats import StatsTracker


def _seed_db(db: Database) -> None:
    """Insert standard test fixtures into an already-connected Database."""
    db.upsert_publisher({"id": 1, "name": "DC Comics"})
    db.upsert_publisher({"id": 2, "name": "Marvel"})
    db.upsert_volume({
        "id": 160294, "name": "Absolute Batman", "start_year": "2024",
        "publisher": {"id": 1, "name": "DC Comics"}, "count_of_issues": 17,
        "description": "Description",
        "image": {"original_url": "https://example.com/img.jpg"},
        "site_detail_url": "https://example.com/vol",
    })
    db.upsert_volume({
        "id": 100001, "name": "Batman", "aliases": "The Batman", "start_year": "2016",
        "publisher": {"id": 1, "name": "DC Comics"}, "count_of_issues": 150,
        "description": "Tom King Batman run",
    })
    db.upsert_volume({
        "id": 100002, "name": "Batman: Arkham", "start_year": "2010",
        "publisher": {"id": 1, "name": "DC Comics"}, "count_of_issues": 50,
        "description": "Arkham series",
    })
    _issue = lambda iid, vid, name, num, cdate, sdate, desc, img=None, url=None: {  # noqa: E731
        "id": iid, "volume": {"id": vid}, "name": name, "issue_number": num,
        "cover_date": cdate, "store_date": sdate, "description": desc,
        "image": {"original_url": img} if img else None, "site_detail_url": url,
        "character_credits": [], "person_credits": [], "team_credits": [],
        "location_credits": [], "story_arc_credits": [], "associated_images": [],
    }
    db.upsert_issue(_issue(1073108, 160294, "Issue 1", "1", "2024-12-01", "2024-11-06",
                            "First issue", "https://example.com/issue.jpg", "https://example.com/issue"))
    db.upsert_issue(_issue(1073109, 160294, "Issue 2", "2", "2025-01-01", "2024-12-04", "Second issue"))
    db.upsert_issue(_issue(1073110, 100001, "Batman 1", "1", "2016-06-01", "2016-04-06", "Issue 1"))
    db.upsert_issue(_issue(1073111, 100001, "Batman 50", "50", "2018-12-01", "2018-10-03", "Issue 50"))
    db.upsert_issue(_issue(1073112, 160294, "Issue 3", "3", None, "2025-02-04", "Third issue no cover date"))


def _create_test_db(db_path: Path) -> None:
    db = Database(db_path)
    db.connect()
    _seed_db(db)
    db.close()


@pytest.fixture
def app_client(tmp_path: Path) -> Generator[TestClient]:
    """Create a test client with a seeded database."""
    db_path = tmp_path / "test.db"
    _create_test_db(db_path)

    settings = Settings(
        cv_api_key="test_key",
        db_path=db_path,
        image_cache_dir=tmp_path / "images",
        stats_db_path=tmp_path / "stats.db",
        sync_enabled=False,
    )
    override_settings(settings)

    app = create_app()
    with TestClient(app) as client:
        yield client


def test_health(app_client: TestClient) -> None:
    resp = app_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "database" in data
    assert "image_cache" in data


def test_search_volumes(app_client: TestClient) -> None:
    resp = app_client.get(
        "/api/search/", params={"query": "Absolute Batman"},
        headers={"X-CV-Cache": "only-if-cached"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert len(data["results"]) >= 1
    assert data["results"][0]["name"] == "Absolute Batman"


def test_search_empty_query(app_client: TestClient) -> None:
    resp = app_client.get("/api/search/", params={"query": ""})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] != 1  # error


def test_get_volume(app_client: TestClient) -> None:
    resp = app_client.get("/api/volume/4050-160294/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert data["results"]["name"] == "Absolute Batman"
    assert "issues" in data["results"]


def test_get_volume_not_found(app_client: TestClient) -> None:
    # This will try upstream (which will fail in test), so we get an error
    resp = app_client.get("/api/volume/4050-999999/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 101


def test_get_issue(app_client: TestClient) -> None:
    resp = app_client.get("/api/issue/4000-1073108/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert data["results"]["issue_number"] == "1"


def test_get_publisher(app_client: TestClient) -> None:
    resp = app_client.get("/api/publisher/4010-1/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert data["results"]["name"] == "DC Comics"


def test_list_issues_by_volume(app_client: TestClient) -> None:
    resp = app_client.get("/api/issues/", params={"filter": "volume:160294"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert len(data["results"]) >= 1


def test_list_volumes_default_fields(app_client: TestClient) -> None:
    """List volumes from DB returns CV-compatible default fields (no characters/concepts)."""
    resp = app_client.get("/api/volumes/", params={"filter": "name:Batman"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert len(data["results"]) >= 1
    result = data["results"][0]
    # Core list fields must be present
    assert "id" in result
    assert "name" in result
    assert "publisher" in result
    assert "count_of_issues" in result
    # Detail-only relational fields must be absent
    assert "characters" not in result
    assert "concepts" not in result
    assert "people" not in result
    assert "objects" not in result


def test_list_volumes_by_id_default_fields(app_client: TestClient) -> None:
    """List volumes by id filter also returns list default fields only."""
    resp = app_client.get("/api/volumes/", params={"filter": "id:160294|100001"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert len(data["results"]) >= 1
    result = data["results"][0]
    assert "id" in result
    assert "characters" not in result
    assert "concepts" not in result


def test_list_issues_default_fields(app_client: TestClient) -> None:
    """List issues from DB returns CV-compatible default fields (no credit arrays)."""
    resp = app_client.get("/api/issues/", params={"filter": "volume:160294"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert len(data["results"]) >= 1
    result = data["results"][0]
    # Core list fields must be present
    assert "id" in result
    assert "issue_number" in result
    assert "volume" in result
    assert "cover_date" in result
    # Detail-only credit arrays must be absent
    assert "character_credits" not in result
    assert "person_credits" not in result
    assert "team_credits" not in result
    assert "story_arc_credits" not in result
    assert "first_appearance_characters" not in result


def test_list_publishers_default_fields(app_client: TestClient) -> None:
    """List publishers from DB returns CV-compatible default fields."""
    resp = app_client.get("/api/publishers/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert len(data["results"]) >= 1
    result = data["results"][0]
    # Core list fields must be present
    assert "id" in result
    assert "name" in result
    # Detail-only relational fields must be absent
    assert "story_arcs" not in result
    assert "teams" not in result
    assert "volumes" not in result
    assert "characters" not in result


def test_search_includes_resource_type(app_client: TestClient) -> None:
    """Search results from DB include resource_type field."""
    resp = app_client.get(
        "/api/search/", params={"query": "Absolute Batman"},
        headers={"X-CV-Cache": "only-if-cached"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert len(data["results"]) >= 1
    assert data["results"][0].get("resource_type") == "volume"


def test_search_default_fields(app_client: TestClient) -> None:
    """Search results from DB omit detail-only relational fields."""
    resp = app_client.get(
        "/api/search/", params={"query": "Absolute Batman"},
        headers={"X-CV-Cache": "only-if-cached"},
    )
    data = resp.json()
    result = data["results"][0]
    assert "characters" not in result
    assert "concepts" not in result
    assert "resource_type" in result


def test_search_relevance_key_ordering() -> None:
    """Relevance sort key produces the correct tier ordering."""
    from cvproxy.routes.api import _search_relevance_key

    query = "Amazing Spider-Man"

    exact        = _search_relevance_key("Amazing Spider-Man", query)          # tier 0
    all_terms    = _search_relevance_key("The Amazing Spider-Man", query)      # tier 1, shorter
    all_long     = _search_relevance_key("Amazing Spider-Man 2099", query)     # tier 1, longer
    partial_one  = _search_relevance_key("Amazing Fantasy", query)             # tier 2, 1 missing
    partial_none = _search_relevance_key("Daredevil", query)                   # tier 2, all missing
    null_name    = _search_relevance_key(None, query)                          # tier 3

    assert exact < all_terms,       "Exact match must rank above all-terms match"
    assert all_terms < all_long,    "Shorter all-term names rank higher"
    assert all_terms < partial_one, "All-terms must rank above partial match"
    assert partial_one < partial_none, "Fewer missing terms must rank higher"
    assert partial_none < null_name, "Any name must rank above None"

    # Tiebreaker: within the same tier, more recent date_last_updated ranks first
    newer = _search_relevance_key("The Amazing Spider-Man", query, "2024-01-01")
    older = _search_relevance_key("The Amazing Spider-Man", query, "2020-01-01")
    no_date = _search_relevance_key("The Amazing Spider-Man", query, None)
    assert newer < older,   "More recent date must rank higher"
    assert older < no_date, "Any date must rank above no date"


def test_list_volumes_field_list_respected(app_client: TestClient) -> None:
    """When field_list is given, only those fields are returned (ignores list defaults)."""
    resp = app_client.get(
        "/api/volumes/", params={"filter": "name:Batman", "field_list": "id,name"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    result = data["results"][0]
    assert set(result.keys()) == {"id", "name"}


def test_dashboard_html(app_client: TestClient) -> None:
    resp = app_client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "CVProxy" in resp.text
    assert "sourceChart" in resp.text


def test_dashboard_data_empty(app_client: TestClient) -> None:
    resp = app_client.get("/dashboard/data")
    assert resp.status_code == 200
    data = resp.json()
    assert data["totals"]["total_requests"] == 0
    assert data["by_client"] == []


def test_dashboard_data_records_after_search(app_client: TestClient) -> None:
    # Make a search request that will hit the local FTS cache
    app_client.get(
        "/api/search/", params={"query": "Absolute Batman"},
        headers={"X-CV-Cache": "only-if-cached"},
    )

    resp = app_client.get("/dashboard/data")
    data = resp.json()
    assert data["totals"]["total_requests"] == 1
    assert data["totals"]["cache_hits"] == 1
    assert len(data["by_client"]) == 1
    recent = data["recent_requests"]
    assert len(recent) == 1
    assert "query_url" in recent[0]
    assert "Absolute+Batman" in recent[0]["query_url"] or "Absolute%20Batman" in recent[0]["query_url"]
    assert "api_key" not in recent[0]["query_url"]


# ---------------------------------------------------------------------------
# Rate limit responses
# ---------------------------------------------------------------------------


def test_upstream_rate_limit_response_propagated_to_client(app_client: TestClient) -> None:
    """When CVClient.get_issue() returns RATE_LIMITED the route must return status 107."""
    from unittest.mock import AsyncMock

    from cvproxy.cv_client import RATE_LIMITED

    cv_client = app_client.app.state.cv_client
    with patch.object(cv_client, "get_issue", new=AsyncMock(return_value=RATE_LIMITED)):
        resp = app_client.get("/api/issue/4000-999999/")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 107
    assert data["error"] == "Rate Limit Exceeded"


def test_route_returns_rate_limit_error_when_above_190_threshold(app_client: TestClient) -> None:
    """When total_hourly_calls() >= 190 the route must refuse upstream and return 107."""
    cv_client = app_client.app.state.cv_client
    with patch.object(cv_client, "total_hourly_calls", return_value=190):
        resp = app_client.get("/api/issue/4000-999999/")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 107
    assert data["error"] == "Rate Limit Exceeded"


def test_route_does_not_rate_limit_below_threshold(app_client: TestClient) -> None:
    """Below the threshold the route should attempt upstream (returning miss/101, not 107)."""
    cv_client = app_client.app.state.cv_client
    with patch.object(cv_client, "total_hourly_calls", return_value=0):
        resp = app_client.get("/api/issue/4000-999999/")

    assert resp.status_code == 200
    data = resp.json()
    # Should get "not found" (101), not "rate limited" (107)
    assert data["status_code"] == 101


def test_dashboard_data_records_volume_hit(app_client: TestClient) -> None:
    app_client.get("/api/volume/4050-160294/")

    resp = app_client.get("/dashboard/data")
    data = resp.json()
    assert data["totals"]["total_requests"] == 1
    assert data["totals"]["cache_hits"] == 1
    assert any(e["endpoint"] == "volume" for e in data["by_endpoint"])


def test_dashboard_data_hours_filter(app_client: TestClient) -> None:
    app_client.get("/api/search/", params={"query": "Absolute Batman"})

    resp = app_client.get("/dashboard/data", params={"hours": 1})
    data = resp.json()
    assert data["period_hours"] == 1.0
    assert data["totals"]["total_requests"] == 1


def test_list_issues_by_store_date_range_cache_hit(app_client: TestClient) -> None:
    """Date range filter returns matching issues from cache."""
    resp = app_client.get(
        "/api/issues/", params={"filter": "store_date:2024-11-01|2024-11-30"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert len(data["results"]) == 1
    assert data["results"][0]["id"] == 1073108
    assert data["number_of_total_results"] == 1


def test_list_issues_by_store_date_range_no_cache_hit(app_client: TestClient) -> None:
    """Date range with no cached match tries upstream (fails in test env) and returns empty."""
    resp = app_client.get(
        "/api/issues/", params={"filter": "store_date:2026-01-01|2026-01-31"}
    )
    assert resp.status_code == 200
    data = resp.json()
    # Upstream will fail in test (no real API key); should return empty envelope
    assert data["status_code"] == 1
    assert data["results"] == []


def test_list_issues_total_count_matches_results(app_client: TestClient) -> None:
    """number_of_total_results must reflect actual match count, not len(page)."""
    resp = app_client.get("/api/issues/", params={"filter": "volume:160294"})
    data = resp.json()
    assert data["number_of_total_results"] == data["number_of_page_results"]
    assert data["number_of_total_results"] == 3


def test_list_volumes_filter_no_match_uses_upstream(app_client: TestClient) -> None:
    """When local cache misses, upstream call should be attempted gracefully."""
    resp = app_client.get(
        "/api/volumes/",
        params={"filter": "name:UnknownSeriesXYZ", "sort": "name:asc", "field_list": "id,name"},
    )
    assert resp.status_code == 200
    # Upstream will fail in test (no real key), returns empty envelope — not a 500
    data = resp.json()
    assert data["status_code"] == 1
    assert isinstance(data["results"], list)


def test_list_volumes_by_id_filter_cache_hit(app_client: TestClient) -> None:
    """filter=id:160294 returns matching volume from cache."""
    resp = app_client.get("/api/volumes/", params={"filter": "id:160294"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert len(data["results"]) == 1
    assert data["results"][0]["id"] == 160294


def test_list_volumes_by_id_filter_partial_match(app_client: TestClient) -> None:
    """filter=id:160294|999999 returns the matching volume from cache even if some IDs missing."""
    resp = app_client.get("/api/volumes/", params={"filter": "id:160294|999999"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    ids = {r["id"] for r in data["results"]}
    assert 160294 in ids


def test_list_volumes_by_id_filter_all_miss_tries_upstream(app_client: TestClient) -> None:
    """filter=id:999999 with no cache hit falls through to upstream gracefully (no 500)."""
    resp = app_client.get("/api/volumes/", params={"filter": "id:999999"})
    assert resp.status_code == 200
    # Upstream will fail in test (no real key), returns empty envelope — not a 500
    data = resp.json()
    assert data["status_code"] == 1
    assert isinstance(data["results"], list)


def test_list_volumes_cache_control_flow(app_client: TestClient) -> None:
    """only-if-cached/no-cache works for volumes list and writes local cache for follow-up."""
    # step 2: only-if-cached should return empty when no local match
    resp = app_client.get(
        "/api/volumes/",
        params={"filter": "name:NoSuchSeriesXYZ"},
        headers={"X-CV-Cache": "only-if-cached"},
    )
    assert resp.status_code == 200
    assert resp.json()["number_of_page_results"] == 0

    # step 3: no-cache should fetch from upstream and upsert it
    from unittest.mock import AsyncMock

    mock_client = AsyncMock()
    mock_client.total_hourly_calls = MagicMock(return_value=0)
    mock_client.get.return_value = {
        "status_code": 1,
        "error": "OK",
        "number_of_total_results": 1,
        "number_of_page_results": 1,
        "limit": 100,
        "offset": 0,
        "results": [
            {
                "id": 77777,
                "name": "NoSuchSeriesXYZ",
                "publisher": {"id": 1, "name": "DC Comics"},
            }
        ],
    }
    app_client.app.state.cv_client = mock_client

    resp = app_client.get(
        "/api/volumes/",
        params={"filter": "name:NoSuchSeriesXYZ"},
        headers={"X-CV-Cache": "no-cache"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1

    # step 4: only-if-cached should now return the new item from DB
    resp = app_client.get(
        "/api/volumes/",
        params={"filter": "name:NoSuchSeriesXYZ"},
        headers={"X-CV-Cache": "only-if-cached"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["results"]) >= 1
    assert any(r["name"] == "NoSuchSeriesXYZ" for r in resp.json()["results"])


def test_generic_single_cache_control_flow(app_client: TestClient) -> None:
    """only-if-cached and no-cache should work for generic single resources."""
    # step 2: only-if-cached no local resource
    resp = app_client.get(
        "/api/character/4005-999999/",
        headers={"X-CV-Cache": "only-if-cached"},
    )
    assert resp.status_code == 200
    assert resp.json()["status_code"] == 101

    # step 3: no-cache should fetch upstream and cache
    from unittest.mock import AsyncMock

    mock_client = AsyncMock()
    mock_client.total_hourly_calls = MagicMock(return_value=0)
    mock_client.get.return_value = {
        "status_code": 1,
        "error": "OK",
        "number_of_total_results": 1,
        "number_of_page_results": 1,
        "limit": 1,
        "offset": 0,
        "results": {
            "id": 999999,
            "name": "Generic Hero",
            "deck": "Test character",
        },
    }
    app_client.app.state.cv_client = mock_client

    resp = app_client.get(
        "/api/character/4005-999999/",
        headers={"X-CV-Cache": "no-cache"},
    )
    assert resp.status_code == 200
    assert resp.json()["status_code"] == 1
    assert resp.json()["results"]["id"] == 999999

    # step 4: only-if-cached should now return from response cache
    resp = app_client.get(
        "/api/character/4005-999999/",
        headers={"X-CV-Cache": "only-if-cached"},
    )
    assert resp.status_code == 200
    assert resp.json()["status_code"] == 1
    assert resp.json()["results"]["name"] == "Generic Hero"


def test_list_issues_by_pipe_separated_volumes(app_client: TestClient) -> None:
    """filter=volume:160294|999999 returns issues from cached volumes."""
    resp = app_client.get("/api/issues/", params={"filter": "volume:160294|999999"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert len(data["results"]) >= 1


def test_list_issues_single_volume_still_works(app_client: TestClient) -> None:
    """filter=volume:160294 (single, no pipe) still works correctly."""
    resp = app_client.get("/api/issues/", params={"filter": "volume:160294"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert len(data["results"]) >= 1


# ---------------------------------------------------------------------------
# Write-through: upsert on upstream fetch
# ---------------------------------------------------------------------------


def test_get_volume_upstream_hit_upserts_to_db(app_client: TestClient) -> None:
    """Volume fetched from upstream should be stored in DB for future cache hits."""
    from unittest.mock import AsyncMock

    upstream_response = {
        "status_code": 1,
        "error": "OK",
        "number_of_total_results": 1,
        "number_of_page_results": 1,
        "limit": 1,
        "offset": 0,
        "results": {
            "id": 77777,
            "name": "Upstream Volume",
            "aliases": None,
            "start_year": "2024",
            "publisher": {"id": 10, "name": "DC Comics"},
            "count_of_issues": 3,
            "description": None,
            "image": {"original_url": "https://example.com/img.jpg"},
            "site_detail_url": "https://comicvine.gamespot.com/v/77777/",
            "issues": [],
        },
    }

    mock_client = AsyncMock()
    mock_client.total_hourly_calls = MagicMock(return_value=0)
    mock_client.get_volume.return_value = upstream_response
    app_client.app.state.cv_client = mock_client

    resp = app_client.get("/api/volume/77777/", params={"api_key": "testkey"})
    assert resp.status_code == 200

    # Now verify the volume was upserted to DB — a second request should be a cache hit
    mock_client.get_volume.reset_mock()
    resp2 = app_client.get("/api/volume/77777/", params={"api_key": "testkey"})
    assert resp2.status_code == 200
    assert resp2.json()["results"]["name"] == "Upstream Volume"
    # Should NOT have called upstream again
    mock_client.get_volume.assert_not_called()


def test_get_issue_upstream_hit_upserts_to_db(app_client: TestClient) -> None:
    """Issue fetched from upstream should be stored in DB for future cache hits."""
    from unittest.mock import AsyncMock

    upstream_response = {
        "status_code": 1,
        "error": "OK",
        "number_of_total_results": 1,
        "number_of_page_results": 1,
        "limit": 1,
        "offset": 0,
        "results": {
            "id": 88888,
            "volume": {"id": 160294, "name": "Absolute Batman"},
            "name": "Upstream Issue",
            "issue_number": "5",
            "cover_date": "2025-03-01",
            "store_date": "2025-03-05",
            "description": None,
            "image": {"original_url": "https://example.com/issue.jpg"},
            "site_detail_url": "https://comicvine.gamespot.com/i/88888/",
            "character_credits": [],
            "person_credits": [],
            "team_credits": [],
            "location_credits": [],
            "story_arc_credits": [],
            "associated_images": [],
        },
    }

    mock_client = AsyncMock()
    mock_client.total_hourly_calls = MagicMock(return_value=0)
    mock_client.get_issue.return_value = upstream_response
    app_client.app.state.cv_client = mock_client

    resp = app_client.get("/api/issue/88888/", params={"api_key": "testkey"})
    assert resp.status_code == 200

    # Verify upserted — second request should be a cache hit
    mock_client.get_issue.reset_mock()
    resp2 = app_client.get("/api/issue/88888/", params={"api_key": "testkey"})
    assert resp2.status_code == 200
    assert resp2.json()["results"]["issue_number"] == "5"
    mock_client.get_issue.assert_not_called()


def test_get_publisher_upstream_hit_upserts_to_db(app_client: TestClient) -> None:
    """Publisher fetched from upstream should be stored in DB for future cache hits."""
    from unittest.mock import AsyncMock

    upstream_response = {
        "status_code": 1,
        "error": "OK",
        "number_of_total_results": 1,
        "number_of_page_results": 1,
        "limit": 1,
        "offset": 0,
        "results": {
            "id": 99999,
            "name": "Upstream Publisher",
            "site_detail_url": "https://comicvine.gamespot.com/p/99999/",
        },
    }

    mock_client = AsyncMock()
    mock_client.total_hourly_calls = MagicMock(return_value=0)
    mock_client.get_publisher.return_value = upstream_response
    app_client.app.state.cv_client = mock_client

    resp = app_client.get("/api/publisher/99999/", params={"api_key": "testkey"})
    assert resp.status_code == 200

    # Verify upserted — second request should be a cache hit
    mock_client.get_publisher.reset_mock()
    resp2 = app_client.get("/api/publisher/99999/", params={"api_key": "testkey"})
    assert resp2.status_code == 200
    assert resp2.json()["results"]["name"] == "Upstream Publisher"
    mock_client.get_publisher.assert_not_called()


# ---------------------------------------------------------------------------
# Field list filtering
# ---------------------------------------------------------------------------


def test_get_volume_field_list_returns_only_requested_fields(app_client: TestClient) -> None:
    """field_list filters volume result to only requested fields."""
    resp = app_client.get(
        "/api/volume/4050-160294/",
        params={"api_key": "k", "field_list": "id,name"}
    )
    assert resp.status_code == 200
    result = resp.json()["results"]
    assert set(result.keys()) == {"id", "name"}


def test_get_volume_no_field_list_returns_all_fields(app_client: TestClient) -> None:
    """Without field_list, volume result includes all fields."""
    resp = app_client.get("/api/volume/4050-160294/", params={"api_key": "k"})
    result = resp.json()["results"]
    assert "name" in result and "description" in result and "aliases" in result


def test_list_issues_field_list_filters_each_result(app_client: TestClient) -> None:
    """field_list filters each item in issues list."""
    resp = app_client.get(
        "/api/issues/",
        params={"api_key": "k", "filter": "volume:160294", "field_list": "id,issue_number"}
    )
    assert resp.status_code == 200
    for item in resp.json()["results"]:
        assert set(item.keys()) == {"id", "issue_number"}


def test_get_volume_field_list_applied_on_upstream_miss(app_client: TestClient) -> None:
    """field_list should be applied even when data comes from upstream (not in DB)."""
    from unittest.mock import AsyncMock

    mock_client = AsyncMock()
    mock_client.total_hourly_calls = MagicMock(return_value=0)
    mock_client.get_volume.return_value = {
        "status_code": 1,
        "error": "OK",
        "number_of_total_results": 1,
        "number_of_page_results": 1,
        "limit": 1,
        "offset": 0,
        "results": {
            "id": 99991,
            "name": "Fresh Volume",
            "publisher": {},
            "image": {},
            "aliases": "x",
        },
    }
    app_client.app.state.cv_client = mock_client

    resp = app_client.get(
        "/api/volume/4050-99991/",
        params={"api_key": "k", "field_list": "id,name"}
    )
    assert resp.status_code == 200
    result = resp.json()["results"]
    assert set(result.keys()) == {"id", "name"}


def test_get_issue_field_list_applied_on_upstream_miss(app_client: TestClient) -> None:
    """field_list should be applied even when issue comes from upstream (not in DB)."""
    from unittest.mock import AsyncMock

    mock_client = AsyncMock()
    mock_client.total_hourly_calls = MagicMock(return_value=0)
    mock_client.get_issue.return_value = {
        "status_code": 1,
        "error": "OK",
        "number_of_total_results": 1,
        "number_of_page_results": 1,
        "limit": 1,
        "offset": 0,
        "results": {
            "id": 99992,
            "issue_number": "42",
            "name": "Fresh Issue",
            "volume": {},
            "cover_date": "2025-03-01",
        },
    }
    app_client.app.state.cv_client = mock_client

    resp = app_client.get(
        "/api/issue/4000-99992/",
        params={"api_key": "k", "field_list": "id,issue_number"}
    )
    assert resp.status_code == 200
    result = resp.json()["results"]
    assert set(result.keys()) == {"id", "issue_number"}


def test_get_publisher_field_list_applied_on_upstream_miss(app_client: TestClient) -> None:
    """field_list should be applied even when publisher comes from upstream (not in DB)."""
    from unittest.mock import AsyncMock

    mock_client = AsyncMock()
    mock_client.total_hourly_calls = MagicMock(return_value=0)
    mock_client.get_publisher.return_value = {
        "status_code": 1,
        "error": "OK",
        "number_of_total_results": 1,
        "number_of_page_results": 1,
        "limit": 1,
        "offset": 0,
        "results": {
            "id": 99993,
            "name": "Fresh Publisher",
            "site_detail_url": "https://example.com/pub",
        },
    }
    app_client.app.state.cv_client = mock_client

    resp = app_client.get(
        "/api/publisher/4010-99993/",
        params={"api_key": "k", "field_list": "id,name"}
    )
    assert resp.status_code == 200
    result = resp.json()["results"]
    assert set(result.keys()) == {"id", "name"}


# ---------------------------------------------------------------------------
# Sort support
# ---------------------------------------------------------------------------


def test_list_volumes_sort_by_name_asc(app_client: TestClient) -> None:
    """Sort volumes by name ascending (cache hit)."""
    resp = app_client.get(
        "/api/volumes/",
        params={"api_key": "k", "filter": "name:Bat", "sort": "name:asc"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 2
    names = [r["name"] for r in data["results"]]
    assert names == sorted(names, key=str.lower)


def test_list_volumes_sort_by_name_desc(app_client: TestClient) -> None:
    """Sort volumes by name descending (cache hit)."""
    resp = app_client.get(
        "/api/volumes/",
        params={"api_key": "k", "filter": "name:Bat", "sort": "name:desc"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 2
    names = [r["name"] for r in data["results"]]
    assert names == sorted(names, key=str.lower, reverse=True)


def test_list_volumes_sort_by_start_year_asc(app_client: TestClient) -> None:
    """Sort volumes by start_year ascending (cache hit)."""
    resp = app_client.get(
        "/api/volumes/",
        params={"api_key": "k", "filter": "name:Bat", "sort": "start_year:asc"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 2
    years = [r["start_year"] for r in data["results"]]
    assert years == sorted(years, key=str.lower)


def test_list_volumes_sort_by_id_asc(app_client: TestClient) -> None:
    """Sort volumes by id ascending (cache hit)."""
    resp = app_client.get(
        "/api/volumes/",
        params={"api_key": "k", "filter": "name:Bat", "sort": "id:asc"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 2
    ids = [r["id"] for r in data["results"]]
    # IDs are numbers but converted to strings for sorting
    assert ids == sorted(ids, key=lambda x: str(x).lower())


def test_list_volumes_sort_default_asc(app_client: TestClient) -> None:
    """Sort with field only (no :asc/:desc) defaults to ascending."""
    resp = app_client.get(
        "/api/volumes/",
        params={"api_key": "k", "filter": "name:Bat", "sort": "name"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 2
    names = [r["name"] for r in data["results"]]
    assert names == sorted(names, key=str.lower)


def test_list_issues_sort_by_issue_number_asc(app_client: TestClient) -> None:
    """Sort issues by issue_number ascending (cache hit)."""
    resp = app_client.get(
        "/api/issues/",
        params={"api_key": "k", "filter": "volume:100001", "sort": "issue_number:asc"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 2
    numbers = [r["issue_number"] for r in data["results"]]
    assert numbers == sorted(numbers, key=str.lower)


def test_list_issues_sort_by_issue_number_desc(app_client: TestClient) -> None:
    """Sort issues by issue_number descending (cache hit)."""
    resp = app_client.get(
        "/api/issues/",
        params={"api_key": "k", "filter": "volume:100001", "sort": "issue_number:desc"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 2
    numbers = [r["issue_number"] for r in data["results"]]
    assert numbers == sorted(numbers, key=str.lower, reverse=True)


def test_list_issues_sort_by_cover_date_asc(app_client: TestClient) -> None:
    """Sort issues by cover_date ascending (cache hit). None values should be last."""
    resp = app_client.get(
        "/api/issues/",
        params={"api_key": "k", "filter": "volume:160294", "sort": "cover_date:asc"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 2
    dates = [r["cover_date"] for r in data["results"]]
    # Separate None and non-None values
    non_none_dates = [d for d in dates if d is not None]
    none_dates = [d for d in dates if d is None]
    # Non-None dates should be sorted ascending, None values at the end
    assert dates == sorted(non_none_dates, key=str.lower) + none_dates


def test_list_volumes_sort_with_field_list(app_client: TestClient) -> None:
    """Sort applied before field_list filtering, allowing sort by any field."""
    resp = app_client.get(
        "/api/volumes/",
        params={
            "api_key": "k",
            "filter": "name:Bat",
            "sort": "start_year:asc",
            "field_list": "id,name"
        }
    )
    assert resp.status_code == 200
    data = resp.json()
    results = data["results"]
    # Results should only have id and name (field_list applied)
    for r in results:
        assert set(r.keys()) == {"id", "name"}
    # But the order should match start_year sorting from the full records
    assert len(results) >= 2


def test_list_volumes_sort_empty_string(app_client: TestClient) -> None:
    """Empty sort string returns results in natural order."""
    resp = app_client.get(
        "/api/volumes/",
        params={"api_key": "k", "filter": "name:Batman", "sort": ""}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 1


def test_list_volumes_by_id_filter_with_sort(app_client: TestClient) -> None:
    """Sort applies to id filter cache hits too."""
    resp = app_client.get(
        "/api/volumes/",
        params={"api_key": "k", "filter": "id:160294|100001|100002", "sort": "name:asc"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 2
    names = [r["name"] for r in data["results"]]
    assert names == sorted(names, key=str.lower)


def test_list_issues_sort_desc_puts_none_last(app_client: TestClient) -> None:
    """Descending sort should put None values at the end, not the front."""
    resp = app_client.get(
        "/api/issues/",
        params={"api_key": "k", "filter": "volume:160294", "sort": "cover_date:desc"}
    )
    assert resp.status_code == 200
    issues = resp.json()["results"]
    dates = [i.get("cover_date") for i in issues]
    # All None values should be at the end
    non_none_dates = [d for d in dates if d is not None]
    none_dates = [d for d in dates if d is None]
    assert dates == non_none_dates + none_dates
    # Non-None dates should be in descending order
    assert non_none_dates == sorted(non_none_dates, reverse=True)


# ---------------------------------------------------------------------------
# Publishers
# ---------------------------------------------------------------------------


def test_list_publishers_returns_all(app_client: TestClient) -> None:
    """List all publishers without filter returns cached publishers."""
    resp = app_client.get("/api/publishers/", params={"api_key": "k"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert data["number_of_total_results"] >= 2  # DC Comics and Marvel in fixtures


def test_list_publishers_name_filter(app_client: TestClient) -> None:
    """Filter publishers by name (DC Comics)."""
    resp = app_client.get(
        "/api/publishers/", params={"api_key": "k", "filter": "name:DC Comics"}
    )
    assert resp.status_code == 200
    names = [r["name"] for r in resp.json()["results"]]
    assert all("DC" in n for n in names)


def test_list_publishers_pagination(app_client: TestClient) -> None:
    """Pagination parameters work correctly (limit and offset)."""
    resp = app_client.get(
        "/api/publishers/", params={"api_key": "k", "limit": 1, "offset": 0}
    )
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 1
    assert resp.json()["number_of_total_results"] >= 2


# ---------------------------------------------------------------------------
# 404 Handler
# ---------------------------------------------------------------------------


def test_non_api_404_returns_standard_format(app_client: TestClient) -> None:
    """Non-/api/ 404s return standard error format with HTTP 404."""
    resp = app_client.get("/nonexistent-page")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


def test_api_404_returns_cv_error_envelope(app_client: TestClient) -> None:
    """Requests to non-existent /api/ endpoints are handled by generic_list, returning empty results."""
    # This path now matches /{resources}/ and is handled by generic_list catch-all.
    # When upstream has no data, returns empty list with status_code=1 (not 101 error).
    resp = app_client.get("/api/nonexistent-endpoint-xyz/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1  # Success envelope with empty results
    assert data["results"] == []
    assert data["number_of_total_results"] == 0


# ---------------------------------------------------------------------------
# Generic single-resource catch-all
# ---------------------------------------------------------------------------


def test_generic_single_serves_from_cache(app_client: TestClient) -> None:
    """Seed response cache, verify generic route serves from it."""
    db: Database = app_client.app.state.db
    db.cache_put_single("character", 12345, {"id": 12345, "name": "Batman"})

    resp = app_client.get("/api/character/4005-12345/", params={"api_key": "k"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status_code"] == 1
    assert data["results"]["name"] == "Batman"


def test_generic_single_fetches_and_caches_on_miss(app_client: TestClient) -> None:
    """Fetch from upstream and cache on cache miss."""
    from unittest.mock import AsyncMock
    mock_client = AsyncMock()
    mock_client.get.return_value = {
        "status_code": 1, "error": "OK",
        "results": {"id": 9001, "name": "The Flash"},
    }
    app_client.app.state.cv_client = mock_client

    resp = app_client.get("/api/character/4005-9001/", params={"api_key": "k"})
    assert resp.status_code == 200

    # Verify it's now in cache
    db: Database = app_client.app.state.db
    assert db.cache_get_single("character", 9001) is not None


def test_generic_single_returns_cv_error_when_not_found(app_client: TestClient) -> None:
    """Return CV error envelope when upstream returns None."""
    from unittest.mock import AsyncMock
    mock_client = AsyncMock()
    mock_client.get.return_value = None
    app_client.app.state.cv_client = mock_client

    resp = app_client.get("/api/character/4005-0/", params={"api_key": "k"})
    assert resp.status_code == 200
    assert resp.json()["status_code"] == 101


def test_generic_single_field_list_filters_cached_result(app_client: TestClient) -> None:
    """field_list parameter filters cached result."""
    db: Database = app_client.app.state.db
    db.cache_put_single("character", 55555, {"id": 55555, "name": "Joker", "aliases": "None"})

    resp = app_client.get(
        "/api/character/55555/",
        params={"api_key": "k", "field_list": "id,name"}
    )
    result = resp.json()["results"]
    assert set(result.keys()) == {"id", "name"}


def test_generic_list_fetches_and_caches_on_miss(app_client: TestClient) -> None:
    """Test generic_list fetches from upstream on cache miss and stores results."""

    from unittest.mock import AsyncMock
    from cvproxy.routes.api import _params_hash
    mock_client = AsyncMock()
    mock_client.get.return_value = {
        "status_code": 1, "error": "OK",
        "number_of_total_results": 2, "number_of_page_results": 2,
        "limit": 100, "offset": 0,
        "results": [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
    }
    app_client.app.state.cv_client = mock_client

    resp = app_client.get("/api/characters/", params={"api_key": "k"})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 2

    # Verify results are now in cache
    db: Database = app_client.app.state.db
    ph = _params_hash({"filter": "", "sort": ""})
    assert db.cache_get_list("characters", ph) is not None


def test_generic_list_serves_subset_from_cache(app_client: TestClient) -> None:
    """Seed cache with 3 items; requesting limit=2 should serve from cache."""
    from cvproxy.routes.api import _params_hash
    db: Database = app_client.app.state.db
    items = [{"id": i, "name": f"Item {i}"} for i in range(3)]
    ph = _params_hash({"filter": "name:test", "sort": ""})
    db.cache_put_list("characters", ph, items)

    resp = app_client.get(
        "/api/characters/",
        params={"api_key": "k", "filter": "name:test", "limit": "2", "offset": "0"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 2
    assert data["number_of_total_results"] == 3  # full cached count


def test_generic_list_same_filter_different_limit_hits_cache(app_client: TestClient) -> None:
    """Second request with smaller limit should NOT call upstream."""
    from unittest.mock import AsyncMock
    from cvproxy.routes.api import _params_hash
    db: Database = app_client.app.state.db
    items = [{"id": i} for i in range(10)]
    ph = _params_hash({"filter": "volume:99", "sort": ""})
    db.cache_put_list("story_arcs", ph, items)

    call_count = 0
    async def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return None
    mock_client = AsyncMock()
    mock_client.get = mock_get
    app_client.app.state.cv_client = mock_client

    resp = app_client.get(
        "/api/story_arcs/",
        params={"api_key": "k", "filter": "volume:99", "limit": "5"}
    )
    assert resp.status_code == 200
    assert call_count == 0  # served from cache, no upstream call
    assert len(resp.json()["results"]) == 5


def test_generic_list_applies_field_list_after_cache_hit(app_client: TestClient) -> None:
    """field_list parameter filters cached results."""
    from cvproxy.routes.api import _params_hash
    db: Database = app_client.app.state.db
    items = [{"id": 1, "name": "Foo", "aliases": "Bar"}]
    ph = _params_hash({"filter": "", "sort": ""})
    db.cache_put_list("characters", ph, items)

    resp = app_client.get(
        "/api/characters/",
        params={"api_key": "k", "field_list": "id,name"}
    )
    result = resp.json()["results"][0]
    assert set(result.keys()) == {"id", "name"}


def test_generic_list_sort_applies_to_full_cache_before_slicing(app_client: TestClient) -> None:
    """Sort should apply to full cached list, then slice — not sort a pre-sliced page."""
    from cvproxy.routes.api import _params_hash
    db: Database = app_client.app.state.db
    # Seed 4 items in non-alphabetical order
    items = [
        {"id": 4, "name": "Delta"},
        {"id": 1, "name": "Alpha"},
        {"id": 3, "name": "Charlie"},
        {"id": 2, "name": "Bravo"},
    ]
    ph = _params_hash({"filter": "", "sort": "name:asc"})
    db.cache_put_list("teams", ph, items)

    # Request offset=0, limit=2, sort=name:asc
    # Expected: Alpha, Bravo (first 2 of alphabetically sorted list)
    resp = app_client.get(
        "/api/teams/",
        params={"api_key": "k", "sort": "name:asc", "limit": "2", "offset": "0"}
    )
    assert resp.status_code == 200
    data = resp.json()
    names = [r["name"] for r in data["results"]]
    assert names == ["Alpha", "Bravo"]  # Not Delta, Alpha (which would be wrong)
    assert data["number_of_total_results"] == 4


# ---------------------------------------------------------------------------
# Issue 3: malformed resource ID should not raise 500
# ---------------------------------------------------------------------------


def test_get_volume_with_invalid_id_returns_cv_error(app_client: TestClient) -> None:
    """Malformed resource ID should return CV error, not 500."""
    resp = app_client.get("/api/volume/not-a-number/", params={"api_key": "k"})
    assert resp.status_code == 200
    data = resp.json()
    # Should return a CV-compatible response, not a 500
    assert "status_code" in data
    assert "results" in data


# ---------------------------------------------------------------------------
# Issue 1: field_list must NOT be forwarded upstream (cache poisoning fix)
# ---------------------------------------------------------------------------


def test_generic_single_no_field_list_forwarded_to_upstream(app_client: TestClient) -> None:
    """field_list should NOT be forwarded to upstream so cache has full fields."""
    from unittest.mock import AsyncMock
    mock_client = AsyncMock()
    mock_client.get.return_value = {
        "status_code": 1, "error": "OK",
        "results": {"id": 44444, "name": "Superman", "aliases": "Man of Steel"},
    }
    app_client.app.state.cv_client = mock_client

    # First request with field_list=id — should NOT poison cache
    resp1 = app_client.get(
        "/api/character/4005-44444/",
        params={"api_key": "k", "field_list": "id"}
    )
    assert resp1.json()["results"] == {"id": 44444}  # filtered

    # Second request with no field_list — should get full cached result
    mock_client.get.reset_mock()  # upstream should NOT be called again
    resp2 = app_client.get("/api/character/4005-44444/", params={"api_key": "k"})
    assert resp2.json()["results"] == {"id": 44444, "name": "Superman", "aliases": "Man of Steel"}
    mock_client.get.assert_not_called()  # served from cache


# ---------------------------------------------------------------------------
# XML format middleware
# ---------------------------------------------------------------------------


def test_xml_format_returns_xml(app_client: TestClient) -> None:
    """Requesting format=xml wraps the JSON response in CV-compatible XML."""
    resp = app_client.get("/api/volume/4050-160294/", params={"format": "xml"})
    assert resp.status_code == 200
    assert "application/xml" in resp.headers["content-type"]
    body = resp.text
    assert body.startswith('<?xml version="1.0" encoding="utf-8"?>')
    assert "<response>" in body
    assert "<status_code>1</status_code>" in body
    assert "Absolute Batman" in body


def test_xml_format_search_uses_volume_tag(app_client: TestClient) -> None:
    """Search XML wraps each result in a <volume> tag inside <results>."""
    resp = app_client.get(
        "/api/search/", params={"query": "Absolute Batman", "format": "xml"},
        headers={"X-CV-Cache": "only-if-cached"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<results><volume>" in body
    assert "</volume></results>" in body


def test_xml_format_json_default(app_client: TestClient) -> None:
    """Without format=xml, response is normal JSON."""
    resp = app_client.get("/api/volume/4050-160294/")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    data = resp.json()
    assert data["status_code"] == 1


# ---------------------------------------------------------------------------
# Client IP detection (_client_ip)
# ---------------------------------------------------------------------------

class TestClientIpDetection:
    """Verify that _client_ip correctly resolves the real client IP when behind
    a reverse proxy (Traefik) that injects X-Forwarded-For / X-Real-IP.
    """

    def test_xff_single_ip_used_as_client_ip(self, app_client: TestClient) -> None:
        """X-Forwarded-For with a single IP should be recorded as client_ip."""
        app_client.get(
            "/api/search/",
            params={"query": "Absolute Batman"},
            headers={"X-Forwarded-For": "1.2.3.4"},
        )
        stats: StatsTracker = app_client.app.state.stats
        recent = stats.summary()["recent_requests"]
        assert recent[0]["client_ip"] == "1.2.3.4"
        assert recent[0]["forwarded_for"] == "1.2.3.4"

    def test_xff_chain_uses_leftmost_ip(self, app_client: TestClient) -> None:
        """X-Forwarded-For with a proxy chain uses the leftmost (original) client IP."""
        app_client.get(
            "/api/search/",
            params={"query": "Absolute Batman"},
            headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1, 172.18.0.2"},
        )
        stats: StatsTracker = app_client.app.state.stats
        recent = stats.summary()["recent_requests"]
        assert recent[0]["client_ip"] == "203.0.113.5"
        assert recent[0]["forwarded_for"] == "203.0.113.5, 10.0.0.1, 172.18.0.2"

    def test_x_real_ip_used_when_no_xff(self, app_client: TestClient) -> None:
        """X-Real-IP is used as client_ip when X-Forwarded-For is absent."""
        app_client.get(
            "/api/search/",
            params={"query": "Absolute Batman"},
            headers={"X-Real-IP": "198.51.100.7"},
        )
        stats: StatsTracker = app_client.app.state.stats
        recent = stats.summary()["recent_requests"]
        assert recent[0]["client_ip"] == "198.51.100.7"
        assert recent[0]["forwarded_for"] is None  # no XFF, so forwarded_for column is NULL

    def test_xff_preferred_over_x_real_ip(self, app_client: TestClient) -> None:
        """X-Forwarded-For takes priority over X-Real-IP when both are present."""
        app_client.get(
            "/api/search/",
            params={"query": "Absolute Batman"},
            headers={
                "X-Forwarded-For": "203.0.113.10",
                "X-Real-IP": "198.51.100.99",
            },
        )
        stats: StatsTracker = app_client.app.state.stats
        recent = stats.summary()["recent_requests"]
        assert recent[0]["client_ip"] == "203.0.113.10"

    def test_no_proxy_headers_falls_back_to_direct_connection(
        self, app_client: TestClient
    ) -> None:
        """Without proxy headers the direct connection IP is used (testclient = testclient)."""
        app_client.get("/api/search/", params={"query": "Absolute Batman"})
        stats: StatsTracker = app_client.app.state.stats
        recent = stats.summary()["recent_requests"]
        # TestClient connects as "testclient" (Starlette's fake host)
        assert recent[0]["client_ip"] is not None
        assert recent[0]["forwarded_for"] is None


# ---------------------------------------------------------------------------
# last_accessed tracking in detail routes (Task 4)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_client_eviction_enabled(tmp_path: Path) -> Generator[TestClient]:
    """Test client with evict_older_than_years=5 to enable last_accessed tracking."""
    db_path = tmp_path / "test_evict.db"
    _create_test_db(db_path)

    settings = Settings(
        cv_api_key="test_key",
        db_path=db_path,
        image_cache_dir=tmp_path / "images",
        stats_db_path=tmp_path / "stats.db",
        sync_enabled=False,
        evict_older_than_years=5,
    )
    override_settings(settings)

    app = create_app()
    with TestClient(app) as client:
        yield client


def test_volume_detail_touches_last_accessed_when_eviction_enabled(
    app_client_eviction_enabled: TestClient,
) -> None:
    """Volume cache hit sets last_accessed when evict_older_than_years > 0."""
    resp = app_client_eviction_enabled.get("/api/volume/4050-160294/")
    assert resp.status_code == 200
    assert resp.json()["status_code"] == 1

    db: Database = app_client_eviction_enabled.app.state.db
    row = db.conn.execute(
        "SELECT last_accessed FROM cv_volume WHERE id = 160294"
    ).fetchone()
    assert row is not None
    assert row[0] is not None


def test_volume_detail_no_touch_when_eviction_disabled(app_client: TestClient) -> None:
    """Volume cache hit does NOT set last_accessed when evict_older_than_years=0."""
    resp = app_client.get("/api/volume/4050-160294/")
    assert resp.status_code == 200

    db: Database = app_client.app.state.db
    row = db.conn.execute(
        "SELECT last_accessed FROM cv_volume WHERE id = 160294"
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_issue_detail_touches_last_accessed_for_old_issue(
    tmp_path: Path,
) -> None:
    """Issue cache hit sets last_accessed for an old issue when eviction is enabled."""
    db_path = tmp_path / "test_old_issue.db"
    db = Database(db_path)
    db.connect()
    db.upsert_publisher({"id": 1, "name": "DC Comics"})
    db.upsert_volume({"id": 160294, "name": "Absolute Batman", "publisher": {"id": 1}})
    db.upsert_issue({
        "id": 9999001, "volume": {"id": 160294}, "name": "Old Issue",
        "issue_number": "1", "cover_date": "1990-01-01",
        "character_credits": [], "person_credits": [], "team_credits": [],
        "location_credits": [], "story_arc_credits": [], "associated_images": [],
    })
    db.close()

    settings = Settings(
        cv_api_key="test_key",
        db_path=db_path,
        image_cache_dir=tmp_path / "images",
        stats_db_path=tmp_path / "stats.db",
        sync_enabled=False,
        evict_older_than_years=5,
    )
    override_settings(settings)

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/issue/4000-9999001/")
        assert resp.status_code == 200
        assert resp.json()["status_code"] == 1

        db: Database = client.app.state.db
        row = db.conn.execute(
            "SELECT last_accessed FROM cv_issue WHERE id = 9999001"
        ).fetchone()
        assert row is not None
        assert row[0] is not None


def test_issue_detail_no_touch_for_recent_issue(
    app_client_eviction_enabled: TestClient,
) -> None:
    """Recent issue (within cutoff) does NOT get last_accessed set."""
    # Issue 1073108 has cover_date='2024-12-01', well within 5-year cutoff from 2026
    resp = app_client_eviction_enabled.get("/api/issue/4000-1073108/")
    assert resp.status_code == 200

    db: Database = app_client_eviction_enabled.app.state.db
    row = db.conn.execute(
        "SELECT last_accessed FROM cv_issue WHERE id = 1073108"
    ).fetchone()
    assert row is not None
    assert row[0] is None


# ---------------------------------------------------------------------------
# Admin /admin/evict endpoint (Task 6)
# ---------------------------------------------------------------------------


def _poll_job(client: TestClient, job_id: str, max_polls: int = 10) -> dict:
    """Poll /admin/jobs/{job_id} until done and return the result."""
    import time

    for _ in range(max_polls):
        r = client.get(f"/admin/jobs/{job_id}")
        assert r.status_code == 200
        job = r.json()
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    return job  # type: ignore[return-value]


def test_admin_evict_dry_run_returns_job_id(app_client: TestClient) -> None:
    """POST /admin/evict?dry_run=true returns status=started with a job_id."""
    resp = app_client.post("/admin/evict", params={"dry_run": "true"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "started"
    assert "job_id" in data
    assert data["dry_run"] is True


def test_admin_evict_disabled_returns_zero_counts(app_client: TestClient) -> None:
    """With evict_older_than_years=0, dry_run returns zeros and an explanatory note."""
    # app_client uses default settings: evict_older_than_years=0, response_cache_ttl_days=30
    # Override to fully disable everything
    from cvproxy.config import Settings

    settings = Settings(
        cv_api_key="test_key",
        db_path=app_client.app.state.db._path,
        image_cache_dir=app_client.app.state.image_cache._cache_dir,
        stats_db_path=app_client.app.state.stats._path,
        sync_enabled=False,
        evict_older_than_years=0,
        response_cache_ttl_days=0,
    )
    override_settings(settings)

    resp = app_client.post("/admin/evict", params={"dry_run": "true"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    job = _poll_job(app_client, job_id)
    assert job["status"] == "done"
    result = job["result"]
    assert result["issues"] == 0
    assert result["volumes"] == 0


def test_admin_evict_dry_run_does_not_delete(
    tmp_path: Path,
) -> None:
    """dry_run=true counts candidates but does NOT delete any rows."""
    db_path = tmp_path / "evict_dryrun.db"
    db = Database(db_path)
    db.connect()
    db.upsert_publisher({"id": 1, "name": "DC Comics"})
    db.upsert_volume({"id": 160294, "name": "Old Volume", "publisher": {"id": 1}})
    db.upsert_issue({
        "id": 8888001, "volume": {"id": 160294}, "name": "Old Issue",
        "issue_number": "1", "cover_date": "1990-01-01",
        "character_credits": [], "person_credits": [], "team_credits": [],
        "location_credits": [], "story_arc_credits": [], "associated_images": [],
    })
    db.close()

    settings = Settings(
        cv_api_key="test_key",
        db_path=db_path,
        image_cache_dir=tmp_path / "images",
        stats_db_path=tmp_path / "stats.db",
        sync_enabled=False,
        evict_older_than_years=5,
        response_cache_ttl_days=0,
    )
    override_settings(settings)

    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/admin/evict", params={"dry_run": "true"})
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        job = _poll_job(client, job_id)
        assert job["status"] == "done"
        # Row must still exist — dry_run must not delete
        db: Database = client.app.state.db
        row = db.conn.execute(
            "SELECT id FROM cv_issue WHERE id = 8888001"
        ).fetchone()
        assert row is not None
