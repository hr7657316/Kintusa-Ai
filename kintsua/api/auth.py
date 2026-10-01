"""Sign in with GitHub through the App's OAuth flow.

``GET /login`` sends the browser to GitHub with a signed, short-lived ``state`` cookie;
``GET /auth/callback`` exchanges the code for a user token, reads the user and the
installations that user can see (``GET /user/installations``) and sets the session cookie
(HttpOnly, SameSite=Lax, Secure when ``base_url`` is https). ``POST /logout`` clears it.

When OAuth is not configured ``/login`` explains how to configure it. In ``ENV=dev`` with
``DEV_LOGIN_INSTALLATION_ID`` set, ``POST /auth/dev`` mints a session for that installation
so local work is possible without an OAuth app.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired

from kintsua.api.deps import (
    OAUTH_SALT,
    OAUTH_STATE_COOKIE,
    clear_session_cookie,
    cookie_secure,
    render,
    serializer,
    set_session_cookie,
)
from kintsua.api.schemas import SessionUser
from kintsua.core.config import Settings, get_settings
from kintsua.core.util import new_token

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

OAUTH_STATE_MAX_AGE_S = 600
HTTP_TIMEOUT_S = 20.0
MAX_INSTALLATION_PAGES = 10


class OAuthError(Exception):
    """GitHub refused or returned something we could not use."""


def oauth_configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool(s.github_oauth_client_id and s.github_oauth_client_secret)


def dev_login_installation_id(settings: Settings | None = None) -> int | None:
    s = settings or get_settings()
    if s.env != "dev":
        return None
    raw = os.environ.get("DEV_LOGIN_INSTALLATION_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("DEV_LOGIN_INSTALLATION_ID=%r is not an integer", raw)
        return None


def github_web_url(settings: Settings | None = None) -> str:
    """``https://github.com`` for github.com; the GHES host for ``.../api/v3`` API URLs."""
    s = settings or get_settings()
    api = s.github_api_url.rstrip("/")
    if api == "https://api.github.com":
        return "https://github.com"
    if api.endswith("/api/v3"):
        return api[: -len("/api/v3")]
    return api


