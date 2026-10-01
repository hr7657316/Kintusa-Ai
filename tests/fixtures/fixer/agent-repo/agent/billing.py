"""Charges customers through Stripe (fixture for the fixer tests)."""

from __future__ import annotations

import stripe

from agent.helpers import to_minor_units


def charge_customer(amount: float, currency: str, source: str) -> dict[str, str]:
    """Create a charge for a saved payment instrument."""
    charge = stripe.Charge.create(
        amount=to_minor_units(amount),
        currency=currency,
        source=source,
        description="Kintsua fixture charge",
    )
    return {"id": charge["id"], "status": charge["status"]}
