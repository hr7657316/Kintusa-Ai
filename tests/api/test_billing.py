"""Stripe Checkout, the Stripe webhook and the monthly billing tick (stripe monkeypatched)."""

from __future__ import annotations

import json

import pytest

from kintsua.core.models import Org
from kintsua.core.queue import HANDLERS
from tests.api.conftest import mint_session


@pytest.fixture
def stripe_mod():
    import stripe

    return stripe


def test_checkout_503_when_stripe_is_not_configured(client, factory, settings, monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", None)
    monkeypatch.setattr(settings, "stripe_price_id", None)
    factory.org()
    assert client.post("/v1/billing/checkout", json={}).status_code == 401
    mint_session(client)
    r = client.post("/v1/billing/checkout", json={})
    assert r.status_code == 503 and "STRIPE_SECRET_KEY" in r.json()["error"]


def test_checkout_creates_a_subscription_session(
    client, factory, settings, monkeypatch, stripe_mod
):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    monkeypatch.setattr(settings, "stripe_price_id", "price_123")
    org = factory.org()
    repo = factory.repo(org)
    src = factory.source()
    factory.dependency(repo, src)
    factory.dependency(repo, src, kind="npm", name="stripe-node")
    factory.dependency(repo, None, kind="http", name="api.unknown.example")
    inactive = factory.repo(org, "acme/old", github_repo_id=9, active=False)
    factory.dependency(inactive, src)
    calls: list[dict] = []

    def fake_create(**params):
        calls.append(params)
        return {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}

    monkeypatch.setattr(stripe_mod.checkout.Session, "create", fake_create)
    mint_session(client)
    r = client.post("/v1/billing/checkout", json={"email": "cto@acme.example"})
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://checkout.stripe.com/c/pay/cs_test_1"
    assert r.json()["quantity"] == 2
    assert r.headers["HX-Redirect"] == "https://checkout.stripe.com/c/pay/cs_test_1"
    params = calls[0]
    assert params["mode"] == "subscription"
    assert params["line_items"] == [{"price": "price_123", "quantity": 2}]
    assert params["client_reference_id"] == str(org.id)
    assert params["customer_email"] == "cto@acme.example"
    assert (
        params["success_url"].startswith(settings.base_url)
        and "{CHECKOUT_SESSION_ID}" in params["success_url"]
    )
    assert params["cancel_url"].startswith(settings.base_url)
    assert stripe_mod.api_key == "sk_test_123"

    # an existing customer is reused, and the quantity is at least one
    org.stripe_customer_id = "cus_existing"
    factory.commit()
    empty_org = factory.org("empty", installation_id=4242)
    mint_session(client, [4242])
    r = client.post("/v1/billing/checkout", json={"org_id": str(empty_org.id)})
    assert r.status_code == 200 and r.json()["quantity"] == 1

    def boom(**params):
        raise RuntimeError("card declined")

    monkeypatch.setattr(stripe_mod.checkout.Session, "create", boom)
    r = client.post("/v1/billing/checkout", json={"org_id": str(empty_org.id)})
    assert r.status_code == 502 and "card declined" not in r.text


def test_stripe_webhook_flow(client, factory, session, settings, monkeypatch, stripe_mod):
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    org = factory.org()

    def fake_construct(payload, sig_header, secret):
        assert secret == "whsec_test"
        if sig_header != "good":
            raise stripe_mod.SignatureVerificationError("No signatures found", sig_header)
        return json.loads(payload)

    monkeypatch.setattr(stripe_mod.Webhook, "construct_event", fake_construct)

    def send(event: dict, sig: str = "good"):
        return client.post(
            "/v1/billing/webhook",
            content=json.dumps(event),
            headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
        )

    r = send(
        {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_1",
                    "client_reference_id": str(org.id),
                    "customer": "cus_123",
                    "subscription": "sub_123",
                }
            },
        }
    )
    assert r.status_code == 200, r.text
    assert r.json()["handled"] == "checkout.session.completed" and r.json()["plan"] == "pro"
    session.expire_all()
    row = session.get(Org, org.id)
    assert row.plan == "pro" and row.stripe_customer_id == "cus_123"
    assert row.settings["stripe_subscription_id"] == "sub_123" and row.settings["pro_since"]

    r = send(
        {
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": "in_1",
                    "customer": "cus_123",
                    "subscription": "sub_123",
                    "attempt_count": 2,
                    "hosted_invoice_url": "https://invoice.stripe.com/i/in_1",
                }
            },
        }
    )
    assert r.status_code == 200 and r.json()["billing_warning"] is True
    session.expire_all()
    warning = session.get(Org, org.id).settings["billing_warning"]
    assert warning["kind"] == "payment_failed" and warning["attempt_count"] == 2
    assert warning["hosted_invoice_url"] == "https://invoice.stripe.com/i/in_1"

    r = send({"type": "invoice.paid", "data": {"object": {"id": "in_2", "customer": "cus_123"}}})
    assert r.status_code == 200 and r.json()["billing_warning"] is False
    session.expire_all()
    assert "billing_warning" not in session.get(Org, org.id).settings

    r = send(
        {
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_123", "customer": "cus_123"}},
        }
    )
    assert r.status_code == 200 and r.json()["plan"] == "free"
    session.expire_all()
    row = session.get(Org, org.id)
    assert (
        row.plan == "free"
        and "stripe_subscription_id" not in row.settings
        and row.settings["pro_until"]
    )

    # unknown orgs and unrelated events
    r = send(
        {
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_x", "customer": "cus_x"}},
        }
    )
    assert r.status_code == 200 and r.json()["skipped"] == "unknown org"
    r = send({"type": "charge.refunded", "data": {"object": {"id": "ch_1"}}})
    assert r.status_code == 202 and r.json()["ignored"] == "charge.refunded"

    # a bad signature is a 400, never a change
    r = send(
        {
            "type": "checkout.session.completed",
            "data": {"object": {"client_reference_id": str(org.id)}},
        },
        sig="bad",
    )
    assert r.status_code == 400
    session.expire_all()
    assert session.get(Org, org.id).plan == "free"


