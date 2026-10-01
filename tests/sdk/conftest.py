"""SDK test fixtures: an isolated fingerprint state dir, no SDK env vars, and a fresh reporter
registry per test (threads are stopped and queues drained so nothing is posted at exit)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def sdk_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KINTSUA_STATE_DIR", str(tmp_path / "state"))
    for k in ("KINTSUA_KEY", "KINTSUA_REPO", "KINTSUA_URL"):
        monkeypatch.delenv(k, raising=False)
    from kintsua.sdk import report

    report._default = None
    report._pool.clear()
    yield
    for r in (report._default, *report._pool.values()):
        if r is not None:
            r._stop.set()
            r._wake.set()
            r._q.clear()
    report._default = None
    report._pool.clear()


@pytest.fixture
def reporter():
    """An enabled default reporter that never flushes on its own (long interval) and whose
    POSTs fail fast (connection refused on 127.0.0.1:9) if something does flush."""
    from kintsua.sdk import report

    rep = report.configure(
        key="kts_test_key", repo="acme/agent", url="http://127.0.0.1:9", flush_interval_s=3600
    )
    rep.retry_delay_s = 0.01
    return rep
