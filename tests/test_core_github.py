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
