"""Server-side watcher plumbing: Postgres snapshots, change-event emission, the ``tick`` /
``poll_source`` job handlers, the daily run and the CLI.

Seams (see docs/CONTRACTS.md):

* ``source_ref_for(source_row) -> SourceRef``
* ``find_or_create_source(session, kind, locator, *, display_name, vendor, meta, poll_interval_s)``
* ``latest_snapshot(session, source_id) -> SnapshotRecord | None``
* ``ingest_canonical_doc(session, source_row, doc, *, text=None) -> ChangeEventRow | None``
* ``ensure_tick_job(session)``
* ``failing_sources(session, days=7) -> list[FailingSource]``
* ``poll_all(session_factory, *, kinds=None, only_due=True)``

Semantics worth knowing:

* ``Source.last_polled_at`` is the time of the last *successful* poll (a failed fetch does
  not move it), so "failing for 7 days" = ``consecutive_failures > 0`` and the last success
  (or creation) is older than 7 days.
* A fetch failure never fails the job: it is recorded on the source (``last_error``,
  ``consecutive_failures``, ``backoff_until = now + min(poll_interval_s * 2**failures,
  settings.source_backoff_ceiling_s)``) and the next tick skips the source until the
  backoff passes. Only a missing source raises ``NonRetryable``.
* The diff stored on ``change_event.diff`` is the full ``ChangeEvent`` dump. When the ops
  list serialises above 256 KB the full dump goes through ``put_document`` (file/S3),
  ``raw_diff_ref`` points at it and the row keeps the first 200 ops with ``ops_truncated``.
* Watchers may expose ``event_text(before, after) -> str | None`` (release notes, new
  changelog paragraphs, changed tool descriptions); it fills ``ChangeEvent.text`` when the
  caller passes none.

CLI: ``python -m kintsua.watchers.run [--all] [--kind mcp] [--source-id X] [--report]``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kintsua.core.config import get_settings
from kintsua.core.events import CanonicalDoc, ChangeEvent, canonical_json
from kintsua.core.models import SOURCE_KINDS, ChangeEventRow, Job, Snapshot, Source
from kintsua.core.queue import (
    JOB_BILLING_TICK,
    JOB_CLASSIFY_EVENT,
    JOB_POLL_SOURCE,
    JOB_TICK,
    JOB_WEEKLY_REPORT,
    NonRetryable,
    enqueue,
    job_handler,
)
from kintsua.core.storage import get_document, put_document, ref_to_string
from kintsua.core.util import parse_duration, utcnow
from kintsua.report.markdown import FailingSource
from kintsua.watchers.base import (
    BaseWatcher,
    SnapshotRecord,
    SourceRef,
    compare,
    get_watcher,
)

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVALS: dict[str, int] = {
    "mcp": 6 * 3600,
    "openapi": 24 * 3600,
    "pypi": 6 * 3600,
    "npm": 6 * 3600,
    "changelog": 24 * 3600,
}
PAID_MCP_POLL_INTERVAL_S = 3600
DIFF_INLINE_LIMIT_BYTES = 256 * 1024
DIFF_OPS_PREVIEW = 200
FAILING_DAYS = 7


def default_poll_interval(kind: str, meta: dict[str, Any] | None = None) -> int:
    """Spec defaults: MCP 6h (1h when a paying customer depends on it), OpenAPI daily,
    packages 6h, changelog daily."""
    if kind == "mcp" and (meta or {}).get("paid"):
        return PAID_MCP_POLL_INTERVAL_S
    return DEFAULT_POLL_INTERVALS.get(kind, 6 * 3600)


# --------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------


def source_ref_for(source_row: Source) -> SourceRef:
    meta = dict(source_row.meta or {})
    meta.setdefault("user_agent", get_settings().user_agent)
    return SourceRef(
        id=source_row.id,
        kind=source_row.kind,  # type: ignore[arg-type]
        locator=source_row.locator,
        display_name=source_row.display_name or source_row.locator,
        vendor=source_row.vendor,
        meta=meta,
    )


def find_or_create_source(
    session: Session,
    kind: str,
    locator: str,
    *,
    display_name: str | None = None,
    vendor: str | None = None,
    meta: dict[str, Any] | None = None,
    poll_interval_s: int | None = None,
) -> Source:
    """Unique on (kind, locator). An existing row is returned as-is except that missing
    display_name / vendor / meta keys are filled in (never overwritten)."""
    if kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source kind {kind!r}")
    locator = locator.strip()
    if not locator:
        raise ValueError("empty locator")
    row = session.scalar(select(Source).where(Source.kind == kind, Source.locator == locator))
    if row is not None:
        if display_name and (not row.display_name or row.display_name == row.locator):
            row.display_name = display_name
        if vendor and not row.vendor:
            row.vendor = vendor
        if meta:
            merged = {**meta, **(row.meta or {})}
            if merged != (row.meta or {}):
                row.meta = merged
        return row
    row = Source(
        kind=kind,
        locator=locator,
        display_name=display_name or locator,
        vendor=vendor,
        meta=dict(meta or {}),
        poll_interval_s=poll_interval_s or default_poll_interval(kind, meta),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(Source).where(Source.kind == kind, Source.locator == locator)
        )
        if existing is None:  # pragma: no cover - cannot happen
            raise
        return existing
    return row


# --------------------------------------------------------------------------------------
# Snapshots and change events
# --------------------------------------------------------------------------------------


def _latest_snapshot_row(session: Session, source_id: UUID) -> Snapshot | None:
    return session.scalar(
        select(Snapshot)
        .where(Snapshot.source_id == source_id)
        .order_by(Snapshot.captured_at.desc(), Snapshot.created_at.desc())
        .limit(1)
    )


def _record(snap: Snapshot, *, load_body: bool) -> SnapshotRecord:
    return SnapshotRecord(
        source_id=snap.source_id,
        content_hash=snap.content_hash,
        version=snap.version,
        captured_at=snap.captured_at,
        body=get_document(snap.storage_ref) if load_body else None,
        snapshot_id=snap.id,
    )


def latest_snapshot(
    session: Session, source_id: UUID, *, load_body: bool = True
) -> SnapshotRecord | None:
    snap = _latest_snapshot_row(session, source_id)
    return _record(snap, load_body=load_body) if snap is not None else None


def _store_diff(event: ChangeEvent) -> dict[str, Any]:
    """The jsonb payload for ``change_event.diff``: the full ChangeEvent dump, or a
    truncated one pointing at the full document when the ops list is large."""
    dump = event.model_dump(mode="json")
    ops_json = canonical_json(dump["ops"])
    if len(ops_json.encode("utf-8")) <= DIFF_INLINE_LIMIT_BYTES:
        event.raw_diff_ref = event.raw_diff_ref or "inline"
        dump["raw_diff_ref"] = event.raw_diff_ref
        return dump
    ref = put_document(dump, prefix="diffs")
    event.raw_diff_ref = ref_to_string(ref)
    dump = event.model_dump(mode="json")
    total = len(dump["ops"])
    dump["ops"] = dump["ops"][:DIFF_OPS_PREVIEW]
    dump["ops_truncated"] = True
    dump["ops_total"] = total
    return dump


def event_text_for(
    watcher: BaseWatcher | Any, before: CanonicalDoc, after: CanonicalDoc
) -> str | None:
    hook = getattr(watcher, "event_text", None)
    if hook is None:
        return None
    try:
        text = hook(before, after)
    except Exception as e:  # noqa: BLE001 - text is optional
        log.warning("event_text hook failed for %s: %s", getattr(watcher, "kind", "?"), e)
        return None
    return str(text)[:20_000] if text else None


def ingest_canonical_doc(
    session: Session,
    source_row: Source,
    doc: CanonicalDoc,
    *,
    text: str | None = None,
    watcher: BaseWatcher | None = None,
) -> ChangeEventRow | None:
    """Store a snapshot of ``doc`` for ``source_row`` and, when it differs from the previous
    snapshot, write a change_event row and enqueue ``classify_event``.

    Returns the new ``ChangeEventRow``, or None when this is the first sight of the source,
    the content hash is unchanged (only ``last_polled_at`` moves) or the diff is empty.
    Does not commit.
    """
    now = utcnow()
    new_hash = doc.content_hash()
    prev_row = _latest_snapshot_row(session, source_row.id)
    if prev_row is not None and prev_row.content_hash == new_hash:
        source_row.last_polled_at = now
        return None

    snap = Snapshot(
        source_id=source_row.id,
        content_hash=new_hash,
        version=doc.version,
        storage_ref=put_document(doc.body),
        captured_at=now,
    )
    session.add(snap)
    session.flush()
    source_row.last_polled_at = now
    if prev_row is None:
        log.info("first snapshot for %s %s", source_row.kind, source_row.locator)
        return None

    previous = _record(prev_row, load_body=True)
    watcher = watcher or get_watcher(source_row.kind)
    if text is None:
        text = event_text_for(watcher, previous.as_doc(), doc)
    event = compare(watcher, source_ref_for(source_row), previous, doc, text=text)
    if event is None:
        return None
    diff_payload = _store_diff(event)
    row = ChangeEventRow(
        source_id=source_row.id,
        from_snapshot_id=prev_row.id,
        to_snapshot_id=snap.id,
        diff=diff_payload,
        detected_at=now,
    )
    session.add(row)
    session.flush()
    enqueue(
        session,
        JOB_CLASSIFY_EVENT,
        {"change_event_id": str(row.id)},
        dedupe_key=f"classify_event:{row.id}",
    )
    log.info(
        "change event %s for %s %s: %d ops (%s -> %s)",
        row.id,
        source_row.kind,
        source_row.locator,
        len(event.ops),
        event.from_version,
        event.to_version,
    )
    return row


# --------------------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------------------


@dataclass
class PollResult:
    source_id: str
    kind: str
    locator: str
    ok: bool
    changed: bool = False
    change_event_id: str | None = None
    version: str | None = None
    error: str | None = None
    backoff_s: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def record_failure(source: Source, error: str) -> int:
    """Exponential backoff with the configured ceiling. Returns the backoff seconds."""
    settings = get_settings()
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    source.last_error = error[:4000]
    delay = min(
        source.poll_interval_s * (2 ** min(source.consecutive_failures, 30)),
        settings.source_backoff_ceiling_s,
    )
    source.backoff_until = utcnow() + timedelta(seconds=delay)
    return int(delay)


def record_success(source: Source) -> None:
    source.consecutive_failures = 0
    source.last_error = None
    source.backoff_until = None


async def poll_source_row(
    session: Session, source: Source, *, watcher: BaseWatcher | None = None
) -> PollResult:
    """Fetch + ingest one source, recording success or failure on the row. Never raises for
    a fetch/ingest failure. Does not commit."""
    base = {"source_id": str(source.id), "kind": source.kind, "locator": source.locator}
    try:
        w = watcher or get_watcher(source.kind)
        doc = await w.fetch(source_ref_for(source))
        event = ingest_canonical_doc(session, source, doc, watcher=w)
    except Exception as e:  # noqa: BLE001 - every failure is recorded, not raised
        msg = f"{type(e).__name__}: {e}".strip()
        delay = record_failure(source, msg)
        log.warning(
            "poll failed for %s %s (failure #%d, backoff %ds): %s",
            source.kind,
            source.locator,
            source.consecutive_failures,
            delay,
            msg[:300],
        )
        return PollResult(**base, ok=False, error=msg, backoff_s=delay)
    record_success(source)
    return PollResult(
        **base,
        ok=True,
        changed=event is not None,
        change_event_id=str(event.id) if event is not None else None,
        version=doc.version,
    )


def is_due(source: Source, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if not source.active:
        return False
    if source.backoff_until is not None and source.backoff_until > now:
        return False
    if source.last_polled_at is None:
        return True
    return source.last_polled_at + timedelta(seconds=source.poll_interval_s) < now


def due_sources(
    session: Session,
    now: datetime | None = None,
    *,
    kinds: Sequence[str] | None = None,
    only_due: bool = True,
) -> list[Source]:
    stmt = select(Source).where(Source.active.is_(True)).order_by(Source.created_at)
    if kinds:
        stmt = stmt.where(Source.kind.in_(list(kinds)))
    rows = list(session.scalars(stmt))
    if not only_due:
        return rows
    now = now or utcnow()
    return [s for s in rows if is_due(s, now)]


def _register(kind: str) -> Any:
    """``@job_handler`` unless this file runs as a script (``python -m kintsua.watchers.run``),
    where the report path imports the real module and would re-register the handlers."""
    if __name__ == "__main__":
        return lambda fn: fn
    return job_handler(kind)


@_register(JOB_POLL_SOURCE)
async def poll_source(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("source_id")
    try:
        source_id = UUID(str(raw))
    except (TypeError, ValueError):
        raise NonRetryable(f"poll_source: bad source_id {raw!r}")
    source = session.get(Source, source_id)
    if source is None:
        raise NonRetryable(f"poll_source: source {source_id} does not exist")
    if not source.active:
        return {"source_id": str(source_id), "skipped": "inactive"}
    result = await poll_source_row(session, source)
    session.commit()
    return result.as_dict()


# --------------------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------------------


def _job_exists(session: Session, dedupe_key: str) -> bool:
    """Any job (queued, running, done or failed) with this dedupe key: used for the once-per
    period jobs so a Monday's later ticks do not re-run the weekly report."""
    return session.scalar(select(Job.id).where(Job.dedupe_key == dedupe_key).limit(1)) is not None


