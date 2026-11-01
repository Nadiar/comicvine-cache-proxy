"""Login form in front of the dashboard, /admin and the API docs.

The credentials come from DASHBOARD_USERNAME / DASHBOARD_PASSWORD; with no
password set, login is off.  A session is a signed cookie holding its expiry;
the signing key is derived from the credentials, so changing the password ends
every session.  /api/* (ComicVine clients) and /health stay open.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import time
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from cvproxy.config import Settings, get_settings
from cvproxy.dashboard import LOGIN_HTML

SESSION_COOKIE = "cvproxy_session"
SESSION_SECONDS = 30 * 86400
DEFAULT_NEXT = "/dashboard"
_PROTECTED = ("/dashboard", "/admin", "/docs", "/redoc", "/openapi.json")
#: Opened by people in a browser: redirect them to the form instead of a 401
_PAGES = ("/dashboard", "/docs", "/redoc")


def login_enabled(settings: Settings) -> bool:
    return bool(settings.dashboard_password)


def _key(username: str, password: str) -> bytes:
    return hashlib.sha256(f"cvproxy-session\0{username}\0{password}".encode()).digest()


def _signature(expires: str, username: str, password: str) -> str:
    return hmac.new(_key(username, password), expires.encode(), hashlib.sha256).hexdigest()


def session_token(username: str, password: str, *, expires: int) -> str:
    return f"{expires}.{_signature(str(expires), username, password)}"


def valid_session(token: str, username: str, password: str, *, now: float) -> bool:
    expires, _, signature = token.partition(".")
    if not expires.isdigit() or int(expires) < now:
        return False
    return hmac.compare_digest(signature, _signature(expires, username, password))


def credentials_match(settings: Settings, username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(username.encode(), settings.dashboard_username.encode())
    password_ok = hmac.compare_digest(password.encode(), settings.dashboard_password.encode())
    return user_ok and password_ok


def safe_next(target: str) -> str:
    """Only redirect back to a path on this site, never to another host."""
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return DEFAULT_NEXT


def _is_protected(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _PROTECTED)


class AuthMiddleware:
    """Turns away requests for protected paths that carry no valid session."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_protected(scope["path"]):
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        request = Request(scope)
        token = request.cookies.get(SESSION_COOKIE, "")
        if not login_enabled(settings) or valid_session(
            token, settings.dashboard_username, settings.dashboard_password,
            now=time.time(),
        ):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if request.method == "GET" and path in _PAGES:
            response: JSONResponse | RedirectResponse = RedirectResponse(
                f"/login?next={quote(path)}", status_code=303
            )
        else:
            response = JSONResponse({"detail": "Login required"}, status_code=401)
        await response(scope, receive, send)


def login_page(target: str, error: str = "") -> str:
    alert = (f'<div class="alert alert-danger py-2 small">{html.escape(error)}</div>'
             if error else "")
    return LOGIN_HTML.replace("<!--ALERT-->", alert).replace("<!--TARGET-->", html.escape(target))
