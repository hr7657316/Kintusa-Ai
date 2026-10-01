"""Small helpers used by the billing module (one import hop from the call site)."""

from __future__ import annotations


def to_minor_units(amount: float) -> int:
    """12.5 -> 1250."""
    return int(round(amount * 100))
