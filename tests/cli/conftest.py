"""CLI test fixtures: no SDK / Actions environment leaks into the invocations."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("KINTSUA_KEY", "KINTSUA_REPO", "KINTSUA_URL", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
