"""Worker process: shutdown, sandbox reaping, re-drive, startup checks
(core-correctness-7, core-security-5, core-correctness-3)."""

from __future__ import annotations

import signal
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

from kintsua import worker
from kintsua.core.util import utcnow


class _Obj:
    def __init__(self, name, created_key, created):
        self.name = name
        self.attrs = {created_key: created}
        self.removed = False

    def remove(self, force=False):
        self.removed = True


def _docker_ts(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def test_reaper_removes_only_orphaned_sandboxes(settings):
    now = utcnow()
    old = now - timedelta(seconds=settings.sandbox_wall_clock_s + 3600)
    stale_c = _Obj("kintsua-old", "Created", _docker_ts(old))
    live_c = _Obj("kintsua-live", "Created", _docker_ts(now - timedelta(seconds=60)))
    stale_v = _Obj("kintsua-fix-old", "CreatedAt", old.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    live_v = _Obj("kintsua-fix-live", "CreatedAt", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    seen = []

    def list_containers(all=False, filters=None):
        assert all
        seen.append(filters)
        return [stale_c, live_c]

    client = SimpleNamespace(
        containers=SimpleNamespace(list=list_containers),
        volumes=SimpleNamespace(list=lambda filters=None: [stale_v, live_v]),
    )
    assert worker.reap_orphaned_sandboxes(settings, client=client, now=now) == 2
    assert stale_c.removed and stale_v.removed
    assert not live_c.removed and not live_v.removed
    assert seen == [{"label": worker.SANDBOX_LABEL}]


def test_reaper_is_a_noop_without_docker(settings, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_backend", "local")
    assert worker.reap_orphaned_sandboxes(settings) == 0


def test_shutdown_first_signal_stops_claiming_second_aborts():
    stop = threading.Event()
    sd = worker.Shutdown(stop, grace_s=0)
    sd.handle(signal.SIGTERM, None)
    assert stop.is_set()
    with pytest.raises(KeyboardInterrupt):
        sd.handle(signal.SIGTERM, None)


def test_shutdown_grace_escalates_to_an_abort(monkeypatch):
    sent = []
    monkeypatch.setattr(worker.os, "kill", lambda pid, sig: sent.append(sig))
    stop = threading.Event()
    sd = worker.Shutdown(stop, grace_s=0.05)
    sd.handle(signal.SIGTERM, None)
    sd._timer.join(timeout=2)
    assert sent == [signal.SIGINT]
    sd.cancel()


def test_cleanup_active_sandboxes_removes_registered_ones():
    from kintsua.fixer.sandbox import active_sandboxes, register_active

    cleaned = []
    sb = SimpleNamespace(job_id="j-test", cleanup=lambda: cleaned.append(1))
    register_active(sb)
    assert worker.cleanup_active_sandboxes() == 1
    assert cleaned == [1] and sb not in active_sandboxes()


def test_worker_refuses_to_start_in_prod_without_shared_storage(settings, monkeypatch):
    monkeypatch.setattr(worker, "setup_logging", lambda: None)
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "s3_bucket", None)
    called = []
    monkeypatch.setattr(worker, "register_all_handlers", lambda: called.append(1))
    assert worker.main(["--once", "--no-tick"]) == 2
    assert called == []


def test_requeue_failed_cli(session, monkeypatch):
    from kintsua.core.models import Job
    from kintsua.core.queue import enqueue, fail

    monkeypatch.setattr(worker, "setup_logging", lambda: None)
    job = enqueue(session, "k_cli", {})
    session.commit()
    job.attempts = job.max_attempts
    fail(session, job, "outage")
    assert worker.main(["--requeue-failed", "--kinds", "k_cli", "--since", "1h"]) == 0
    session.expire_all()
    assert session.get(Job, job.id).status == "queued"
