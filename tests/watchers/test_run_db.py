"""Postgres-backed tests for the watcher plumbing: sources, ingest, poll backoff, tick."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from kintsua.core.events import CanonicalDoc
from kintsua.core.models import ChangeEventRow, Job, Snapshot, Source
from kintsua.core.queue import NonRetryable
from kintsua.core.util import utcnow
from kintsua.watchers.base import SourceRef
from kintsua.watchers.mcp import MCPWatcher
from kintsua.watchers.run import (
    billing_tick_key,
    due_sources,
    ensure_tick_job,
    failing_sources,
    find_or_create_source,
    ingest_canonical_doc,
    is_due,
    latest_snapshot,
    poll_all,
    poll_source,
    source_ref_for,
    tick,
    weekly_report_key,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "watchers"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text("utf-8"))


def mcp_doc(name: str) -> CanonicalDoc:
    body = load(name)
    return CanonicalDoc(version=body["server"]["version"], body=body)


class ScriptedMCP(MCPWatcher):
    """An MCP watcher whose fetch returns scripted documents (or raises)."""

    def __init__(self, *items):
        self.items = list(items)

    async def fetch(self, source: SourceRef) -> CanonicalDoc:
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# --------------------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------------------


def test_find_or_create_source_is_unique_and_applies_spec_intervals(session):
    s = find_or_create_source(session, "mcp", "https://x.example/mcp", display_name="X", vendor="x")
    assert s.id is not None and s.poll_interval_s == 6 * 3600 and s.active
    again = find_or_create_source(session, "mcp", "https://x.example/mcp", meta={"extra": 1})
    assert again.id == s.id and again.display_name == "X" and again.meta == {"extra": 1}
    assert count(session, Source) == 1
    paid = find_or_create_source(session, "mcp", "https://paid.example/mcp", meta={"paid": True})
    assert paid.poll_interval_s == 3600
    assert (
        find_or_create_source(session, "openapi", "https://x/spec.json").poll_interval_s
        == 24 * 3600
    )
    assert (
        find_or_create_source(session, "changelog", "https://x/changelog").poll_interval_s
        == 24 * 3600
    )
    assert find_or_create_source(session, "pypi", "openai").poll_interval_s == 6 * 3600
    assert (
        find_or_create_source(session, "npm", "openai", poll_interval_s=900).poll_interval_s == 900
    )
    with pytest.raises(ValueError):
        find_or_create_source(session, "bogus", "x")
    with pytest.raises(ValueError):
        find_or_create_source(session, "mcp", "   ")


def test_source_ref_for_carries_meta_and_user_agent(session, settings):
    s = find_or_create_source(
        session, "openapi", "https://x/spec.json", vendor="x", meta={"format": "discovery"}
    )
    ref = source_ref_for(s)
    assert ref.id == s.id and ref.kind == "openapi" and ref.vendor == "x"
    assert ref.meta["format"] == "discovery" and ref.meta["user_agent"] == settings.user_agent


# --------------------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------------------


def test_ingest_first_sight_change_and_unchanged(session):
    src = find_or_create_source(session, "mcp", "fake://weather", display_name="Weather")
    before, after = mcp_doc("mcp_before.json"), mcp_doc("mcp_after.json")
    w = MCPWatcher()

    # first sight: snapshot only
    assert ingest_canonical_doc(session, src, before, watcher=w) is None
    assert count(session, Snapshot) == 1 and count(session, ChangeEventRow) == 0
    assert src.last_polled_at is not None
    first_polled = src.last_polled_at

    # a change: snapshot + change_event + classify job
    row = ingest_canonical_doc(session, src, after, watcher=w)
    assert row is not None
    assert count(session, Snapshot) == 2 and count(session, ChangeEventRow) == 1
    assert row.from_snapshot_id != row.to_snapshot_id
    assert row.diff["source_kind"] == "mcp"
    assert row.diff["from_version"] == "1.4.0" and row.diff["to_version"] == "2.0.0"
    assert row.diff["raw_diff_ref"] == "inline"
    assert any(op["op"] == "required_added" for op in row.diff["ops"])
    assert "get_weather:" in row.diff["text"]
    job = session.scalar(select(Job).where(Job.kind == "classify_event"))
    assert (
        job is not None
        and job.payload == {"change_event_id": str(row.id)}
        and job.status == "queued"
    )
    assert job.dedupe_key == f"classify_event:{row.id}"

    # unchanged content: only last_polled_at moves
    assert ingest_canonical_doc(session, src, after, watcher=w) is None
    assert count(session, Snapshot) == 2 and count(session, ChangeEventRow) == 1
    assert count(session, Job) == 1
    assert src.last_polled_at >= first_polled

    rec = latest_snapshot(session, src.id)
    assert rec is not None and rec.snapshot_id == row.to_snapshot_id
    assert (
        rec.content_hash == after.content_hash()
        and rec.body == after.body
        and rec.version == "2.0.0"
    )
    assert latest_snapshot(session, uuid4()) is None


def test_ingest_uses_watcher_event_text_hook_and_explicit_text_wins(session):
    src = find_or_create_source(session, "mcp", "fake://hook")
    before, after = mcp_doc("mcp_before.json"), mcp_doc("mcp_after.json")
    ingest_canonical_doc(session, src, before)
    row = ingest_canonical_doc(session, src, after, text="explicit notes")
    assert row.diff["text"] == "explicit notes"


def test_ingest_large_diff_is_stored_out_of_row(session, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    src = find_or_create_source(session, "mcp", "fake://big")

    def body(suffix: str) -> dict:
        tools = {
            f"tool_{i:04d}": {
                "description": f"Tool number {i} does something {suffix} " + "x" * 120,
                "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
            }
            for i in range(3000)
        }
        return {
            "server": {"name": "big", "version": suffix},
            "tools": tools,
            "resources": {},
            "prompts": {},
        }

    assert ingest_canonical_doc(session, src, CanonicalDoc(version="1", body=body("v1"))) is None
    row = ingest_canonical_doc(session, src, CanonicalDoc(version="2", body=body("v2")))
    assert row is not None
    assert row.diff["ops_truncated"] is True and row.diff["ops_total"] == 3000
    assert len(row.diff["ops"]) == 200
    assert row.diff["raw_diff_ref"].startswith("file://")
    full = json.loads(Path(row.diff["raw_diff_ref"][len("file://") :]).read_text("utf-8"))
    assert len(full["ops"]) == 3000 and full["to_version"] == "2"
    snap = session.get(Snapshot, row.to_snapshot_id)
    assert snap.storage_ref["kind"] == "file"


# --------------------------------------------------------------------------------------
# poll_source
# --------------------------------------------------------------------------------------


async def test_poll_source_records_failures_with_backoff_then_recovers(
    session, settings, monkeypatch
):
    src = find_or_create_source(session, "mcp", "fake://flaky", poll_interval_s=600)
    session.commit()
    monkeypatch.setattr(
        "kintsua.watchers.run.get_watcher",
        lambda kind: ScriptedMCP(RuntimeError("connection refused")),
    )

    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["ok"] is False and "connection refused" in res["error"] and res["backoff_s"] == 1200
    session.refresh(src)
    assert src.consecutive_failures == 1
    assert src.last_error.startswith("RuntimeError: connection refused")
    assert src.last_polled_at is None
    assert src.backoff_until > utcnow() + timedelta(seconds=1200 - 10)

    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["backoff_s"] == 2400
    session.refresh(src)
    assert src.consecutive_failures == 2

    monkeypatch.setattr(settings, "source_backoff_ceiling_s", 1000)
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["backoff_s"] == 1000
    session.refresh(src)
    assert src.consecutive_failures == 3 and src.backoff_until <= utcnow() + timedelta(seconds=1001)
    assert not is_due(src)

    monkeypatch.setattr(
        "kintsua.watchers.run.get_watcher", lambda kind: ScriptedMCP(mcp_doc("mcp_before.json"))
    )
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["ok"] is True and res["changed"] is False and res["version"] == "1.4.0"
    session.refresh(src)
    assert src.consecutive_failures == 0 and src.last_error is None and src.backoff_until is None
    assert src.last_polled_at is not None
    assert count(session, Snapshot) == 1


async def test_poll_source_emits_change_event_on_second_poll(session, monkeypatch):
    src = find_or_create_source(session, "mcp", "fake://changing")
    session.commit()
    scripted = ScriptedMCP(mcp_doc("mcp_before.json"), mcp_doc("mcp_after.json"))
    monkeypatch.setattr("kintsua.watchers.run.get_watcher", lambda kind: scripted)
    first = await poll_source(session, {"source_id": str(src.id)})
    assert first["changed"] is False
    second = await poll_source(session, {"source_id": str(src.id)})
    assert second["changed"] is True and second["change_event_id"]
    assert session.get(ChangeEventRow, second["change_event_id"]) is not None


async def test_poll_source_rejects_missing_or_bad_ids(session):
    with pytest.raises(NonRetryable):
        await poll_source(session, {"source_id": str(uuid4())})
    with pytest.raises(NonRetryable):
        await poll_source(session, {"source_id": "not-a-uuid"})
    src = find_or_create_source(session, "mcp", "fake://inactive")
    src.active = False
    assert (await poll_source(session, {"source_id": str(src.id)}))["skipped"] == "inactive"


# --------------------------------------------------------------------------------------
# tick / scheduling
# --------------------------------------------------------------------------------------


def _source(session, locator, **kw):
    s = find_or_create_source(session, "mcp", locator, poll_interval_s=kw.pop("interval", 3600))
    for k, v in kw.items():
        setattr(s, k, v)
    session.flush()
    return s


def test_due_sources_selection(session):
    now = utcnow()
    never = _source(session, "fake://never")
    recent = _source(session, "fake://recent", last_polled_at=now - timedelta(minutes=5))
    stale = _source(session, "fake://stale", last_polled_at=now - timedelta(hours=2))
    backoff = _source(session, "fake://backoff", backoff_until=now + timedelta(hours=1))
    expired_backoff = _source(session, "fake://expired", backoff_until=now - timedelta(minutes=1))
    inactive = _source(session, "fake://inactive", active=False)
    due = {s.locator for s in due_sources(session, now)}
    assert due == {never.locator, stale.locator, expired_backoff.locator}
    assert not is_due(recent, now) and not is_due(backoff, now) and not is_due(inactive, now)
    assert {s.locator for s in due_sources(session, now, only_due=False)} == {
        never.locator,
        recent.locator,
        stale.locator,
        backoff.locator,
        expired_backoff.locator,
    }
    assert due_sources(session, now, kinds=["openapi"]) == []


def test_tick_enqueues_due_polls_and_reschedules_itself(session, settings):
    now = utcnow()
    due = _source(session, "fake://due")
    _source(session, "fake://fresh", last_polled_at=now)
    _source(session, "fake://backoff", backoff_until=now + timedelta(hours=1))
    session.commit()

    out = tick(session, {})
    assert out["polls_enqueued"] == 1
    polls = session.scalars(
        select(Job).where(Job.kind == "poll_source", Job.status == "queued")
    ).all()
    assert len(polls) == 1 and polls[0].payload == {"source_id": str(due.id)}
    assert polls[0].dedupe_key == f"poll_source:{due.id}"
    ticks = session.scalars(select(Job).where(Job.kind == "tick", Job.status == "queued")).all()
    assert len(ticks) == 1 and ticks[0].dedupe_key == "tick"
    assert (
        abs(
            (
                ticks[0].run_after - (utcnow() + timedelta(seconds=settings.tick_interval_s))
            ).total_seconds()
        )
        < 5
    )

    # a second tick while the first jobs are still queued does not duplicate anything
    tick(session, {})
    periodic = session.scalar(
        select(func.count()).select_from(Job).where(Job.kind.in_(["poll_source", "tick"]))
    )
    assert periodic == 2  # (billing_tick / weekly_report may also be queued on the 1st / a Monday)


def test_tick_schedules_weekly_report_and_billing_once(session, monkeypatch):
    monday_first = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    assert monday_first.weekday() == 0 and monday_first.day == 1
    monkeypatch.setattr("kintsua.watchers.run.utcnow", lambda: monday_first)
    out = tick(session, {})
    assert out["scheduled"] == [weekly_report_key(monday_first), billing_tick_key(monday_first)]
    assert weekly_report_key(monday_first) == "weekly_report:2026-W23"
    assert billing_tick_key(monday_first) == "billing_tick:2026-06"
    kinds = {j.kind for j in session.scalars(select(Job))}
    assert kinds == {"weekly_report", "billing_tick", "tick"}
    # later ticks the same day (even after the jobs ran) do not schedule them again
    for j in session.scalars(select(Job).where(Job.kind.in_(["weekly_report", "billing_tick"]))):
        j.status = "done"
    session.flush()
    assert tick(session, {})["scheduled"] == []
    # a plain Tuesday schedules nothing periodic
    monkeypatch.setattr(
        "kintsua.watchers.run.utcnow", lambda: datetime(2026, 6, 2, 9, 0, tzinfo=UTC)
    )
    assert tick(session, {})["scheduled"] == []


def test_ensure_tick_job_is_idempotent(session):
    ensure_tick_job(session)
    ensure_tick_job(session)
    ticks = session.scalars(select(Job).where(Job.kind == "tick")).all()
    assert len(ticks) == 1 and ticks[0].status == "queued" and ticks[0].dedupe_key == "tick"


# --------------------------------------------------------------------------------------
# poll_all / failing sources
# --------------------------------------------------------------------------------------


async def test_poll_all_runs_each_source_in_its_own_session(session, db, monkeypatch):
    from kintsua.core.db import new_session

    good = find_or_create_source(session, "mcp", "fake://good")
    bad = find_or_create_source(session, "mcp", "fake://bad")
    find_or_create_source(session, "pypi", "notpolled")
    session.commit()

    class ByLocator(MCPWatcher):
        async def fetch(self, source: SourceRef) -> CanonicalDoc:
            if source.locator == "fake://bad":
                raise RuntimeError("nope")
            return mcp_doc("mcp_before.json")

    monkeypatch.setattr("kintsua.watchers.run.get_watcher", lambda kind: ByLocator())
    results = await poll_all(new_session, kinds=["mcp"], only_due=True, concurrency=2)
    assert {r.source_id for r in results} == {str(good.id), str(bad.id)}
    assert sum(1 for r in results if r.ok) == 1 and sum(1 for r in results if not r.ok) == 1
    s2 = new_session()
    try:
        rows = {s.locator: s for s in s2.scalars(select(Source))}
        oks = [
            s
            for s in rows.values()
            if s.kind == "mcp" and s.consecutive_failures == 0 and s.last_polled_at
        ]
        fails = [s for s in rows.values() if s.consecutive_failures == 1]
        assert len(oks) == 1 and len(fails) == 1
    finally:
        s2.close()
    # explicit ids bypass the due filter
    results = await poll_all(new_session, source_ids=[str(good.id)], concurrency=1)
    assert len(results) == 1 and results[0].source_id == str(good.id)


def test_failing_sources_flags_only_week_old_failures(session):
    now = utcnow()
    old = _source(
        session,
        "fake://old",
        consecutive_failures=3,
        last_error="boom",
        last_polled_at=now - timedelta(days=8),
    )
    _source(
        session,
        "fake://fresh-fail",
        consecutive_failures=1,
        last_error="x",
        last_polled_at=now - timedelta(days=1),
    )
    never = _source(session, "fake://never-worked", consecutive_failures=5, last_error="dead")
    never.created_at = now - timedelta(days=9)
    _source(session, "fake://inactive", consecutive_failures=9, active=False)
    _source(session, "fake://healthy", last_polled_at=now - timedelta(days=30))
    session.flush()
    failing = failing_sources(session)
    assert {f.locator for f in failing} == {old.locator, never.locator}
    f = next(f for f in failing if f.locator == old.locator)
    assert f.kind == "mcp" and f.last_error == "boom" and f.consecutive_failures == 3
    assert f.failing_since == old.last_polled_at
    assert failing_sources(session, days=30) == []
