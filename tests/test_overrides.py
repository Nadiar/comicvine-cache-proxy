"""Settings overrides edited on the dashboard and stored in jobs.db."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from cvproxy.app import create_app
from cvproxy.config import Settings, get_settings, override_settings
from cvproxy.jobs import JobRunner
from cvproxy.overrides import InvalidOverride, OverrideStore, apply_overrides
from cvproxy.scheduler import build_job_specs, create_scheduler


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(
        cv_api_key="test_key", db_path=tmp_path / "test.db",
        image_cache_dir=tmp_path / "images", stats_db_path=tmp_path / "stats.db",
        rate_state_path=tmp_path / "rate_state.json", sync_enabled=False, **kw,
    )


# -- apply_overrides ----------------------------------------------------------


def test_apply_overrides_replaces_editable_values_and_keeps_the_rest(tmp_path: Path) -> None:
    base = _settings(tmp_path)
    result = apply_overrides(base, {"backfill_min_year": 2015, "search_max_pages": 2})
    assert result.backfill_min_year == 2015
    assert result.search_max_pages == 2
    assert result.cv_api_key == "test_key"
    assert base.backfill_min_year == 2010


@pytest.mark.parametrize("overrides", [
    {"cv_api_key": "other"},
    {"db_path": "/tmp/x.db"},
    {"search_max_pages": 0},
    {"backup_keep": -1},
    {"backfill_min_year": "2015"},
    {"backfill_min_year": True},
    {"backfill_min_year": 2.5},
])
def test_apply_overrides_rejects_bad_fields_and_values(tmp_path: Path,
                                                      overrides: dict[str, Any]) -> None:
    with pytest.raises(InvalidOverride):
        apply_overrides(_settings(tmp_path), overrides)


# -- OverrideStore ------------------------------------------------------------


def test_override_store_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    store = OverrideStore(path)
    store.save({"backfill_min_year": 2015, "backup_keep": 5})
    store.close()
    store = OverrideStore(path)
    assert store.load() == {"backfill_min_year": 2015, "backup_keep": 5}
    store.save({"backup_keep": 5})
    assert store.load() == {"backup_keep": 5}
    store.close()


# -- job catalog and schedule follow the current settings ----------------------


def test_catalog_description_follows_the_current_settings(tmp_path: Path) -> None:
    current = {"settings": _settings(tmp_path)}
    runner = JobRunner(settings_provider=lambda: current["settings"])
    runner.register(build_job_specs(current["settings"], MagicMock(), MagicMock(), MagicMock()))
    current["settings"] = apply_overrides(current["settings"], {"backfill_min_year": 2018})
    desc = {j["id"]: j["description"] for j in runner.catalog()}
    assert "2018 or later (BACKFILL_MIN_YEAR)" in desc["issue_detail_backfill"]


def test_sync_schedule_adds_and_removes_jobs_a_setting_turns_on_or_off(tmp_path: Path) -> None:
    current = {"settings": _settings(tmp_path, backup_keep=0)}
    runner = JobRunner(settings_provider=lambda: current["settings"])
    scheduler = create_scheduler(current["settings"], MagicMock(), MagicMock(), MagicMock(),
                                 runner)
    assert scheduler.get_job("database_backup") is None
    current["settings"] = apply_overrides(current["settings"], {"backup_keep": 3})
    runner.sync_schedule()
    assert scheduler.get_job("database_backup") is not None
    current["settings"] = apply_overrides(current["settings"], {"backup_keep": 0})
    runner.sync_schedule()
    assert scheduler.get_job("database_backup") is None


# -- /admin/settings ----------------------------------------------------------


def _client(settings: Settings) -> Generator[TestClient]:
    override_settings(settings)
    with TestClient(create_app()) as client:
        client.app.state.cv_client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        )
        yield client


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient]:
    yield from _client(_settings(tmp_path, search_max_pages=4))


def _by_name(resp: httpx.Response) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in resp.json()}


def test_settings_endpoint_lists_only_editable_settings_with_their_source(
    client: TestClient,
) -> None:
    resp = client.get("/admin/settings")
    assert resp.status_code == 200
    settings = _by_name(resp)
    assert "cv_api_key" not in settings and "db_path" not in settings
    assert "test_key" not in resp.text
    assert settings["backfill_min_year"] == {
        **settings["backfill_min_year"], "value": 2010, "default": 2010, "source": "default",
        "env": "BACKFILL_MIN_YEAR",
    }
    assert settings["search_max_pages"]["source"] == "env"
    assert settings["search_max_pages"]["value"] == 4


def test_saving_a_setting_applies_it_and_clearing_returns_to_the_base_value(
    client: TestClient,
) -> None:
    resp = client.put("/admin/settings", json={"search_max_pages": 2})
    assert resp.status_code == 200
    assert _by_name(resp)["search_max_pages"]["source"] == "override"
    assert get_settings().search_max_pages == 2
    resp = client.put("/admin/settings", json={"search_max_pages": None})
    assert _by_name(resp)["search_max_pages"] == {
        **_by_name(resp)["search_max_pages"], "value": 4, "source": "env",
    }
    assert get_settings().search_max_pages == 4


@pytest.mark.parametrize("body", [{"cv_api_key": "x"}, {"search_max_pages": 0}])
def test_saving_a_bad_setting_is_rejected_and_changes_nothing(client: TestClient,
                                                              body: dict[str, Any]) -> None:
    resp = client.put("/admin/settings", json={"backfill_min_year": 2012, **body})
    assert resp.status_code == 422
    assert get_settings().backfill_min_year == 2010
    assert _by_name(client.get("/admin/settings"))["backfill_min_year"]["source"] == "default"


def test_stored_overrides_are_applied_at_startup_and_stale_ones_ignored(tmp_path: Path) -> None:
    store = OverrideStore(tmp_path / "jobs.db")
    store.save({"backfill_min_year": 2016, "no_longer_a_setting": 1})
    store.close()
    for _client_ in _client(_settings(tmp_path)):
        assert get_settings().backfill_min_year == 2016
        assert _by_name(_client_.get("/admin/settings"))["backfill_min_year"]["source"] == \
            "override"
