"""api-correctness-2: the ``async`` routes (they await the request body) must run their
blocking database / GitHub / Stripe / httpx work in the threadpool, never on the event loop
that also serves ``/v1/ingest`` and every other request."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from kintsua.api import billing, keys, report, repos, setup, webhooks
from kintsua.api.deps import MANIFEST_SALT, serializer
from tests.api.conftest import mint_session, post_webhook


def on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class Spy:
    """Wraps a module attribute and records, per call, whether it ran on the event loop."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, module: Any, name: str, fake: Any = None):
        self.calls: list[bool] = []
        real: Callable[..., Any] = getattr(module, name)

        def spy(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(on_event_loop())
            return fake(*args, **kwargs) if fake is not None else real(*args, **kwargs)

        monkeypatch.setattr(module, name, spy)

    def assert_off_loop(self) -> None:
        assert self.calls, "the wrapped function was not called"
        assert self.calls == [False] * len(self.calls), "blocking work ran on the event loop"


def test_github_webhook_handlers_run_off_the_event_loop(client, monkeypatch):
    calls: list[bool] = []

    def ping(session, payload):
        calls.append(on_event_loop())
        return {"zen": payload.get("zen")}

    monkeypatch.setitem(webhooks.HANDLERS, "ping", ping)
    r = post_webhook(client, "ping", {"zen": "z"})
    assert r.status_code == 200 and r.json()["zen"] == "z"
    assert calls == [False]


def test_settings_writes_run_off_the_event_loop(client, factory, monkeypatch):
    org = factory.org()
    repo = factory.repo(org)
    mint_session(client)
    spies = [
        Spy(monkeypatch, repos, "add_source"),
        Spy(monkeypatch, repos, "dismiss_muted_impacts"),
        Spy(monkeypatch, repos, "effective_fix_mode"),
        Spy(monkeypatch, repos, "open_issues_for_pending"),
        Spy(monkeypatch, keys, "rotate_sdk_key"),
        Spy(monkeypatch, report, "subscribe_email"),
    ]
    base = f"/v1/repos/{repo.id}"
    url = "https://api.vendor.example/openapi.json"
    assert client.post(f"{base}/sources", json={"kind": "openapi", "url": url}).status_code == 201
    assert client.post(f"{base}/mute", json={"pattern": "vendor"}).status_code == 200
    assert client.post(f"{base}/fix-mode", json={"mode": "pr"}).status_code == 200
    assert client.post(f"{base}/issues/open", json={}).status_code == 200
    assert client.post("/v1/keys", json={}).status_code == 200
    assert client.post("/report/subscribe", json={"email": "a@b.example"}).status_code == 200
    for spy in spies:
        spy.assert_off_loop()


def test_billing_and_setup_run_off_the_event_loop(client, factory, settings, monkeypatch):
    import stripe

    factory.org()
    mint_session(client)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    monkeypatch.setattr(settings, "stripe_price_id", "price_123")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    checkout = Spy(
        monkeypatch,
        billing,
        "create_checkout_session",
        fake=lambda session, org, email=None: {"url": "https://checkout.example/x"},
    )
    stripe_event = Spy(monkeypatch, billing, "handle_stripe_event", fake=lambda s, e: None)
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda p, s, k: json.loads(p))
    manifest = Spy(
        monkeypatch,
        setup,
        "convert_manifest",
        fake=lambda s, code: {"id": 1, "slug": "kintsua-dev", "pem": "x"},
    )

    assert client.post("/v1/billing/checkout", json={}).status_code == 200
    r = client.post(
        "/v1/billing/webhook",
        content=json.dumps({"type": "customer.created", "data": {"object": {}}}),
        headers={"Stripe-Signature": "sig", "Content-Type": "application/json"},
    )
    assert r.status_code == 202
    state = serializer(MANIFEST_SALT).dumps({"n": "nonce"})
    assert client.get(f"/setup/github/callback?code=abc&state={state}").status_code == 200
    for spy in (checkout, stripe_event, manifest):
        spy.assert_off_loop()