def weekly_report_key(now: datetime) -> str:
    iso = now.isocalendar()
    return f"weekly_report:{iso.year}-W{iso.week:02d}"


def billing_tick_key(now: datetime) -> str:
    return f"billing_tick:{now:%Y-%m}"


@_register(JOB_TICK)
def tick(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    now = utcnow()
    due = due_sources(session, now)
    for s in due:
        enqueue(
            session, JOB_POLL_SOURCE, {"source_id": str(s.id)}, dedupe_key=f"poll_source:{s.id}"
        )
    scheduled: list[str] = []
    if now.weekday() == 0:  # Monday UTC
        key = weekly_report_key(now)
        if not _job_exists(session, key):
            enqueue(session, JOB_WEEKLY_REPORT, {}, dedupe_key=key)
            scheduled.append(key)
    if now.day == 1:
        key = billing_tick_key(now)
        if not _job_exists(session, key):
            enqueue(session, JOB_BILLING_TICK, {}, dedupe_key=key)
            scheduled.append(key)
    enqueue(session, JOB_TICK, {}, delay_s=settings.tick_interval_s, dedupe_key="tick")
    session.commit()
    log.info("tick: %d sources due, scheduled=%s", len(due), scheduled or "-")
    return {"polls_enqueued": len(due), "scheduled": scheduled}


def ensure_tick_job(session: Session) -> None:
    """Enqueue the self-perpetuating tick now unless one is already queued."""
    enqueue(session, JOB_TICK, {}, dedupe_key="tick")


# --------------------------------------------------------------------------------------
# Daily run / CLI
# --------------------------------------------------------------------------------------


async def poll_all(
    session_factory: Callable[[], Session],
    *,
    kinds: Sequence[str] | None = None,
    only_due: bool = True,
    source_ids: Sequence[UUID | str] | None = None,
    concurrency: int = 4,
) -> list[PollResult]:
    """Poll every (due) active source, each in its own session, ``concurrency`` at a time."""
    session = session_factory()
    try:
        if source_ids:
            ids = [UUID(str(x)) for x in source_ids]
        else:
            ids = [s.id for s in due_sources(session, kinds=kinds, only_due=only_due)]
    finally:
        session.close()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def run(sid: UUID) -> PollResult:
        async with sem:
            s = session_factory()
            try:
                source = s.get(Source, sid)
                if source is None:
                    return PollResult(str(sid), "?", "?", ok=False, error="source not found")
                result = await poll_source_row(s, source)
                s.commit()
                return result
            except Exception as e:  # noqa: BLE001 - keep the run going
                s.rollback()
                log.exception("poll_all: unexpected error for %s", sid)
                return PollResult(str(sid), "?", "?", ok=False, error=f"{type(e).__name__}: {e}")
            finally:
                s.close()

    return list(await asyncio.gather(*(run(sid) for sid in ids)))


def failing_sources(session: Session, days: int = FAILING_DAYS) -> list[FailingSource]:
    """Active sources that are failing and whose last success (or creation) is older than
    ``days``: a vanished spec is itself a signal for the weekly report."""
    cutoff = utcnow() - timedelta(days=days)
    rows = session.scalars(
        select(Source)
        .where(Source.active.is_(True), Source.consecutive_failures > 0)
        .order_by(Source.vendor, Source.display_name)
    )
    out: list[FailingSource] = []
    for s in rows:
        since = s.last_polled_at or s.created_at
        if since is None or since > cutoff:
            continue
        out.append(
            FailingSource(
                display_name=s.display_name,
                kind=s.kind,
                locator=s.locator,
                last_error=s.last_error,
                failing_since=since,
                consecutive_failures=s.consecutive_failures,
            )
        )
    return out


def _print_results(results: list[PollResult]) -> None:
    ok = [r for r in results if r.ok]
    changed = [r for r in ok if r.changed]
    failed = [r for r in results if not r.ok]
    for r in results:
        status = "CHANGED" if r.changed else ("ok" if r.ok else "FAIL")
        line = f"{status:8} {r.kind:9} {r.locator[:70]:70} {r.version or ''}"
        if r.error:
            line += f"  {r.error[:120]}"
        print(line)
    print(f"\n{len(results)} polled: {len(ok)} ok, {len(changed)} changed, {len(failed)} failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kintsua.watchers.run",
        description="Poll watched sources and emit change events.",
    )
    parser.add_argument(
        "--all", action="store_true", help="poll every active source, not only due ones"
    )
    parser.add_argument("--kind", action="append", help="restrict to a source kind (repeatable)")
    parser.add_argument("--source-id", action="append", help="poll one source by id (repeatable)")
    parser.add_argument("--report", action="store_true", help="print the weekly Markdown report")
    parser.add_argument("--since", default="7d", help="report window, e.g. 7d (default)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--quiet", action="store_true", help="only print the summary / report")
    args = parser.parse_args(argv)

    from kintsua.core.db import new_session
    from kintsua.log import setup_logging

    setup_logging("WARNING" if args.quiet else None)
    do_poll = args.all or args.kind or args.source_id or not args.report
    if do_poll:
        results = asyncio.run(
            poll_all(
                new_session,
                kinds=args.kind,
                only_due=not (args.all or args.source_id),
                source_ids=args.source_id,
                concurrency=args.concurrency,
            )
        )
        _print_results(results)
    if args.report:
        from kintsua.report.build import build_weekly_report
        from kintsua.report.markdown import render_markdown

        until = utcnow()
        since = until - parse_duration(args.since)
        session = new_session()
        try:
            report = build_weekly_report(session, since, until)
        finally:
            session.close()
        if do_poll:
            print()
        sys.stdout.write(render_markdown(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
