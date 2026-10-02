from datetime import timedelta

from sqlalchemy import select

from kintsua.core.models import Job
from kintsua.core.queue import (
    HANDLERS,
    NonRetryable,
    drain,
    enqueue,
    job_handler,
    reschedule_queued,
)
from kintsua.core.util import utcnow


def test_enqueue_claim_complete(session, db):
    from kintsua.core.db import new_session

    seen = []

    @job_handler("test_echo")
    def _h(s, payload):
        seen.append(payload["x"])

    enqueue(session, "test_echo", {"x": 1})
    enqueue(session, "test_echo", {"x": 2})
    session.commit()
    assert drain(new_session, ["test_echo"]) == 2
    assert sorted(seen) == [1, 2]
    s2 = new_session()
    assert {j.status for j in s2.scalars(select(Job))} == {"done"}
    s2.close()
    HANDLERS.pop("test_echo", None)


def test_dedupe_and_reschedule(session):
    a = enqueue(session, "k", {"a": 1}, dedupe_key="k:1")
    b = enqueue(session, "k", {"a": 2}, dedupe_key="k:1")
    assert a is not None and b is not None and a.id == b.id
    later = utcnow() + timedelta(minutes=5)
    assert reschedule_queued(session, "k:1", later)
    session.refresh(a)
    assert abs((a.run_after - later).total_seconds()) < 1


def test_failure_backoff_and_nonretryable(session, db):
    from kintsua.core.db import new_session

    @job_handler("test_fail")
    def _f(s, payload):
        raise RuntimeError("boom")

    @job_handler("test_fatal")
    def _g(s, payload):
        raise NonRetryable("nope")

    j1 = enqueue(session, "test_fail", {})
    j2 = enqueue(session, "test_fatal", {})
    session.commit()
    drain(new_session, ["test_fail", "test_fatal"])
    s2 = new_session()
    a, b = s2.get(Job, j1.id), s2.get(Job, j2.id)
    assert (
        a.status == "queued"
        and a.attempts == 1
        and "boom" in a.last_error
        and a.run_after > utcnow()
    )
    assert b.status == "failed" and "nope" in b.last_error
    s2.close()
    HANDLERS.pop("test_fail", None)
    HANDLERS.pop("test_fatal", None)


async def test_async_handler(session, db):
    from kintsua.core.db import new_session

    out = []

    @job_handler("test_async")
    async def _a(s, payload):
        out.append("ran")

    enqueue(session, "test_async", {})
    session.commit()
    # drain runs the async handler under asyncio.run inside the worker
    import asyncio

    await asyncio.to_thread(drain, new_session, ["test_async"])
    assert out == ["ran"]
    HANDLERS.pop("test_async", None)


# --------------------------------------------------------------------------------------
# review fixes: lease reaper, superseded siblings, interrupts, retry budgets, re-drive
# --------------------------------------------------------------------------------------


def _running_job(session, kind="k_stale", *, age_s=7200, attempts=1, max_attempts=5, key=None):
    from sqlalchemy import text

    job = enqueue(session, kind, {}, dedupe_key=key, max_attempts=max_attempts)
    session.flush()
    session.execute(
        text(
            "UPDATE job SET status = 'running', attempts = :a, "
            "locked_at = now() - make_interval(secs => :age) WHERE id = :id"
        ),
        {"a": attempts, "age": age_s, "id": job.id},
    )
    session.commit()
    session.expire_all()
    return job.id


def test_stale_running_job_is_handed_out_again(session, db, monkeypatch):
    from kintsua.core import queue
    from kintsua.core.db import new_session

    job_id = _running_job(session, attempts=1)
    fresh_id = _running_job(session, age_s=5)  # a live worker's job (recent heartbeat)
    monkeypatch.setattr(queue, "_next_reap_at", 0.0)
    s2 = new_session()
    try:
        job = queue.claim(s2, ["k_stale"])
        assert job is not None and job.id == job_id
        assert job.status == "running" and job.attempts == 2
        assert "lease expired" in job.last_error
        assert s2.get(Job, fresh_id).status == "running"
    finally:
        s2.close()


def test_stale_job_out_of_attempts_ends_failed(session):
    from kintsua.core.queue import reap_stale

    job_id = _running_job(session, attempts=5, max_attempts=5)
    assert reap_stale(session, lease_s=60) == 1
    job = session.get(Job, job_id)
    session.refresh(job)
    assert job.status == "failed" and job.finished_at is not None and job.locked_at is None
    assert "lease expired" in job.last_error


def test_stale_job_with_queued_sibling_is_superseded_without_integrity_error(session):
    from kintsua.core.queue import reap_stale

    job_id = _running_job(session, key="dup:1")
    sibling = enqueue(session, "k_stale", {}, dedupe_key="dup:1")
    session.commit()
    assert sibling.id != job_id
    assert reap_stale(session, lease_s=60) == 1
    session.expire_all()
    job = session.get(Job, job_id)
    assert job.status == "failed" and job.last_error.startswith("superseded by queued job")
    assert str(sibling.id) in job.last_error
    assert session.get(Job, sibling.id).status == "queued"


def test_running_job_heartbeat_keeps_its_lease(session, db, monkeypatch):
    import time

    from kintsua.core import queue
    from kintsua.core.db import new_session

    monkeypatch.setattr(queue, "heartbeat_interval_s", lambda: 0.05)
    seen = {}

    @job_handler("test_slow")
    def _slow(s, payload):
        first = new_session()
        try:
            seen["claimed"] = first.get(Job, payload["_job_id"]).locked_at
        finally:
            first.close()
        time.sleep(0.4)
        later = new_session()
        try:
            seen["later"] = later.get(Job, payload["_job_id"]).locked_at
        finally:
            later.close()

    try:
        enqueue(session, "test_slow", {})
        session.commit()
        assert drain(new_session, ["test_slow"]) == 1
    finally:
        HANDLERS.pop("test_slow", None)
    assert seen["later"] > seen["claimed"]


def test_retryable_failure_with_queued_sibling_does_not_orphan_the_job(session, db):
    """fail() used to requeue onto the partial unique dedupe index: IntegrityError out of
    process_one and the job stuck 'running' forever."""
    from kintsua.core.db import new_session
    from kintsua.core.queue import process_one

    @job_handler("test_vt")
    def _h(s, payload):
        other = new_session()
        try:
            enqueue(other, "test_vt", {"n": 2}, dedupe_key="vt:1")
            other.commit()
        finally:
            other.close()
        raise RuntimeError("transient")

    try:
        a = enqueue(session, "test_vt", {"n": 1}, dedupe_key="vt:1")
        session.commit()
        assert process_one(new_session, ["test_vt"]) is True
    finally:
        HANDLERS.pop("test_vt", None)
    s2 = new_session()
    try:
        jobs = {j.id: j for j in s2.scalars(select(Job).where(Job.kind == "test_vt"))}
        first = jobs.pop(a.id)
        (second,) = jobs.values()
        assert first.status == "failed" and first.locked_at is None
        assert first.last_error.startswith(f"superseded by queued job {second.id}")
        assert "transient" in first.last_error
        assert second.status == "queued"
    finally:
        s2.close()


def test_failure_recording_errors_never_leave_the_job_running(session, db, monkeypatch):
    from kintsua.core import queue
    from kintsua.core.db import new_session

    @job_handler("test_badfail")
    def _h(s, payload):
        raise RuntimeError("boom")

    def broken_fail(*a, **kw):
        raise RuntimeError("could not record")

    monkeypatch.setattr(queue, "fail", broken_fail)
    try:
        j = enqueue(session, "test_badfail", {})
        session.commit()
        assert queue.process_one(new_session, ["test_badfail"]) is True
    finally:
        HANDLERS.pop("test_badfail", None)
    s2 = new_session()
    try:
        job = s2.get(Job, j.id)
        assert job.status == "failed" and job.locked_at is None and "boom" in job.last_error
    finally:
        s2.close()


def test_interrupted_job_goes_straight_back_without_charging_the_attempt(session, db):
    import pytest

    from kintsua.core.db import new_session
    from kintsua.core.queue import process_one

    @job_handler("test_interrupt")
    def _h(s, payload):
        raise KeyboardInterrupt

    try:
        j = enqueue(session, "test_interrupt", {})
        session.commit()
        with pytest.raises(KeyboardInterrupt):
            process_one(new_session, ["test_interrupt"])
    finally:
        HANDLERS.pop("test_interrupt", None)
    s2 = new_session()
    try:
        job = s2.get(Job, j.id)
        assert job.status == "queued" and job.attempts == 0 and job.locked_at is None
        assert job.run_after <= utcnow() and "interrupted" in job.last_error
    finally:
        s2.close()


def test_pipeline_jobs_get_a_longer_retry_budget(session):
    from kintsua.core.queue import (
        DEFAULT_MAX_ATTEMPTS,
        JOB_CLASSIFY_EVENT,
        JOB_MATCH_EVENT,
        JOB_POLL_SOURCE,
        JOB_TICK,
        backoff_seconds,
    )

    classify = enqueue(session, JOB_CLASSIFY_EVENT, {})
    match = enqueue(session, JOB_MATCH_EVENT, {})
    tick = enqueue(session, JOB_TICK, {})
    poll = enqueue(session, JOB_POLL_SOURCE, {})
    explicit = enqueue(session, JOB_CLASSIFY_EVENT, {"x": 1}, max_attempts=3)
    assert classify.max_attempts >= 12 and match.max_attempts >= 12
    assert tick.max_attempts >= 1000
    assert poll.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert explicit.max_attempts == 3
    # a classify job keeps retrying through a multi-hour provider outage
    budget = sum(backoff_seconds(n) for n in range(1, classify.max_attempts))
    assert budget >= 4 * 3600


def test_requeue_failed_redrives_failed_jobs(session):
    from kintsua.core.queue import fail, requeue_failed

    lone = enqueue(session, "k_redrive", {"n": 1})
    keyed = enqueue(session, "k_redrive", {"n": 2}, dedupe_key="rd:1")
    shadowed = enqueue(session, "k_redrive", {"n": 3}, dedupe_key="rd:2")
    other_kind = enqueue(session, "k_other", {"n": 4})
    session.commit()
    for job in (lone, keyed, shadowed, other_kind):
        job.attempts = job.max_attempts
        fail(session, job, "provider outage")
    newer = enqueue(session, "k_redrive", {"n": 5}, dedupe_key="rd:2")  # still queued
    session.commit()

    assert requeue_failed(session, kinds=["k_redrive"]) == 2
    session.expire_all()
    for job in (lone, keyed):
        j = session.get(Job, job.id)
        assert j.status == "queued" and j.attempts == 0 and j.finished_at is None
    assert session.get(Job, shadowed.id).status == "failed"
    assert session.get(Job, newer.id).status == "queued"
    assert session.get(Job, other_kind.id).status == "failed"
    assert requeue_failed(session) == 1  # k_other; the rest are queued now
