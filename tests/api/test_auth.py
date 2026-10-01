"""GitHub OAuth sign-in (GitHub mocked with respx), logout and the dev login."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from kintsua.api.deps import (
    OAUTH_SALT,
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    load_session,
    serializer,
)


@pytest.fixture
def oauth(settings, monkeypatch):
    monkeypatch.setattr(settings, "github_oauth_client_id", "Iv1.abc123")
    monkeypatch.setattr(settings, "github_oauth_client_secret", "oauth-secret")
    return settings


def test_login_explains_configuration_when_oauth_is_missing(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "github_oauth_client_id", None)
    monkeypatch.setattr(settings, "github_oauth_client_secret", None)
    r = client.get("/login")
    assert r.status_code == 200
    assert "GITHUB_OAUTH_CLIENT_ID" in r.text and "/setup/github" in r.text
    assert f"{settings.base_url}/auth/callback" in r.text
    assert "Dev login" not in r.text
    assert client.get("/auth/callback?code=x&state=y").status_code == 404


def test_login_redirects_to_github_with_a_signed_state(client, oauth):
    r = client.get("/login?next=/settings/acme/support-agent", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://github.com/login/oauth/authorize?")
    qs = parse_qs(urlsplit(loc).query)
    assert qs["client_id"] == ["Iv1.abc123"]
    assert qs["redirect_uri"] == [f"{oauth.base_url}/auth/callback"]
    state = qs["state"][0]
    assert client.cookies.get(OAUTH_STATE_COOKIE) == state
    assert serializer(OAUTH_SALT).loads(state)["next"] == "/settings/acme/support-agent"
    assert "HttpOnly" in r.headers["set-cookie"]

    # open redirects are refused
    r = client.get("/login?next=https://evil.example/phish", follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    assert serializer(OAUTH_SALT).loads(state)["next"] == "/settings"
    r = client.get("/login?next=//evil.example", follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    assert serializer(OAUTH_SALT).loads(state)["next"] == "/settings"


@respx.mock
def test_callback_exchanges_the_code_and_sets_the_session(client, factory, oauth):
    factory.org()  # installation 12345678
    token_route = respx.post("https://github.com/login/oauth/access_token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "gho_user_token", "token_type": "bearer"}
        )
    )
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat", "id": 1})
    )
    inst_route = respx.get("https://api.github.com/user/installations").mock(
        return_value=httpx.Response(
            200, json={"total_count": 2, "installations": [{"id": 12345678}, {"id": 99}]}
        )
    )
    r = client.get("/login?next=/settings", follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]

    r = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/settings"
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=lax" in r.headers[
        "set-cookie"
    ].lower().replace("samesite=lax", "SameSite=lax")
    assert b"code=abc" in token_route.calls.last.request.content
    assert b"client_secret=oauth-secret" in token_route.calls.last.request.content
    assert inst_route.calls.last.request.headers["Authorization"] == "Bearer gho_user_token"
    user = load_session(client.cookies.get(SESSION_COOKIE))
    assert user is not None and user.login == "octocat" and user.id == 1
    assert user.installation_ids == [99, 12345678]

    # the session now opens the settings page
    r = client.get("/settings")
    assert r.status_code == 200 and "acme" in r.text and "Sign out (octocat)" in r.text

    # logout clears the cookie
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert 'kintsua_session=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    assert client.get("/settings", follow_redirects=False).status_code == 302


@respx.mock
def test_callback_rejects_bad_state_and_github_errors(client, oauth):
    r = client.get("/auth/callback?code=abc&state=forged")
    assert r.status_code == 400 and "state" in r.text.lower()
    r = client.get("/auth/callback?error=access_denied&error_description=The+user+denied")
    assert r.status_code == 400 and "denied" in r.text
    r = client.get("/login", follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    respx.post("https://github.com/login/oauth/access_token").mock(
        return_value=httpx.Response(
            200, json={"error": "bad_verification_code", "error_description": "The code is wrong"}
        )
    )
    r = client.get(f"/auth/callback?code=abc&state={state}")
    assert r.status_code == 502 and "The code is wrong" in r.text
    assert client.cookies.get(SESSION_COOKIE) is None


def test_dev_login_only_in_dev(client, settings, monkeypatch):
    monkeypatch.setenv("DEV_LOGIN_INSTALLATION_ID", "12345678")
    monkeypatch.setattr(settings, "env", "test")
    assert client.post("/auth/dev").status_code == 404
    monkeypatch.setattr(settings, "env", "dev")
    r = client.post("/auth/dev?next=/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    user = load_session(client.cookies.get(SESSION_COOKIE))
    assert user is not None and user.installation_ids == [12345678] and user.login == "dev"
    monkeypatch.setattr(settings, "github_oauth_client_id", None)
    r = client.get("/login")
    assert "Dev login" in r.text
