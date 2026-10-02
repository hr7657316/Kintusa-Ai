"""Secret redaction (core-security-3)."""

from __future__ import annotations

import pytest

from kintsua.core.util import redact_secrets

SECRET_SAMPLES = [
    (("Authorization: Basic " + "dXNlcjpwYXNzd29yZA=="), "dXNlcjpwYXNzd29yZA=="),
    ("authorization: Token 0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
    ('{"Proxy-Authorization": "Digest abcdef123456"}', "abcdef123456"),
    (("Authorization: Bearer abcdefghi" + "jklmnopqrstuvwxyz"), "abcdefghijklmnopqrstuvwxyz"),
    (("key=sk-proj-" + "AbCdEfGhIjKlMnOpQrStUvWx_yz-0123456789"), "AbCdEfGhIjKlMnOpQrStUvWx"),
    (("sk-svcacct-AbCdEfGh" + "IjKlMnOpQrStUvWx0123"), "AbCdEfGhIjKlMnOpQrStUvWx0123"),
    ("using kts_" + "A" * 16 + "b-_" + "C" * 13, "A" * 16),
    (
        ("github_pat_11ABCDEFG01234567" + "89_abcdefghijklmnopqrstuvwxyz"),
        "abcdefghijklmnopqrstuvwxyz",
    ),
    (("maps AIzaSyA-0123456789a" + "bcdefghijklmnopqrstu"), "0123456789abcdefghijklmnopqrstu"),
    (("slack xapp-1-" + "A0123456789-0123456789-abcdef"), "A0123456789-0123456789-abcdef"),
    ("slack xoxe-1-abcdefghij0123", "abcdefghij0123"),
    ("//registry.npmjs.org/:_authToken npm_" + "a1B2" * 9, "a1B2" * 9),
    ("pypi-" + "AgEIcHlwaS5vcmc" * 4, "AgEIcHlwaS5vcmc" * 4),
    ("fetch https://user:SECRETPW@api.example.com/x failed", "SECRETPW"),
    ("git clone https://x-access-token:ghs_tok123@github.com/a/b.git", "ghs_tok123"),
    ("postgresql+psycopg://postgres:dev-pass@db:5432/kintsua", "dev-pass"),
    # a Basic credential without its header name; private key spellings
    (("echo: basic " + "dXNlcjpwYXNzd29yZDEyMw=="), "dXNlcjpwYXNzd29yZDEyMw=="),
    (("upstream said Basic " + "YWxhZGRpbjpvcGVuc2VzYW1l"), "YWxhZGRpbjpvcGVuc2VzYW1l"),
    (
        ("private-key=MII" + "EvQIBADANBgkqhkiG9w0BAQEFAAS"),
        ("MII" + "EvQIBADANBgkqhkiG9w0BAQEFAAS"),
    ),
    (
        ('{"privateKey": "MII' + 'EvQIBADANBgkqhkiG9w0BAQEFAAS"}'),
        ("MII" + "EvQIBADANBgkqhkiG9w0BAQEFAAS"),
    ),
    (
        ('{"private_key": "MII' + 'EvQIBADANBgkqhkiG9w0BAQEFAAS"}'),
        ("MII" + "EvQIBADANBgkqhkiG9w0BAQEFAAS"),
    ),
    # still covered
    ("ghp_" + "a" * 36, "a" * 36),
    ("sk_live_" + "b" * 24, "b" * 24),
    ("sk-ant-api03-" + "c" * 30, "c" * 30),
    (("AKIAABCDEF" + "GHIJKLMNOP"), "ABCDEFGHIJKLMNOP"),
    ('{"password": "hunter22"}', "hunter22"),
]


@pytest.mark.parametrize("text,secret", SECRET_SAMPLES)
def test_redact_secrets_removes_known_shapes(text, secret):
    out = redact_secrets(text)
    assert secret not in out, out
    assert "[REDACTED]" in out


def test_redaction_keeps_the_url_host_and_plain_text():
    assert redact_secrets("https://user:pw123@api.example.com/v1") == (
        "https://[REDACTED]@api.example.com/v1"
    )
    plain = "GET https://api.example.com:8443/v1/items?id=42 returned 500: Basic auth failed"
    assert redact_secrets(plain) == plain
    assert redact_secrets("kts_short") == "kts_short"  # the 12-char key prefix shown in the UI


def test_basic_and_private_key_patterns_leave_prose_alone():
    for prose in (
        "basic authentication failed",
        "Basic auth is not supported; see the private keys docs",
        "a basic example",
        "the privatekey field is required",
    ):
        assert redact_secrets(prose) == prose