def safe_next(value: str | None, default: str = "/settings") -> str:
    """Only same-site paths are allowed as a post-login redirect (no open redirects)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    return value[:500]


def redirect_uri(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return f"{s.base_url.rstrip('/')}/auth/callback"


# --------------------------------------------------------------------------------------
# GitHub calls (plain httpx so tests can mock them with respx)
# --------------------------------------------------------------------------------------


def exchange_code(settings: Settings, code: str) -> str:
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            r = client.post(
                f"{github_web_url(settings)}/login/oauth/access_token",
                data={
                    "client_id": settings.github_oauth_client_id,
                    "client_secret": settings.github_oauth_client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri(settings),
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as e:
        raise OAuthError(f"could not reach GitHub: {type(e).__name__}") from e
    if r.status_code != 200:
        raise OAuthError(f"token exchange returned HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise OAuthError("token exchange returned a non-JSON body")
    if not isinstance(data, dict) or data.get("error"):
        desc = data.get("error_description") if isinstance(data, dict) else None
        raise OAuthError(str(desc or (data.get("error") if isinstance(data, dict) else data)))
    token = data.get("access_token")
    if not token:
        raise OAuthError("no access_token in GitHub's response")
    return str(token)


def _api_get(settings: Settings, token: str, path: str, params: dict[str, Any] | None = None):
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            r = client.get(
                f"{settings.github_api_url.rstrip('/')}{path}",
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": settings.user_agent,
                },
            )
    except httpx.HTTPError as e:
        raise OAuthError(f"could not reach GitHub: {type(e).__name__}") from e
    if r.status_code != 200:
        raise OAuthError(f"GET {path} returned HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise OAuthError(f"GET {path} returned a non-JSON body")


def fetch_user(settings: Settings, token: str) -> dict[str, Any]:
    data = _api_get(settings, token, "/user")
    if not isinstance(data, dict) or not data.get("login"):
        raise OAuthError("GitHub did not return a user")
    return data


def fetch_installation_ids(settings: Settings, token: str) -> list[int]:
    ids: list[int] = []
    for page in range(1, MAX_INSTALLATION_PAGES + 1):
        data = _api_get(
            settings, token, "/user/installations", params={"per_page": 100, "page": page}
        )
        rows = data.get("installations") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            break
        for inst in rows:
            if isinstance(inst, dict) and inst.get("id") is not None:
                try:
                    ids.append(int(inst["id"]))
                except (TypeError, ValueError):
                    continue
        if len(rows) < 100:
            break
    return sorted(set(ids))


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------


@router.get("/login")
def login(request: Request, next: str | None = None) -> Response:
    s = get_settings()
    target = safe_next(next)
    if not oauth_configured(s):
        return render(
            request,
            "login.html",
            configured=False,
            dev_login=dev_login_installation_id(s) is not None,
            next=target,
            redirect_uri=redirect_uri(s),
        )
    state = serializer(OAUTH_SALT).dumps({"n": new_token(16), "next": target})
    params = {
        "client_id": s.github_oauth_client_id,
        "redirect_uri": redirect_uri(s),
        "state": state,
    }
    resp = RedirectResponse(
        url=f"{github_web_url(s)}/login/oauth/authorize?{urlencode(params)}", status_code=302
    )
    resp.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=OAUTH_STATE_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
        path="/",
    )
    return resp


@router.get("/auth/callback")
def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> Response:
    s = get_settings()
    if not oauth_configured(s):
        return render(
            request,
            "error.html",
            status_code=404,
            title="Sign-in is not configured",
            message="GITHUB_OAUTH_CLIENT_ID / GITHUB_OAUTH_CLIENT_SECRET are not set.",
        )
    if error:
        return render(
            request,
            "error.html",
            status_code=400,
            title="GitHub refused the sign-in",
            message=error_description or error,
        )
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE) or ""
    if not code or not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
        return render(
            request,
            "error.html",
            status_code=400,
            title="Sign-in state mismatch",
            message="The sign-in link expired or was opened in another browser. Try again.",
        )
    try:
        data = serializer(OAUTH_SALT).loads(state, max_age=OAUTH_STATE_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return render(
            request,
            "error.html",
            status_code=400,
            title="Sign-in state expired",
            message="Start again from /login.",
        )
    try:
        token = exchange_code(s, code)
        gh_user = fetch_user(s, token)
        installation_ids = fetch_installation_ids(s, token)
    except OAuthError as e:
        log.warning("GitHub OAuth failed: %s", e)
        return render(
            request,
            "error.html",
            status_code=502,
            title="GitHub sign-in failed",
            message=str(e),
        )
    user = SessionUser(
        login=str(gh_user["login"]),
        id=int(gh_user.get("id") or 0),
        installation_ids=installation_ids,
    )
    resp = RedirectResponse(url=safe_next(data.get("next")), status_code=303)
    set_session_cookie(resp, user)
    resp.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    log.info("user %s signed in with %d installation(s)", user.login, len(installation_ids))
    return resp


@router.post("/logout")
def logout() -> Response:
    resp = RedirectResponse(url="/", status_code=303)
    clear_session_cookie(resp)
    return resp


@router.api_route("/auth/dev", methods=["GET", "POST"])
def dev_login(request: Request, next: str | None = None) -> Response:
    """Local development only: ``ENV=dev`` plus ``DEV_LOGIN_INSTALLATION_ID``."""
    inst = dev_login_installation_id()
    if inst is None:
        return render(
            request,
            "error.html",
            status_code=404,
            title="Not found",
            message="Dev login is only available with ENV=dev and DEV_LOGIN_INSTALLATION_ID set.",
        )
    user = SessionUser(login=os.environ.get("DEV_LOGIN_USER", "dev"), id=0, installation_ids=[inst])
    resp = RedirectResponse(url=safe_next(next), status_code=303)
    set_session_cookie(resp, user)
    return resp


__all__ = [
    "OAuthError",
    "dev_login_installation_id",
    "exchange_code",
    "fetch_installation_ids",
    "fetch_user",
    "github_web_url",
    "oauth_configured",
    "redirect_uri",
    "router",
    "safe_next",
]
