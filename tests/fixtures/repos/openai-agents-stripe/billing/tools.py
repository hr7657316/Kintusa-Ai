import os

import stripe
from agents import function_tool

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"])


@function_tool
def charge_customer(customer_id: str, amount_cents: int, currency: str = "usd") -> str:
    """Charge a customer."""
    intent = client.v1.payment_intents.create(
        params={
            "amount": amount_cents,
            "currency": currency,
            "customer": customer_id,
            "confirm": True,
        }
    )
    return intent.id


@function_tool
def refund(charge_id: str) -> str:
    """Refund a charge."""
    r = stripe.Refund.create(charge=charge_id)
    return r.id


def customer_email(customer_id: str) -> str:
    return stripe.Customer.retrieve(customer_id).email


def recent_charges(limit: int = 10):
    return stripe.Charge.list(limit=limit).auto_paging_iter()
