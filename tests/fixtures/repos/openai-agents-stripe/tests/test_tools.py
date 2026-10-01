import stripe

from billing.tools import refund


def test_refund(monkeypatch):
    monkeypatch.setattr(stripe.Refund, "create", lambda **kw: type("R", (), {"id": "re_1"})())
    assert refund("ch_1") == "re_1"


def test_list_charges_shape():
    assert stripe.Charge.list(limit=1) is not None
