"""Postgres-backed job queue (``SELECT ... FOR UPDATE SKIP LOCKED``).

Every arrow in the architecture is a job here. Components register handlers with
``@job_handler("kind")``; the worker claims one job at a time, runs the handler in a
fresh session, and records success or failure with exponential backoff.

Handler signature: ``def handler(session: Session, payload: dict) -> Any`` (sync) or
``async def handler(session, payload)``; async handlers run under ``asyncio.run``.

Delivery is at-least-once. A running job heartbeats ``locked_at``; a job whose worker
died (SIGKILL, OOM, lost machine) stops heartbeating and, once ``locked_at`` is older
than ``Settings.job_lease_s``, ``reap_stale`` hands it out again (or fails it when its
attempts are used up). A worker that is interrupted mid-job (shutdown) puts the job
straight back. At most one *queued* job exists per ``dedupe_key``; a failing job whose
key already has a queued sibling is marked superseded instead of being requeued.
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

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kintsua.core.config import get_settings
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

DEFAULT_MAX_ATTEMPTS = 5
# Retry budgets for the pipeline arrows. With the 30 s doubling backoff capped at an hour,
# 12 attempts ride out ~5 h of provider or GitHub outage before a job gives up (5 would
# give up after ~7.5 minutes). The scheduler tick must never give up.
MAX_ATTEMPTS_BY_KIND: dict[str, int] = {
    JOB_TICK: 1_000_000,
    JOB_CLASSIFY_EVENT: 12,
    JOB_MATCH_EVENT: 12,
    JOB_OPEN_ISSUE: 20,
    JOB_FIX_IMPACT: 8,
    JOB_PR_OUTCOME: 12,
    JOB_INDEX_REPO: 8,
    JOB_BILLING_TICK: 12,
    JOB_WEEKLY_REPORT: 12,
}

SUPERSEDED_PREFIX = "superseded by queued job"
REAP_EVERY_S = 60.0  # how often (per process) claim() looks for orphaned running jobs
_reap_lock = threading.Lock()
_next_reap_at = 0.0  # time.monotonic() deadline; 0 = reap on the next claim


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
    max_attempts: int | None = None,
) -> Job | None:
    """Insert a queued job. With ``dedupe_key``, a second queued job with the same key is
    dropped (returns the existing one) so bursts collapse into one run.

    ``max_attempts`` defaults to the kind's budget in ``MAX_ATTEMPTS_BY_KIND`` (else
    ``DEFAULT_MAX_ATTEMPTS``). Uses a SAVEPOINT so a duplicate does not poison the
    caller's transaction.
    """
    if max_attempts is None:
        max_attempts = MAX_ATTEMPTS_BY_KIND.get(kind, DEFAULT_MAX_ATTEMPTS)
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
    """Claim one due job with SKIP LOCKED. Commits the claim so other workers see it.

    About once a minute per process it first reclaims orphaned jobs (``reap_stale``)."""
    _maybe_reap(session)
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


def fail(
    session: Session,
    job: Job,
    error: str,
    *,
    retry: bool = True,
    delay_s: float | None = None,
) -> None:
    """Record a failed attempt and commit: back to the queue after ``delay_s`` (default:
    exponential backoff) while attempts remain, else ``failed``.

    Only one queued job may exist per ``dedupe_key`` (the partial unique index), and
    ``enqueue`` queues a sibling while this one runs. If such a sibling exists the job is
    not requeued: the sibling will do the work, so this one ends ``failed`` with a
    ``superseded by queued job <id>`` note instead of violating the index."""
    job_id = job.id
    error = error[-4000:]
    requeue = retry and job.attempts < job.max_attempts
    sibling = _queued_sibling(session, job) if requeue else None
    if sibling is not None:
        _mark_superseded(job, sibling, error)
    elif requeue:
        job.status = "queued"
        job.locked_at = None
        job.last_error = error
        wait = backoff_seconds(job.attempts) if delay_s is None else max(delay_s, 0.0)
        job.run_after = utcnow() + timedelta(seconds=wait)
    else:
        job.status = "failed"
        job.locked_at = None
        job.last_error = error
        job.finished_at = utcnow()
    try:
        session.commit()
    except IntegrityError:
        # a sibling was queued between the check and the commit
        session.rollback()
        job = session.get(Job, job_id)
        if job is None:
            return
        _mark_superseded(job, _queued_sibling(session, job), error)
        session.commit()


def _queued_sibling(session: Session, job: Job) -> uuid.UUID | None:
    if not job.dedupe_key:
        return None
    return session.scalar(
        select(Job.id)
        .where(Job.dedupe_key == job.dedupe_key, Job.status == "queued", Job.id != job.id)
        .limit(1)
    )


def _mark_superseded(job: Job, sibling: uuid.UUID | None, error: str) -> None:
    job.status = "failed"
    job.locked_at = None
    job.finished_at = utcnow()
    job.last_error = f"{SUPERSEDED_PREFIX} {sibling or '?'}; last error: {error[-3800:]}"


def _fail_safely(
    session: Session, job_id: Any, error: str, *, retry: bool, delay_s: float | None = None
) -> None:
    """``fail`` that never leaves the job ``running`` because recording the failure itself
    broke: the last resort marks it ``failed`` directly (if even that fails, the lease
    reaper gets it later)."""
    try:
        job = session.get(Job, job_id)
        if job is not None:
            fail(session, job, error, retry=retry, delay_s=delay_s)
        return
    except Exception:  # noqa: BLE001
        log.exception("job %s: could not record the failure; marking it failed", job_id)
    try:
        session.rollback()
        session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running")
            .values(
                status="failed",
                locked_at=None,
                finished_at=func.now(),
                last_error=("failure not recorded normally; " + error)[-4000:],
            )
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except Exception:  # noqa: BLE001
        log.exception("job %s: last-resort failure update failed", job_id)
        session.rollback()


def reap_stale(session: Session, *, lease_s: float | None = None, limit: int = 100) -> int:
    """Hand out again the jobs a dead worker left ``running``: ``locked_at`` (which a live
    worker heartbeats) older than the lease. Each goes through ``fail`` with no backoff,
    so it is requeued at once, superseded by a queued sibling, or ``failed`` when its
    attempts are used up. Returns how many were reaped."""
    lease = float(get_settings().job_lease_s if lease_s is None else lease_s)
    cutoff = func.now() - timedelta(seconds=lease)
    seen: list[Any] = []  # a row whose failure cannot be recorded is not retried this pass
    n = 0
    while n < limit:
        q = select(Job).where(Job.status == "running", Job.locked_at < cutoff)
        if seen:
            q = q.where(Job.id.not_in(seen))
        job = session.scalar(q.order_by(Job.locked_at).limit(1).with_for_update(skip_locked=True))
        if job is None:
            break
        job_id, kind, attempt = job.id, job.kind, job.attempts
        seen.append(job_id)
        log.warning(
            "job %s %s: lease expired (no heartbeat for %ss); worker lost during attempt %s",
            kind,
            job_id,
            int(lease),
            attempt,
        )
        _fail_safely(
            session,
            job_id,
            f"lease expired after {int(lease)}s: the worker was lost during attempt {attempt}",
            retry=True,
            delay_s=0,
        )
        n += 1
    session.commit()
    return n


def _maybe_reap(session: Session) -> None:
    global _next_reap_at
    now = time.monotonic()
    with _reap_lock:
        if now < _next_reap_at:
            return
        _next_reap_at = now + REAP_EVERY_S
    try:
        reap_stale(session)
    except Exception:  # noqa: BLE001 - reaping must never stop the worker from claiming
        log.exception("reaping stale jobs failed")
        session.rollback()


def release(session_factory: Callable[[], Session], job_id: Any) -> None:
    """Put a job this worker was running straight back in the queue without charging the
    attempt (the worker is shutting down, it is not the job's fault)."""
    try:
        session = session_factory()
    except Exception:  # noqa: BLE001
        log.exception("job %s: could not release it (the lease reaper will)", job_id)
        return
    try:
        job = session.get(Job, job_id)
        if job is not None and job.status == "running":
            kind = job.kind
            job.attempts = max(job.attempts - 1, 0)
            _fail_safely(
                session, job_id, "interrupted: the worker shut down mid-job", retry=True, delay_s=0
            )
            log.warning("job %s %s: interrupted; returned to the queue", kind, job_id)
    except Exception:  # noqa: BLE001
        log.exception("job %s: could not release it (the lease reaper will)", job_id)
    finally:
        session.close()


def requeue_failed(
    session: Session,
    *,
    kinds: list[str] | None = None,
    since: datetime | None = None,
    limit: int = 1000,
) -> int:
    """Operator re-drive (``python -m kintsua.worker --requeue-failed``): put ``failed``
    jobs back in the queue with a fresh retry budget. Skips superseded jobs and any job
    whose dedupe key already has a newer or queued job (that job carries the work).
    Returns how many were requeued."""
    q = select(Job).where(Job.status == "failed")
    if kinds:
        q = q.where(Job.kind.in_(kinds))
    if since is not None:
        q = q.where(Job.finished_at >= since)
    q = q.order_by(Job.created_at.desc()).limit(limit).with_for_update(skip_locked=True)
    n = 0
    for job in session.scalars(q).all():
        if (job.last_error or "").startswith(SUPERSEDED_PREFIX):
            continue
        if job.dedupe_key:
            other = session.scalar(
                select(Job.id)
                .where(
                    Job.dedupe_key == job.dedupe_key,
                    Job.id != job.id,
                    (Job.status == "queued") | (Job.created_at > job.created_at),
                )
                .limit(1)
            )
            if other is not None:
                continue
        try:
            with session.begin_nested():
                job.status = "queued"
                job.attempts = 0
                job.locked_at = None
                job.finished_at = None
                job.run_after = utcnow()
                session.flush()
        except IntegrityError:  # a sibling was queued concurrently
            continue
        n += 1
    session.commit()
    return n


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


def heartbeat_interval_s() -> float:
    """How often a running job refreshes ``locked_at``: a fifth of the lease, at most 60 s."""
    return max(1.0, min(60.0, get_settings().job_lease_s / 5))


class _Heartbeat:
    """Background thread that keeps a running job's ``locked_at`` fresh (own session), so
    ``reap_stale`` only ever reclaims jobs whose worker is gone."""

    def __init__(
        self, session_factory: Callable[[], Session], job_id: Any, interval_s: float
    ) -> None:
        self.session_factory = session_factory
        self.job_id = job_id
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="job-heartbeat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                s = self.session_factory()
                try:
                    s.execute(
                        update(Job)
                        .where(Job.id == self.job_id, Job.status == "running")
                        .values(locked_at=func.now())
                        .execution_options(synchronize_session=False)
                    )
                    s.commit()
                finally:
                    s.close()
            except Exception:  # noqa: BLE001
                log.warning("job %s: heartbeat failed", self.job_id, exc_info=True)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)


def process_one(session_factory: Callable[[], Session], kinds: list[str] | None = None) -> bool:
    """Claim and run one job. Returns False when the queue is empty.

    A handler exception is recorded with ``fail``; an interrupt (``KeyboardInterrupt``,
    ``SystemExit``: the worker is shutting down) returns the job to the queue at once and
    propagates."""
    session = session_factory()
    try:
        job = claim(session, kinds)
        if job is None:
            return False
        job_id, kind = job.id, job.kind
        log.info("job %s %s start (attempt %s)", kind, job_id, job.attempts)
        t0 = time.monotonic()
        try:
            beat = _Heartbeat(session_factory, job_id, heartbeat_interval_s())
            try:
                run_job(session, job)
            finally:
                beat.stop()
            session.commit()
            complete(session, job)
            log.info("job %s %s done in %.1fs", kind, job_id, time.monotonic() - t0)
        except Exception as e:  # noqa: BLE001
            session.rollback()
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}"
            log.exception("job %s %s failed", kind, job_id)
            _fail_safely(session, job_id, err, retry=not isinstance(e, NonRetryable))
        except BaseException:
            release(session_factory, job_id)
            raise
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
