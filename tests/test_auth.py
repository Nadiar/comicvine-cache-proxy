"""Login form in front of the dashboard, /admin and the API docs."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cvproxy.app import create_app
from cvproxy.auth import SESSION_COOKIE, session_token, valid_session
from cvproxy.config import Settings, override_settings


def _client(tmp_path: Path, **kw: Any) -> Generator[TestClient]:
    override_settings(Settings(
        cv_api_key="test_key", db_path=tmp_path / "test.db",
        image_cache_dir=tmp_path / "images", stats_db_path=tmp_path / "stats.db",
        rate_state_path=tmp_path / "rate_state.json", sync_enabled=False, **kw,
    ))
    with TestClient(create_app(), follow_redirects=False) as client:
        client.app.state.cv_client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        )
        yield client


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient]:
    yield from _client(tmp_path, dashboard_username="admin", dashboard_password="s3cret")


@pytest.fixture
def open_client(tmp_path: Path) -> Generator[TestClient]:
    yield from _client(tmp_path)


def _login(client: TestClient, password: str = "s3cret", nxt: str = "/dashboard") -> httpx.Response:
    return client.post("/login", data={"username": "admin", "password": password, "next": nxt})


def test_session_token_is_valid_until_it_expires_and_only_for_the_same_credentials() -> None:
    token = session_token("admin", "s3cret", expires=2_000)
    assert valid_session(token, "admin", "s3cret", now=1_999)
    assert not valid_session(token, "admin", "s3cret", now=2_001)
    assert not valid_session(token, "admin", "changed", now=1_999)
    assert not valid_session(token.replace(".", ".0", 1), "admin", "s3cret", now=1_999)
    assert not valid_session("garbage", "admin", "s3cret", now=1_999)


def test_pages_redirect_to_the_login_form_and_api_calls_get_401(client: TestClient) -> None:
    resp = client.get("/dashboard")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/dashboard"
    assert client.get("/docs").status_code == 303
    assert client.get("/admin/settings").status_code == 401
    assert client.get("/dashboard/data").status_code == 401
    assert client.get("/openapi.json").status_code == 401


def test_health_and_comicvine_api_stay_open(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/api/volume/4050-796/").status_code != 401


def test_wrong_password_shows_the_form_again(client: TestClient) -> None:
    resp = _login(client, password="nope")
    assert resp.status_code == 401
    assert "Wrong username or password" in resp.text
    assert SESSION_COOKIE not in resp.cookies


def test_login_sets_a_session_that_opens_the_dashboard_and_logout_ends_it(
    client: TestClient,
) -> None:
    resp = _login(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert "httponly" in resp.headers["set-cookie"].lower()
    assert client.get("/dashboard").status_code == 200
    assert client.get("/admin/settings").status_code == 200
    assert client.post("/logout").status_code == 303
    assert client.get("/admin/settings").status_code == 401


@pytest.mark.parametrize("nxt", ["https://evil.example/", "//evil.example/", "dashboard"])
def test_login_only_redirects_within_the_site(client: TestClient, nxt: str) -> None:
    assert _login(client, nxt=nxt).headers["location"] == "/dashboard"


def test_tampered_cookie_is_rejected(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, session_token("admin", "other", expires=4_000_000_000))
    assert client.get("/admin/settings").status_code == 401


def test_without_a_configured_password_nothing_is_protected(open_client: TestClient) -> None:
    assert open_client.get("/dashboard").status_code == 200
    assert open_client.get("/admin/settings").status_code == 200
