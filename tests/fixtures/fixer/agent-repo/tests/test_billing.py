import stripe

from agent.billing import charge_customer


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "ch_123", "status": "succeeded"}


def test_charge_passes_amount_in_minor_units(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(stripe.Charge, "create", rec)
    out = charge_customer(12.5, "usd", "tok_visa")
    assert out == {"id": "ch_123", "status": "succeeded"}
    (call,) = rec.calls
    assert call["amount"] == 1250
    assert call["currency"] == "usd"


def test_charge_passes_the_payment_instrument(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(stripe.Charge, "create", rec)
    charge_customer(1, "eur", "tok_mastercard")
    (call,) = rec.calls
    assert "tok_mastercard" in call.values()
