"""Tests for the pending-fetch queue worker."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from cvproxy.cv_client import NOT_FOUND, RATE_LIMITED
from cvproxy.db import Database
from cvproxy.pending import MAX_ATTEMPTS, process_pending


def _client(**calls: Any) -> MagicMock:
    client = MagicMock()
    client.retry_after.return_value = 0.0
    client.hourly_calls.return_value = 0
    client.hourly_limit = 180
    for name, value in calls.items():
        setattr(client, name, AsyncMock(return_value=value))
    return client


def _issue_detail(iid: int) -> dict[str, Any]:
    return {
        "status_code": 1, "error": "OK",
        "results": {
            "id": iid, "volume": {"id": 160294}, "issue_number": "9", "name": "Fetched",
            "person_credits": [{"id": 1, "name": "Writer"}],
        },
    }


async def test_pending_issue_is_fetched_cached_and_removed(tmp_db: Database) -> None:
    tmp_db.pending_add("issue", "5555", {"id": 5555})
    client = _client(get_issue=_issue_detail(5555))

    result = await process_pending(client, tmp_db)

    assert result == {"fetched": 1, "skipped": 0, "failed": 0}
    client.get_issue.assert_called_once_with(5555)
    assert tmp_db.issue_needs_detail(5555) is False
    assert tmp_db.pending_list(limit=10) == []


async def test_pending_items_wait_while_their_endpoint_is_limited(tmp_db: Database) -> None:
    tmp_db.pending_add("issue", "5556", {"id": 5556})
    tmp_db.pending_add("publisher", "77", {"id": 77})
    client = _client(
        get_issue=_issue_detail(5556),
        get_publisher={"status_code": 1, "results": {"id": 77, "name": "Pub"}},
    )
    client.retry_after.side_effect = lambda endpoint: 300.0 if endpoint == "issue/" else 0.0

    result = await process_pending(client, tmp_db)

    assert result == {"fetched": 1, "skipped": 1, "failed": 0}
    client.get_issue.assert_not_called()
    assert [item["kind"] for item in tmp_db.pending_list(limit=10)] == ["issue"]


async def test_pending_rate_limited_item_stays_queued(tmp_db: Database) -> None:
    tmp_db.pending_add("issue", "5557", {"id": 5557})
    client = _client(get_issue=RATE_LIMITED)

    await process_pending(client, tmp_db)

    items = tmp_db.pending_list(limit=10)
    assert len(items) == 1
    assert items[0]["attempts"] == 0  # a refusal isn't the item's fault


async def test_pending_not_found_is_removed(tmp_db: Database) -> None:
    tmp_db.pending_add("volume", "424242", {"id": 424242})
    client = _client(get_volume=NOT_FOUND)

    await process_pending(client, tmp_db)

    assert tmp_db.pending_list(limit=10) == []


async def test_pending_failures_are_retried_then_dropped(tmp_db: Database) -> None:
    tmp_db.pending_add("issue", "5558", {"id": 5558})
    client = _client(get_issue=None)

    for _ in range(MAX_ATTEMPTS - 1):
        await process_pending(client, tmp_db)
    assert tmp_db.pending_list(limit=10)[0]["attempts"] == MAX_ATTEMPTS - 1

    await process_pending(client, tmp_db)
    assert tmp_db.pending_list(limit=10) == []


async def test_pending_search_fills_search_cache(tmp_db: Database) -> None:
    from cvproxy.routes.api import _search_cache_key

    params_hash = _search_cache_key("Absolute Flash 13", "issue")
    tmp_db.pending_add("search", params_hash, {"query": "Absolute Flash 13", "resources": "issue"})
    page = {
        "status_code": 1, "error": "OK", "number_of_total_results": 1,
        "results": [{"id": 1, "name": "Absolute Flash #13", "resource_type": "issue"}],
    }
    client = _client(search=page)

    await process_pending(client, tmp_db)

    cached = tmp_db.cache_get_search(params_hash)
    assert cached is not None
    assert [r["id"] for r in cached["results"]] == [1]
    assert tmp_db.pending_list(limit=10) == []


async def test_pending_volume_issue_list_is_refreshed(tmp_db: Database) -> None:
    tmp_db.pending_add("volume_issues", "160294", {"volume_id": 160294})
    page = {
        "status_code": 1, "error": "OK", "number_of_total_results": 1,
        "results": [{"id": 1073108, "volume": {"id": 160294}, "issue_number": "1"}],
    }
    client = _client(get=page)

    await process_pending(client, tmp_db)

    assert tmp_db.volume_issues_complete(160294, sync_ttl_hours=24) is True
    assert tmp_db.pending_list(limit=10) == []


async def test_pending_issues_filter_negative_caches_empty_volume_lookup(
    tmp_db: Database,
) -> None:
    tmp_db.pending_add(
        "issues_filter", "neg-key",
        {"filter": "volume:777|778,issue_number:5", "negative_key": "neg-key"},
    )
    client = _client(get={"status_code": 1, "error": "OK", "number_of_total_results": 0,
                           "results": []})

    await process_pending(client, tmp_db)

    assert tmp_db.negative_cache_hit("issues", "neg-key", ttl_hours=24) is True
    assert tmp_db.pending_list(limit=10) == []


async def test_pending_leaves_live_reserve_on_each_endpoint(tmp_db: Database) -> None:
    """Draining the queue must not use up the window live requests need."""
    from cvproxy.pending import LIVE_RESERVE

    tmp_db.pending_add("issue", "5559", {"id": 5559})
    client = _client(get_issue=_issue_detail(5559))
    client.hourly_calls.return_value = client.hourly_limit - LIVE_RESERVE

    result = await process_pending(client, tmp_db)

    assert result == {"fetched": 0, "skipped": 1, "failed": 0}
    client.get_issue.assert_not_called()


async def test_pending_volume_search_fetches_one_page(tmp_db: Database) -> None:
    """Nobody is waiting on a queued search; page 1 warms the cache."""
    tmp_db.pending_add("search", "k", {"query": "Batman", "resources": "volume"})
    page = {
        "status_code": 1, "error": "OK", "number_of_total_results": 900,
        "results": [{"id": i, "name": f"Batman {i}"} for i in range(100)],
    }
    client = _client(search=page)

    await process_pending(client, tmp_db)

    assert [c.kwargs["page"] for c in client.search.call_args_list] == [1]
