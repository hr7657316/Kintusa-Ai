"""Billing: Stripe Checkout, the Stripe webhook and the monthly ``billing_tick`` job.

Free: public repos and up to five watched integrations. Pro: $29 per watched integration
per month, billed on the count of ``dependency`` rows with a watched source on the first of
the month (``billing_tick`` updates the subscription item quantity).

* ``POST /v1/billing/checkout`` (session) -> ``{"url": <Stripe Checkout URL>}``; 503 when
  Stripe is not configured.
* ``POST /v1/billing/webhook`` (Stripe signature) -> ``checkout.session.completed`` sets
  ``org.plan = "pro"``; ``customer.subscription.deleted`` sets it back to ``free``;
  ``invoice.payment_failed`` stores ``org.settings["billing_warning"]``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kintsua.api.deps import get_db, pick_org, read_body, require_user
from kintsua.api.schemas import CheckoutBody, SessionUser
from kintsua.core.config import Settings, get_settings
from kintsua.core.models import Dependency, Org, Repo
from kintsua.core.queue import JOB_BILLING_TICK, job_handler
from kintsua.core.util import utcnow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/billing", tags=["billing"])

PRICING_LINE = (
    "Free for public repos and up to 5 watched integrations · "
    "Pro $29 per watched integration per month"
)
PRO_PLAN = "pro"
FREE_PLAN = "free"


def pricing_line(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return (
        f"Free for public repos and up to {s.free_watched_integration_limit} watched integrations · "
        f"Pro ${s.pro_price_per_integration_usd} per watched integration per month"
    )


def stripe_configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool(s.stripe_secret_key and s.stripe_price_id)


def stripe_module(settings: Settings | None = None) -> Any:
    """The ``stripe`` module with the API key set (imported lazily; tests monkeypatch it)."""
    import stripe

    s = settings or get_settings()
    if s.stripe_secret_key:
        stripe.api_key = s.stripe_secret_key
    return stripe


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from a Stripe object or a plain dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def watched_integration_count(session: Session, org: Org) -> int:
    """Dependency rows of the org's active repos that have a watched (active) source."""
    from kintsua.core.models import Source

    return int(
        session.scalar(
            select(func.count(Dependency.id))
            .join(Repo, Repo.id == Dependency.repo_id)
            .join(Source, Source.id == Dependency.source_id)
            .where(Repo.org_id == org.id, Repo.active.is_(True), Source.active.is_(True))
        )
        or 0
    )


# --------------------------------------------------------------------------------------
# Checkout
# --------------------------------------------------------------------------------------


def create_checkout_session(
    session: Session, org: Org, *, email: str | None = None
) -> dict[str, Any]:
    s = get_settings()
    if not stripe_configured(s):
        raise HTTPException(
            status_code=503,
            detail="billing is not configured (STRIPE_SECRET_KEY / STRIPE_PRICE_ID)",
        )
    quantity = max(1, watched_integration_count(session, org))
    base = s.base_url.rstrip("/")
    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": s.stripe_price_id, "quantity": quantity}],
        "success_url": f"{base}/settings?billing=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/settings?billing=cancelled",
        "client_reference_id": str(org.id),
        "metadata": {"org_id": str(org.id), "installation_id": str(org.github_installation_id)},
        "subscription_data": {"metadata": {"org_id": str(org.id)}},
        "allow_promotion_codes": True,
    }
    if org.stripe_customer_id:
        params["customer"] = org.stripe_customer_id
    elif email:
        params["customer_email"] = email
    stripe = stripe_module(s)
    try:
        checkout = stripe.checkout.Session.create(**params)
    except Exception as e:  # noqa: BLE001 - never leak a Stripe stack trace
        log.exception("Stripe checkout creation failed for org %s", org.name)
        raise HTTPException(status_code=502, detail=f"Stripe error: {type(e).__name__}")
    url = _get(checkout, "url")
    if not url:
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")
    log.info("checkout session for org %s: quantity %d", org.name, quantity)
    return {
        "url": str(url),
        "quantity": quantity,
        "org_id": str(org.id),
        "id": _get(checkout, "id"),
    }


def _checkout(session: Session, user: SessionUser, body: CheckoutBody) -> dict[str, Any]:
    org = pick_org(session, user, body.org_id)
    return create_checkout_session(session, org, email=body.email)