def test_stripe_webhook_503_when_unconfigured(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", None)
    r = client.post("/v1/billing/webhook", content=b"{}", headers={"Stripe-Signature": "x"})
    assert r.status_code == 503


def test_billing_tick_updates_subscription_quantities(
    session, factory, settings, monkeypatch, stripe_mod
):
    from kintsua.api.billing import billing_tick

    assert HANDLERS["billing_tick"] is billing_tick
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    monkeypatch.setattr(settings, "stripe_price_id", "price_123")
    org = factory.org(plan="pro", stripe_subscription_id="sub_123")
    repo = factory.repo(org)
    src = factory.source()
    factory.dependency(repo, src)
    factory.dependency(repo, src, kind="npm", name="stripe-node")
    factory.dependency(repo, None, kind="http", name="api.unknown.example")
    factory.org("free-org", installation_id=4242)
    factory.org("pro-without-sub", installation_id=4343, plan="pro")
    modified: list[tuple] = []

    def fake_retrieve(sub_id, **kw):
        return {
            "id": sub_id,
            "items": {"data": [{"id": "si_1", "quantity": 1, "price": {"id": "price_123"}}]},
        }

    monkeypatch.setattr(stripe_mod.Subscription, "retrieve", fake_retrieve)
    monkeypatch.setattr(
        stripe_mod.SubscriptionItem, "modify", lambda item_id, **kw: modified.append((item_id, kw))
    )

    # an uninstalled pro org is flagged for the operator, its quantity left alone
    gone = factory.org("gone", installation_id=4444, plan="pro", stripe_subscription_id="sub_9")
    gone.active = False
    factory.commit()

    out = billing_tick(session, {})
    assert out["orgs"] == 2 and out["updated"] == 1 and out["errors"] == []
    assert out["inactive"] == [str(gone.id)]
    assert modified == [("si_1", {"quantity": 2, "proration_behavior": "none"})]
    session.expire_all()
    assert session.get(Org, org.id).settings["billing_last_quantity"] == 2
    warning = session.get(Org, gone.id).settings["billing_warning"]
    assert warning["kind"] == "org_inactive" and warning["subscription"] == "sub_9"

    # already at the right quantity: nothing to modify
    monkeypatch.setattr(
        stripe_mod.Subscription,
        "retrieve",
        lambda sub_id, **kw: {
            "id": sub_id,
            "items": {"data": [{"id": "si_1", "quantity": 2, "price": {"id": "price_123"}}]},
        },
    )
    out = billing_tick(session, {})
    assert out["updated"] == 0 and len(modified) == 1

    # a Stripe failure is reported, not raised
    def broken(sub_id, **kw):
        raise RuntimeError("stripe down")

    monkeypatch.setattr(stripe_mod.Subscription, "retrieve", broken)
    out = billing_tick(session, {})
    assert out["updated"] == 0 and out["errors"] and "stripe down" in out["errors"][0]

    # not configured: skipped and logged
    monkeypatch.setattr(settings, "stripe_secret_key", None)
    out = billing_tick(session, {})
    assert out["skipped"] == "stripe not configured" and out["orgs"] == 2
