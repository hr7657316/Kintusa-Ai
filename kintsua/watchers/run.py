"""Server-side watcher plumbing: Postgres snapshots, change-event emission, the ``tick`` /
``poll_source`` job handlers, the daily run and the CLI.

Seams (see docs/CONTRACTS.md):

* ``source_ref_for(source_row) -> SourceRef``
* ``find_or_create_source(session, kind, locator, *, display_name, vendor, meta, poll_interval_s,
  origin, owner_org_id)``; raises ``SourceNotShareable`` for another caller's pushed /
  observed / org-owned row
* ``latest_snapshot(session, source_id) -> SnapshotRecord | None``
* ``ingest_canonical_doc(session, source_row, doc, *, text=None) -> ChangeEventRow | None``
* ``ensure_tick_job(session)``
* ``failing_sources(session, days=7, *, public_only=True) -> list[FailingSource]``
* ``poll_all(session_factory, *, kinds=None, only_due=True)``

Semantics worth knowing:

* ``Source.last_polled_at`` is the time of the last *successful* poll (a failed fetch does
  not move it), so "failing for 7 days" = ``consecutive_failures > 0`` and the last success
  (or creation) is older than 7 days.
* A fetch failure never fails the job: it is recorded on the source (``last_error``,
  ``consecutive_failures``, ``backoff_until = now + min(min(poll_interval_s,
  settings.source_backoff_base_s) * 2**(failures - 1), settings.source_backoff_ceiling_s)``)
  and the next tick skips the source until the backoff passes. Only a missing source raises
  ``NonRetryable``. A whole fetch is bounded by ``meta["poll_timeout_s"]``, else
  ``settings.poll_timeout_s`` (10 min); each HTTP request by ``meta["timeout_s"]``, else
  ``settings.poll_http_timeout_s`` (handed to the adapters by ``source_ref_for``). The
  ingest runs in a SAVEPOINT, so a failed ingest persists nothing but the failure.
* Trust (docs/CONTRACTS.md, "Source trust"): only ``Source.pollable`` sources are polled;
  an ``mcp`` source whose locator is a command line is spawned only when
  ``Source.trusted`` (operator-seeded), otherwise the poll records why and deactivates it;
  every other source is fetched with ``meta["_net_guard"]`` (SSRF guard) when
  ``Settings.net_guard`` is on.
* An http(s) MCP source whose ``meta.auth_env`` variable is unset in the worker is
  "unconfigured": not due, and a poll records it without counting a failure, so it never
  shows as failing on the weekly report.
* MCP sources a paying (``pro``) org's active repo depends on are due every hour.
* Each tick also calls ``kintsua.matcher.match.requeue_stalled_issues`` (``open_issue`` jobs
  that ran out of attempts); a failure there is logged and never stops the tick.
* The diff stored on ``change_event.diff`` is the full ``ChangeEvent`` dump. When the ops
  list serialises above 256 KB the full dump goes through ``put_document`` (file/S3),
  ``raw_diff_ref`` / ``storage_ref`` point at it and the row keeps 200 ops, structural ones
  (removed, renamed, required_added ...) first, with ``ops_truncated`` / ``ops_total``.
* Watchers may expose ``event_text(before, after) -> str | None`` (release notes, new
  changelog paragraphs, changed tool descriptions); it fills ``ChangeEvent.text`` when the
  caller passes none.

CLI: ``python -m kintsua.watchers.run [--all] [--kind mcp] [--source-id X] [--report]``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
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
from kintsua.core.models import (
    SOURCE_KINDS,
    SOURCE_ORIGINS,
    ChangeEventRow,
    Dependency,
    Job,
    Org,
    Repo,
    Snapshot,
    Source,
)
from kintsua.core.netguard import is_http_locator
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
from kintsua.core.util import parse_duration, redact_secrets, utcnow
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
PAID_PLANS = ("pro",)
DIFF_INLINE_LIMIT_BYTES = 256 * 1024
DIFF_OPS_PREVIEW = 200
FAILING_DAYS = 7
NOT_POLLED_ORIGINS = ("push", "observed")  # Source.pollable, as SQL
PRIVATE_ORIGINS = ("push", "observed")  # handed only to a caller of the same origin
UNCONFIGURED_PREFIX = "unconfigured: "
STDIO_REFUSED = (
    "refused: the hosted watcher only runs stdio MCP commands from the operator's seed list; "
    "use the server's http(s) URL or `kintsua watch --push` from CI"
)
# preview order for truncated diffs: what the rules classify first
OP_PRIORITY = {
    op: i
    for i, op in enumerate(
        (
            "removed",
            "renamed",
            "required_added",
            "type_changed",
            "enum_changed",
            "auth_changed",
            "deprecated",
            "required_removed",
            "added",
            "text_changed",
        )
    )
}


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
    """The adapter's view of a row. Runtime-only keys (``_...``) are never read from the
    row; ``meta["_net_guard"] = True`` is added for every source that is not
    operator-seeded while ``Settings.net_guard`` is on (rule 3 in docs/CONTRACTS.md).
    ``user_agent`` and ``timeout_s`` (the per-request HTTP timeout every adapter reads)
    default to ``Settings.user_agent`` / ``Settings.poll_http_timeout_s``."""
    settings = get_settings()
    meta = {k: v for k, v in (source_row.meta or {}).items() if not str(k).startswith("_")}
    meta.setdefault("user_agent", settings.user_agent)
    if not meta.get("timeout_s"):
        meta["timeout_s"] = settings.poll_http_timeout_s
    if settings.net_guard and not source_row.trusted:
        meta["_net_guard"] = True
    return SourceRef(
        id=source_row.id,
        kind=source_row.kind,  # type: ignore[arg-type]
        locator=source_row.locator,
        display_name=source_row.display_name or source_row.locator,
        vendor=source_row.vendor,
        meta=meta,
    )


class SourceNotShareable(ValueError):
    """``find_or_create_source`` found a row under this (kind, locator) that the caller may
    not use: another org's pushed snapshot, a repo's synthetic ``observed`` source, or a row
    owned by a different org. Callers record the dependency as unwatched (indexer) or answer
    409/422 (API) instead of attaching it."""

    def __init__(self, source: Source, *, origin: str):
        self.source_id = source.id
        self.kind = source.kind
        self.locator = source.locator
        self.source_origin = source.origin
        self.caller_origin = origin
        owner = "an org-scoped" if source.owner_org_id is not None else "a private"
        super().__init__(
            f"{source.kind} source {source.locator!r} is {owner} {source.origin} source "
            "and cannot be shared"
        )


def _same_org(a: Any, b: Any) -> bool:
    return a is not None and b is not None and str(a) == str(b)


def ensure_shareable(row: Source, *, origin: str, owner_org_id: Any = None) -> None:
    """Raise ``SourceNotShareable`` unless a caller with this ``origin`` / ``owner_org_id``
    may be handed ``row``: a ``push`` / ``observed`` row only to a caller of the same origin,
    and a row with an ``owner_org_id`` only to a caller of that org."""
    if row.origin in PRIVATE_ORIGINS and origin != row.origin:
        raise SourceNotShareable(row, origin=origin)
    if row.owner_org_id is not None and not _same_org(row.owner_org_id, owner_org_id):
        raise SourceNotShareable(row, origin=origin)


def find_or_create_source(
    session: Session,
    kind: str,
    locator: str,
    *,
    display_name: str | None = None,
    vendor: str | None = None,
    meta: dict[str, Any] | None = None,
    poll_interval_s: int | None = None,
    origin: str = "customer",
    owner_org_id: Any = None,
) -> Source:
    """Unique on (kind, locator). An existing row is returned as-is except that missing
    display_name / vendor / meta keys are filled in (never overwritten).

    ``origin`` (see ``kintsua.core.models.SOURCE_ORIGINS``) is set only when the row is
    created; an existing row keeps its origin, so a customer can never upgrade a source to
    ``seed`` by naming the same locator. Only the seed loader promotes rows to ``seed``.
    A seeded row named by anyone else is returned unchanged (its meta decides what the
    worker runs, so no caller may add keys to it), and runtime-only meta keys (``_...``,
    e.g. ``_net_guard``) are never stored.

    Raises ``SourceNotShareable`` (a ``ValueError``) instead of handing out an existing
    ``push`` / ``observed`` row to a caller of another origin, or a row whose
    ``owner_org_id`` differs from the caller's: a repo naming ``push:<other org>:...`` must
    not get a dependency on another org's pushed snapshots."""
    if kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source kind {kind!r}")
    locator = locator.strip()
    if not locator:
        raise ValueError("empty locator")
    meta = {k: v for k, v in (meta or {}).items() if not str(k).startswith("_")}
    row = session.scalar(select(Source).where(Source.kind == kind, Source.locator == locator))
    if row is not None:
        ensure_shareable(row, origin=origin, owner_org_id=owner_org_id)
        if row.trusted and origin != "seed":
            return row
        if display_name and (not row.display_name or row.display_name == row.locator):
            row.display_name = display_name
        if vendor and not row.vendor:
            row.vendor = vendor
        if meta:
            merged = {**meta, **(row.meta or {})}
            if merged != (row.meta or {}):
                row.meta = merged
        return row
    if origin not in SOURCE_ORIGINS:
        raise ValueError(f"unknown source origin {origin!r}")
    row = Source(
        kind=kind,
        locator=locator,
        display_name=display_name or locator,
        vendor=vendor,
        meta=dict(meta or {}),
        poll_interval_s=poll_interval_s or default_poll_interval(kind, meta),
        origin=origin,
        owner_org_id=owner_org_id,
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
        ensure_shareable(existing, origin=origin, owner_org_id=owner_org_id)
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
    truncated one pointing at the full document when the ops list is large. The truncated
    preview keeps structural ops (removed, renamed, required_added ...) ahead of text edits,
    so a removal is never cut off behind hundreds of description changes, and carries the
    full document's ``storage_ref``."""
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
    ranked = sorted(dump["ops"], key=lambda o: OP_PRIORITY.get(str(o.get("op")), len(OP_PRIORITY)))
    dump["ops"] = ranked[:DIFF_OPS_PREVIEW]
    dump["ops_truncated"] = True
    dump["ops_total"] = total
    dump["storage_ref"] = ref
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
    Does not commit; on an exception the caller must roll back (``poll_source_row`` runs it
    in a SAVEPOINT).

    The source row is locked (``SELECT ... FOR UPDATE``) first, so two concurrent ingests of
    one source (overlapping polls, a poll and a push) serialise: the second sees the first's
    snapshot once it commits and returns None instead of emitting a duplicate event.
    """
    now = utcnow()
    new_hash = doc.content_hash()
    session.execute(select(Source.id).where(Source.id == source_row.id).with_for_update())
    prev_row = _latest_snapshot_row(session, source_row.id)
    if prev_row is not None and prev_row.content_hash == new_hash:
        source_row.last_polled_at = now
        return None
    # read the previous body before writing anything: if it cannot be loaded the ingest
    # fails with nothing stored, and the next poll retries the same diff
    previous = _record(prev_row, load_body=True) if prev_row is not None else None

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
    if prev_row is None or previous is None:
        log.info("first snapshot for %s %s", source_row.kind, source_row.locator)
        return None

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
    """Exponential backoff with the configured ceiling. Returns the backoff seconds.

    The base is the poll interval capped at ``Settings.source_backoff_base_s`` (15 min), so
    one transient error is retried soon instead of skipping the next regular poll: a daily
    source waits 15 min, 30 min, 1 h ... and reaches the ceiling
    (``Settings.source_backoff_ceiling_s``, 48 h) only after sustained failure."""
    settings = get_settings()
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    source.last_error = error[:4000]
    cap = max(1, settings.source_backoff_base_s)
    base = max(1, min(source.poll_interval_s or cap, cap))
    delay = min(
        base * (2 ** min(source.consecutive_failures - 1, 30)),
        settings.source_backoff_ceiling_s,
    )
    source.backoff_until = utcnow() + timedelta(seconds=delay)
    return int(delay)


def record_success(source: Source) -> None:
    source.consecutive_failures = 0
    source.last_error = None
    source.backoff_until = None


def missing_auth_env(source: Source) -> str | None:
    """The unset ``meta.auth_env`` variable an http(s) MCP source needs, else None."""
    if source.kind != "mcp":
        return None
    auth_env = (source.meta or {}).get("auth_env")
    if auth_env and is_http_locator(source.locator) and not os.environ.get(str(auth_env)):
        return str(auth_env)
    return None


def refuses_stdio(source: Source) -> bool:
    """Rule 2: an ``mcp`` command line is spawned only for operator-seeded sources."""
    return source.kind == "mcp" and not is_http_locator(source.locator) and not source.trusted


class PollTimeout(TimeoutError):
    """The fetch did not finish within the poll budget."""


def _poll_timeout_s(source: Source) -> float:
    """The whole-fetch budget: ``meta.poll_timeout_s`` (per-source override), else
    ``Settings.poll_timeout_s``."""
    default = float(get_settings().poll_timeout_s)
    try:
        value = float((source.meta or {}).get("poll_timeout_s") or default)
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else default


async def _fetch_bounded(w: BaseWatcher, source: Source) -> CanonicalDoc:
    limit = _poll_timeout_s(source)
    cm = asyncio.timeout(limit)
    try:
        async with cm:
            return await w.fetch(source_ref_for(source))
    except TimeoutError as e:
        if cm.expired():
            raise PollTimeout(f"fetch did not finish within {limit:.0f}s") from e
        raise


async def poll_source_row(
    session: Session, source: Source, *, watcher: BaseWatcher | None = None
) -> PollResult:
    """Fetch + ingest one source, recording success or failure on the row. Never raises for
    a fetch/ingest failure. Does not commit.

    Refuses without fetching: a source that is not ``pollable`` (pushed / observed), a
    customer stdio MCP command (recorded on the row, which is deactivated: rule 2) and an
    MCP source whose ``auth_env`` is unset (recorded as unconfigured, not as a failure).
    The ingest runs in a SAVEPOINT: a failure after the snapshot was written rolls it back,
    so nothing but the failure is persisted and the session stays usable."""
    base = {"source_id": str(source.id), "kind": source.kind, "locator": source.locator}
    if not source.pollable:
        return PollResult(**base, ok=False, error=f"{source.origin} sources are not polled")
    if refuses_stdio(source):
        source.last_error = STDIO_REFUSED
        source.active = False
        source.backoff_until = None
        log.warning("refusing to spawn %s source %s: %s", source.origin, source.id, source.locator)
        return PollResult(**base, ok=False, error=STDIO_REFUSED)
    unset = missing_auth_env(source)
    if unset:
        msg = f"{UNCONFIGURED_PREFIX}environment variable {unset} is not set"
        source.last_error = msg
        source.consecutive_failures = 0
        source.backoff_until = None
        log.info("skipping %s %s: %s", source.kind, source.locator, msg)
        return PollResult(**base, ok=False, error=msg)
    try:
        w = watcher or get_watcher(source.kind)
        doc = await _fetch_bounded(w, source)
        with session.begin_nested():
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


def effective_interval(source: Source, paid_ids: set[UUID] | frozenset[UUID] = frozenset()) -> int:
    """``poll_interval_s``, or at most an hour for an MCP source a paying org depends on
    (spec: "every hour for sources a paying customer depends on"). Computed at schedule time
    so a downgrade reverts on its own."""
    if source.kind == "mcp" and source.id in paid_ids:
        return min(source.poll_interval_s, PAID_MCP_POLL_INTERVAL_S)
    return source.poll_interval_s


def paid_source_ids(session: Session) -> set[UUID]:
    """Sources a dependency of an active repo of an active paying org points at."""
    return set(
        session.scalars(
            select(Dependency.source_id)
            .join(Repo, Repo.id == Dependency.repo_id)
            .join(Org, Org.id == Repo.org_id)
            .where(
                Dependency.source_id.is_not(None),
                Repo.active.is_(True),
                Org.active.is_(True),
                Org.plan.in_(PAID_PLANS),
            )
            .distinct()
        )
    )


def is_due(
    source: Source,
    now: datetime | None = None,
    *,
    paid_ids: set[UUID] | frozenset[UUID] = frozenset(),
) -> bool:
    now = now or utcnow()
    if not source.active or not source.pollable:
        return False
    if missing_auth_env(source):
        return False  # unconfigured: due again as soon as the worker has the variable
    if source.backoff_until is not None and source.backoff_until > now:
        return False
    if source.last_polled_at is None:
        return True
    return source.last_polled_at + timedelta(seconds=effective_interval(source, paid_ids)) < now


def due_sources(
    session: Session,
    now: datetime | None = None,
    *,
    kinds: Sequence[str] | None = None,
    only_due: bool = True,
) -> list[Source]:
    """Active, pollable sources (all of them with ``only_due=False``)."""
    stmt = (
        select(Source)
        .where(Source.active.is_(True), Source.origin.not_in(NOT_POLLED_ORIGINS))
        .order_by(Source.created_at)
    )
    if kinds:
        stmt = stmt.where(Source.kind.in_(list(kinds)))
    rows = list(session.scalars(stmt))
    if not only_due:
        return rows
    now = now or utcnow()
    paid = paid_source_ids(session)
    return [s for s in rows if is_due(s, now, paid_ids=paid)]


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
    if not source.pollable:
        return {"source_id": str(source_id), "skipped": f"{source.origin} sources are not polled"}
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
    requeued = requeue_stalled(session, now)
    log.info(
        "tick: %d sources due, scheduled=%s, open_issue requeued=%d",
        len(due),
        scheduled or "-",
        requeued,
    )
    return {"polls_enqueued": len(due), "scheduled": scheduled, "issues_requeued": requeued}


def requeue_stalled(session: Session, now: datetime | None = None) -> int:
    """Once per tick: ``kintsua.matcher.match.requeue_stalled_issues`` (re-queues
    ``open_issue`` for draft impacts whose last job ran out of attempts). Runs after the
    tick's own work is committed and never raises: a matcher error is logged and rolled
    back, so it can never stop the scheduler. Returns how many impacts were re-queued."""
    try:
        from kintsua.matcher.match import requeue_stalled_issues

        return len(requeue_stalled_issues(session, now=now))
    except Exception:  # noqa: BLE001 - the tick must go on
        session.rollback()
        log.exception("tick: requeue_stalled_issues failed; skipped this tick")
        return 0


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


def failing_sources(
    session: Session, days: int = FAILING_DAYS, *, public_only: bool = True
) -> list[FailingSource]:
    """Active sources that are failing and whose last success (or creation) is older than
    ``days``: a vanished spec is itself a signal for the weekly report.

    ``public_only`` (the default; the public report) keeps only ``Source.public`` sources,
    so no customer locator or error reaches it. Errors always go through
    ``redact_secrets``. Unconfigured sources (``consecutive_failures == 0``) never count."""
    cutoff = utcnow() - timedelta(days=days)
    rows = session.scalars(
        select(Source)
        .where(Source.active.is_(True), Source.consecutive_failures > 0)
        .order_by(Source.vendor, Source.display_name)
    )
    out: list[FailingSource] = []
    for s in rows:
        if public_only and not s.public:
            continue
        since = s.last_polled_at or s.created_at
        if since is None or since > cutoff:
            continue
        out.append(
            FailingSource(
                display_name=s.display_name,
                kind=s.kind,
                locator=s.locator,
                last_error=redact_secrets(s.last_error) if s.last_error else None,
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
    parser.add_argument(
        "--include-private",
        action="store_true",
        help="operator view: include customer sources and repo links in the report",
    )
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
            report = build_weekly_report(
                session, since, until, public_only=not args.include_private
            )
        finally:
            session.close()
        if do_poll:
            print()
        sys.stdout.write(render_markdown(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
