"""Postgres-backed job queue (``SELECT ... FOR UPDATE SKIP LOCKED``).

Every arrow in the architecture is a job here. Components register handlers with
``@job_handler("kind")``; the worker claims one job at a time, runs the handler in a
fresh session, and records success or failure with exponential backoff.

Handler signature: ``def handler(session: Session, payload: dict) -> Any`` (sync) or
``async def handler(session, payload)``; async handlers run under ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kintsua.core.models import Job
from kintsua.core.util import utcnow

log = logging.getLogger(__name__)

Handler = Callable[[Session, dict[str, Any]], Any]
HANDLERS: dict[str, Handler] = {}

# Job kinds (documented so every component uses the same names).
JOB_TICK = "tick"
JOB_POLL_SOURCE = "poll_source"
JOB_CLASSIFY_EVENT = "classify_event"
JOB_MATCH_EVENT = "match_event"
JOB_OPEN_ISSUE = "open_issue"
JOB_INDEX_REPO = "index_repo"
JOB_FIX_IMPACT = "fix_impact"
JOB_MATCH_RUNTIME = "match_runtime"
JOB_BILLING_TICK = "billing_tick"
JOB_WEEKLY_REPORT = "weekly_report"
JOB_PR_OUTCOME = "pr_outcome"


def job_handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        if kind in HANDLERS and HANDLERS[kind] is not fn:
            log.warning("job handler for %s re-registered", kind)
        HANDLERS[kind] = fn
        return fn

    return deco


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    run_after: datetime | None = None,
    delay_s: float | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
) -> Job | None:
    """Insert a queued job. With ``dedupe_key``, a second queued job with the same key is
    dropped (returns the existing one) so bursts collapse into one run.

    Uses a SAVEPOINT so a duplicate does not poison the caller's transaction.
    """
    when = run_after or (utcnow() + timedelta(seconds=delay_s) if delay_s else utcnow())
    payload = _jsonable(payload or {})
    if dedupe_key:
        existing = session.scalar(
            select(Job).where(Job.dedupe_key == dedupe_key, Job.status == "queued").limit(1)
        )
        if existing is not None:
            return existing
    job = Job(
        kind=kind, payload=payload, run_after=when, dedupe_key=dedupe_key, max_attempts=max_attempts
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        # lost a race on dedupe_key: return the winner
        return session.scalar(
            select(Job).where(Job.dedupe_key == dedupe_key, Job.status == "queued").limit(1)
        )
    return job


def reschedule_queued(session: Session, dedupe_key: str, run_after: datetime) -> bool:
    """Push an already-queued deduped job later (push debounce). Returns True if one existed."""
    res = session.execute(
        update(Job)
        .where(Job.dedupe_key == dedupe_key, Job.status == "queued")
        .values(run_after=run_after)
    )
    return (res.rowcount or 0) > 0


def claim(session: Session, kinds: list[str] | None = None) -> Job | None:
    """Claim one due job with SKIP LOCKED. Commits the claim so other workers see it."""
    kind_filter = ""
    params: dict[str, Any] = {}
    if kinds:
        kind_filter = " AND kind = ANY(:kinds)"
        params["kinds"] = kinds
    row = session.execute(
        text(
            f"""
            UPDATE job SET status = 'running', locked_at = now(), attempts = attempts + 1
            WHERE id = (
                SELECT id FROM job
                WHERE status = 'queued' AND run_after <= now(){kind_filter}
                ORDER BY run_after
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id
            """
        ),
        params,
    ).first()
    session.commit()
    if row is None:
        return None
    return session.get(Job, row[0])


def complete(session: Session, job: Job) -> None:
    job.status = "done"
    job.finished_at = utcnow()
    job.locked_at = None
    session.commit()


def backoff_seconds(attempts: int) -> float:
    return min(30.0 * (2 ** max(attempts - 1, 0)), 3600.0)


def fail(session: Session, job: Job, error: str, *, retry: bool = True) -> None:
    job.last_error = error[-4000:]
    job.locked_at = None
    if retry and job.attempts < job.max_attempts:
        job.status = "queued"
        job.run_after = utcnow() + timedelta(seconds=backoff_seconds(job.attempts))
    else:
        job.status = "failed"
        job.finished_at = utcnow()
    session.commit()


def run_job(session: Session, job: Job) -> Any:
    handler = HANDLERS.get(job.kind)
    if handler is None:
        raise RuntimeError(f"no handler registered for job kind {job.kind!r}")
    payload = dict(job.payload)
    payload.setdefault("_job_id", str(job.id))
    if inspect.iscoroutinefunction(handler):
        return asyncio.run(handler(session, payload))
    result = handler(session, payload)
    if inspect.isawaitable(result):
        return asyncio.run(_await(result))
    return result


async def _await(x: Any) -> Any:
    return await x


def process_one(session_factory: Callable[[], Session], kinds: list[str] | None = None) -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    session = session_factory()
    try:
        job = claim(session, kinds)
        if job is None:
            return False
        job_id, kind = job.id, job.kind
        log.info("job %s %s start (attempt %s)", kind, job_id, job.attempts)
        t0 = time.monotonic()
        try:
            run_job(session, job)
            session.commit()
            complete(session, job)
            log.info("job %s %s done in %.1fs", kind, job_id, time.monotonic() - t0)
        except Exception as e:  # noqa: BLE001
            session.rollback()
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}"
            log.exception("job %s %s failed", kind, job_id)
            job = session.get(Job, job_id)
            if job is not None:
                fail(session, job, err, retry=not isinstance(e, NonRetryable))
        return True
    finally:
        session.close()


class NonRetryable(Exception):
    """Raise from a handler to mark the job failed without retries."""


def run_worker(
    session_factory: Callable[[], Session],
    kinds: list[str] | None = None,
    *,
    once: bool = False,
    idle_sleep_s: float = 2.0,
    stop_event: threading.Event | None = None,
) -> int:
    """Loop forever (or until the queue is empty with ``once``). Returns jobs processed."""
    n = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            return n
        try:
            worked = process_one(session_factory, kinds)
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            worked = False
            time.sleep(idle_sleep_s)
        if worked:
            n += 1
            continue
        if once:
            return n
        time.sleep(idle_sleep_s)


def drain(
    session_factory: Callable[[], Session], kinds: list[str] | None = None, limit: int = 1000
) -> int:
    """Run queued jobs until none are due (tests and ``--once``)."""
    n = 0
    while n < limit and process_one(session_factory, kinds):
        n += 1
    return n


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj
