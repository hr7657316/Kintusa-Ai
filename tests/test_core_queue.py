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