@router.post("/checkout")
async def checkout(
    request: Request,
    user: SessionUser = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    if not stripe_configured():
        raise HTTPException(
            status_code=503,
            detail="billing is not configured (STRIPE_SECRET_KEY / STRIPE_PRICE_ID)",
        )
    body = await read_body(request, CheckoutBody)
    result = await run_in_threadpool(_checkout, session, user, body)
    # HTMX follows HX-Redirect; JSON clients read "url".
    return JSONResponse(result, headers={"HX-Redirect": result["url"]})


# --------------------------------------------------------------------------------------
# Webhook
# --------------------------------------------------------------------------------------


def _org_by_subscription(session: Session, subscription_id: str | None) -> Org | None:
    if not subscription_id:
        return None
    return session.scalar(
        select(Org).where(Org.settings["stripe_subscription_id"].astext == str(subscription_id))
    )


def _org_by_customer(session: Session, customer_id: str | None) -> Org | None:
    if not customer_id:
        return None
    return session.scalar(select(Org).where(Org.stripe_customer_id == str(customer_id)))


def _org_from_checkout(session: Session, obj: Any) -> Org | None:
    ref = _get(obj, "client_reference_id") or _get(_get(obj, "metadata") or {}, "org_id")
    if ref:
        from uuid import UUID

        try:
            org = session.get(Org, UUID(str(ref)))
        except ValueError:
            org = None
        if org is not None:
            return org
    return _org_by_customer(session, _get(obj, "customer"))


def _org_for_invoice(session: Session, obj: Any) -> Org | None:
    return _org_by_subscription(session, _get(obj, "subscription")) or _org_by_customer(
        session, _get(obj, "customer")
    )


def handle_stripe_event(session: Session, event: Any) -> dict[str, Any] | None:
    """Apply one Stripe event; returns a dict for handled types, None for ignored ones."""
    etype = str(_get(event, "type") or "")
    obj = _get(_get(event, "data") or {}, "object") or {}
    now = utcnow().isoformat()
    if etype == "checkout.session.completed":
        org = _org_from_checkout(session, obj)
        if org is None:
            return {"skipped": "unknown org"}
        org.plan = PRO_PLAN
        customer = _get(obj, "customer")
        if customer:
            org.stripe_customer_id = str(customer)
        settings = dict(org.settings or {})
        sub = _get(obj, "subscription")
        if sub:
            settings["stripe_subscription_id"] = str(sub)
        settings.pop("billing_warning", None)
        settings["pro_since"] = settings.get("pro_since") or now
        org.settings = settings
        log.info("org %s is now pro (subscription %s)", org.name, sub)
        return {"org_id": str(org.id), "plan": org.plan}
    if etype == "customer.subscription.deleted":
        org = _org_by_subscription(session, _get(obj, "id")) or _org_by_customer(
            session, _get(obj, "customer")
        )
        if org is None:
            return {"skipped": "unknown org"}
        org.plan = FREE_PLAN
        settings = dict(org.settings or {})
        settings.pop("stripe_subscription_id", None)
        settings["pro_until"] = now
        org.settings = settings
        log.info("org %s is back on free", org.name)
        return {"org_id": str(org.id), "plan": org.plan}
    if etype == "invoice.payment_failed":
        org = _org_for_invoice(session, obj)
        if org is None:
            return {"skipped": "unknown org"}
        settings = dict(org.settings or {})
        settings["billing_warning"] = {
            "kind": "payment_failed",
            "at": now,
            "invoice": _get(obj, "id"),
            "attempt_count": _get(obj, "attempt_count"),
            "hosted_invoice_url": _get(obj, "hosted_invoice_url"),
        }
        org.settings = settings
        log.warning("payment failed for org %s", org.name)
        return {"org_id": str(org.id), "billing_warning": True}
    if etype in ("invoice.paid", "invoice.payment_succeeded"):
        org = _org_for_invoice(session, obj)
        if org is None:
            return {"skipped": "unknown org"}
        if (org.settings or {}).get("billing_warning"):
            settings = dict(org.settings or {})
            settings.pop("billing_warning", None)
            org.settings = settings
        return {"org_id": str(org.id), "billing_warning": False}
    return None


def _apply_stripe_event(session: Session, event: Any) -> dict[str, Any] | None:
    result = handle_stripe_event(session, event)
    session.commit()
    return result


@router.post("/webhook")
async def stripe_webhook(request: Request, session: Session = Depends(get_db)) -> Response:
    s = get_settings()
    if not s.stripe_webhook_secret:
        raise HTTPException(status_code=503, detail="STRIPE_WEBHOOK_SECRET is not configured")
    payload = await request.body()
    signature = request.headers.get("stripe-signature") or ""
    stripe = stripe_module(s)
    try:
        event = stripe.Webhook.construct_event(payload, signature, s.stripe_webhook_secret)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid Stripe payload")
    except Exception as e:  # noqa: BLE001 - SignatureVerificationError and friends
        log.warning("Stripe webhook rejected: %s", type(e).__name__)
        raise HTTPException(status_code=400, detail="invalid Stripe signature")
    etype = str(_get(event, "type") or "")
    result = await run_in_threadpool(_apply_stripe_event, session, event)
    if result is None:
        return JSONResponse({"ok": True, "ignored": etype}, status_code=202)
    return JSONResponse({"ok": True, "handled": etype, **result})


# --------------------------------------------------------------------------------------
# Monthly quantity sync
# --------------------------------------------------------------------------------------


def pro_orgs(session: Session) -> list[Org]:
    return [
        o
        for o in session.scalars(select(Org).where(Org.plan == PRO_PLAN).order_by(Org.name))
        if (o.settings or {}).get("stripe_subscription_id")
    ]


def sync_subscription_quantity(session: Session, org: Org, stripe: Any) -> dict[str, Any]:
    s = get_settings()
    sub_id = str((org.settings or {}).get("stripe_subscription_id"))
    quantity = max(1, watched_integration_count(session, org))
    sub = stripe.Subscription.retrieve(sub_id)
    items = _get(_get(sub, "items") or {}, "data") or []
    item = None
    for it in items:
        if _get(_get(it, "price") or {}, "id") == s.stripe_price_id:
            item = it
            break
    if item is None and items:
        item = items[0]
    if item is None:
        raise RuntimeError(f"subscription {sub_id} has no items")
    current = int(_get(item, "quantity") or 0)
    changed = current != quantity
    if changed:
        stripe.SubscriptionItem.modify(
            _get(item, "id"), quantity=quantity, proration_behavior="none"
        )
    settings = dict(org.settings or {})
    settings["billing_last_quantity"] = quantity
    settings["billing_last_tick"] = utcnow().isoformat()
    org.settings = settings
    return {"org_id": str(org.id), "quantity": quantity, "previous": current, "changed": changed}


def flag_inactive_org(org: Org) -> None:
    """An uninstalled org's subscription keeps billing until someone cancels it in Stripe:
    leave its quantity alone and flag it for the operator (a reinstall reuses the org)."""
    settings = dict(org.settings or {})
    warning = settings.get("billing_warning")
    if isinstance(warning, dict) and warning.get("kind") == "org_inactive":
        return
    settings["billing_warning"] = {
        "kind": "org_inactive",
        "at": utcnow().isoformat(),
        "subscription": settings.get("stripe_subscription_id"),
    }
    org.settings = settings
    log.warning(
        "billing_tick: org %s is uninstalled but subscription %s is still active",
        org.name,
        settings.get("stripe_subscription_id"),
    )


@job_handler(JOB_BILLING_TICK)
def billing_tick(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """First of the month: set every active pro org's subscription quantity to its watched
    integration count (uninstalled orgs are flagged instead). Skips (and logs) when Stripe
    is not configured."""
    orgs = pro_orgs(session)
    if not stripe_configured():
        log.warning("billing_tick: Stripe is not configured; skipping %d pro org(s)", len(orgs))
        return {"skipped": "stripe not configured", "orgs": len(orgs), "updated": 0}
    stripe = stripe_module()
    updated = 0
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    inactive: list[str] = []
    for org in orgs:
        if not org.active:
            flag_inactive_org(org)
            inactive.append(str(org.id))
            continue
        try:
            res = sync_subscription_quantity(session, org, stripe)
        except Exception as e:  # noqa: BLE001 - one org's Stripe trouble must not stop the rest
            log.exception("billing_tick: org %s failed", org.name)
            errors.append(f"{org.name}: {type(e).__name__}: {e}"[:300])
            continue
        results.append(res)
        if res["changed"]:
            updated += 1
    session.commit()
    log.info("billing_tick: %d org(s), %d updated, %d error(s)", len(orgs), updated, len(errors))
    return {
        "orgs": len(orgs),
        "updated": updated,
        "errors": errors,
        "results": results,
        "inactive": inactive,
    }


__all__ = [
    "FREE_PLAN",
    "PRICING_LINE",
    "PRO_PLAN",
    "billing_tick",
    "create_checkout_session",
    "flag_inactive_org",
    "handle_stripe_event",
    "pricing_line",
    "pro_orgs",
    "router",
    "stripe_configured",
    "stripe_module",
    "sync_subscription_quantity",
    "watched_integration_count",
]
