"""Webhook signature verification (core-security-9)."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from kintsua.core.github import verify_webhook_signature

BODY = b'{"zen": "Keep it logically awesome."}'
SECRET = "s3cret"


def _sig(body: bytes = BODY, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature():
    assert verify_webhook_signature(BODY, _sig(), SECRET) is True
    assert verify_webhook_signature(BODY, _sig(secret="other"), SECRET) is False
    assert verify_webhook_signature(BODY, _sig(), None) is False


@pytest.mark.parametrize(
    "header",
    [
        "sha256=\u00e9\u00e9",  # latin-1 decoded header bytes: used to raise TypeError
        "sha256=" + "\u00e9" * 64,
        "sha256=abc123",
        "sha256=" + "g" * 64,
        "sha256=",
        "sha1=" + "a" * 40,
        "",
        None,
    ],
)
def test_malformed_signature_headers_are_a_mismatch_not_an_error(header):
    assert verify_webhook_signature(BODY, header, SECRET) is False


def test_webhook_endpoint_answers_401_for_a_non_ascii_signature(session):
    from fastapi.testclient import TestClient

    from kintsua.api.app import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    r = client.post(
        "/webhooks/github",
        content=BODY,
        headers={
            "X-Hub-Signature-256": b"sha256=\xe9\xe9",  # raw non-ASCII header bytes
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "d-1",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 401


class _Integration:
    """``GithubIntegration.get_access_token`` stand-in: each call mints a new token."""

    def __init__(self, lifetime_s: float):
        self.lifetime_s = lifetime_s
        self.minted: list[str] = []

    def get_access_token(self, installation_id: int):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace

        self.minted.append(f"ghs_{installation_id}_{len(self.minted) + 1}")
        expires = datetime.now(UTC) + timedelta(seconds=self.lifetime_s)
        return SimpleNamespace(token=self.minted[-1], expires_at=expires)


def _app(lifetime_s: float):
    from kintsua.core.github import GitHubApp

    app = GitHubApp(app_id="1", private_key="pem", base_url="https://api.github.com")
    integration = _Integration(lifetime_s)
    app._integration = integration
    return app, integration


def test_installation_token_honours_min_ttl():
    """A cached token is reused only while it stays valid for ``min_ttl_s`` more seconds:
    a caller that needs it for a long step (the fixer's push) gets a fresh one."""
    app, integration = _app(lifetime_s=300)  # cached token has ~5 minutes left
    first = app.installation_token(7)
    assert app.installation_token(7) == first  # default min_ttl_s=120: still good
    assert app.installation_token(7, min_ttl_s=120) == first
    fresh = app.installation_token(7, min_ttl_s=600)  # 5 minutes is not enough
    assert fresh != first and integration.minted == [first, fresh]
    assert app.installation_token(8) != fresh  # cached per installation


def test_clone_url_passes_min_ttl_through():
    from kintsua.core.github import DEFAULT_MIN_TOKEN_TTL_S

    assert DEFAULT_MIN_TOKEN_TTL_S == 120
    app, integration = _app(lifetime_s=300)
    url = app.clone_url(7, "acme/agent")
    assert url == f"https://x-access-token:{integration.minted[0]}@github.com/acme/agent.git"
    assert app.clone_url(7, "acme/agent") == url
    longer = app.clone_url(7, "acme/agent", min_ttl_s=600)
    assert longer != url and integration.minted[1] in longer
