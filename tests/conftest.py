"""Shared fixtures.

* ``db`` / ``session``: a throwaway Postgres database per test session (``kintsua_test_<hex>``)
  created from the maintenance DB in ``TEST_DATABASE_URL`` (default: local ``postgres``
  with the dev password), tables from ``Base.metadata``; every test runs in a clean
  truncation.
* ``fake_llm``: a scripted LLM installed as the process default.
* ``settings``: the Settings object, with the environment forced to ``test``.

Tests that need Postgres are marked ``db`` automatically when they use these fixtures;
if the server is unreachable they are skipped, not failed.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("KINTSUA_ENV", "test")
os.environ.setdefault("ENV", "test")
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("SANDBOX_BACKEND", "local")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("DATA_DIR", str(Path(__file__).parent / ".data"))

MAINT_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:dev@localhost:5432/postgres"
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def settings():
    from kintsua.core.config import get_settings, reset_settings_cache

    reset_settings_cache()
    return get_settings()


@pytest.fixture(scope="session")
def _test_database_url():
    """Create a throwaway database; yield its URL; drop it afterwards."""
    from sqlalchemy import create_engine, text

    name = f"kintsua_test_{uuid.uuid4().hex[:10]}"
    try:
        admin = create_engine(MAINT_URL, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable at {MAINT_URL}: {e}")
    url = MAINT_URL.rsplit("/", 1)[0] + f"/{name}"
    yield url
    try:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    except Exception:  # noqa: BLE001
        pass
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def db(_test_database_url, settings):
    """Engine bound to the throwaway database, installed as the process engine."""
    from sqlalchemy import create_engine

    from kintsua.core import db as dbmod
    from kintsua.core.models import Base

    os.environ["DATABASE_URL"] = _test_database_url
    settings.database_url = _test_database_url
    engine = create_engine(_test_database_url, pool_pre_ping=True, future=True)
    Base.metadata.create_all(engine)
    dbmod.configure_engine(engine)
    yield engine
    dbmod.reset_engine()
    engine.dispose()


@pytest.fixture
def session(db):
    """A session on a freshly truncated database."""
    from sqlalchemy import text

    from kintsua.core.db import new_session
    from kintsua.core.models import Base

    with db.begin() as conn:
        names = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        conn.execute(text(f"TRUNCATE {names} CASCADE"))
    s = new_session()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def fake_llm():
    from kintsua.core.config import get_settings
    from kintsua.core.llm import LLM, FakeLLM, set_llm

    fake = FakeLLM()
    set_llm(LLM(provider=fake, settings=get_settings()))
    yield fake
    set_llm(None)


@pytest.fixture
def fake_github():
    """A stand-in GitHubApp whose installation clients are in-memory fakes."""
    from kintsua.core.github import set_github_app
    from tests.fakes.github import FakeGitHubApp

    app = FakeGitHubApp()
    set_github_app(app)
    yield app
    set_github_app(None)


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "session" in getattr(item, "fixturenames", ()) or "db" in getattr(
            item, "fixturenames", ()
        ):
            item.add_marker(pytest.mark.db)
