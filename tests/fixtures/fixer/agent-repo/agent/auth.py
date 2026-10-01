"""Loads credentials from the environment. Kintsua never patches this module."""

from __future__ import annotations

import os


def stripe_api_key() -> str:
    try:
        return os.environ["STRIPE_API_KEY"]
    except KeyError:
        return "unset"
