"""Settings guards (core-security-1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kintsua.core.config import DEFAULT_SESSION_SECRET, Settings


def test_prod_refuses_the_default_session_secret(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(ValidationError, match="SESSION_SECRET"):
        Settings(env="prod", _env_file=None)
    with pytest.raises(ValidationError, match="SESSION_SECRET"):
        Settings(env="prod", session_secret=DEFAULT_SESSION_SECRET, _env_file=None)


@pytest.mark.parametrize("weak", ["change-me", "short-secret", "x" * 31])
def test_prod_refuses_placeholder_or_short_secrets(weak):
    with pytest.raises(ValidationError, match="SESSION_SECRET"):
        Settings(env="prod", session_secret=weak, _env_file=None)


def test_prod_accepts_a_real_secret_and_dev_keeps_the_default(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    s = Settings(env="prod", session_secret="k" * 40, _env_file=None)
    assert s.env == "prod"
    assert Settings(env="dev", _env_file=None).session_secret == DEFAULT_SESSION_SECRET
    assert Settings(env="test", _env_file=None).session_secret == DEFAULT_SESSION_SECRET
