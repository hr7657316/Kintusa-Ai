"""Postgres-backed regression tests for ingest: atomic polls (a failed ingest persists
nothing), concurrent ingests, truncated diff previews, version-only changes, long locators."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from sqlalchemy import func, select

from kintsua.classify.rules import classify_by_rules
from kintsua.classify.run import build_change_event
from kintsua.core.events import CanonicalDoc
from kintsua.core.models import ChangeEventRow, Job, Snapshot, Source
from kintsua.watchers.base import SourceRef
from kintsua.watchers.mcp import MCPWatcher
from kintsua.watchers.run import find_or_create_source, ingest_canonical_doc, poll_source

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "watchers"


def mcp_doc(name: str) -> CanonicalDoc:
    body = json.loads((FIX / name).read_text("utf-8"))
    return CanonicalDoc(version=body["server"]["version"], body=body)


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


class Scripted(MCPWatcher):
    def __init__(self, *docs):
        self.docs = list(docs)

    async def fetch(self, source: SourceRef) -> CanonicalDoc:
        return self.docs.pop(0) if len(self.docs) > 1 else self.docs[0]


def _seeded(session, locator: str) -> Source:
    src = find_or_create_source(session, "mcp", locator, origin="seed")
    session.commit()
    return src


# --------------------------------------------------------------------------------------
# watchers-correctness-1
# --------------------------------------------------------------------------------------


async def test_unreadable_previous_body_persists_nothing_and_the_change_is_not_lost(
    session, monkeypatch
):
    src = _seeded(session, "fake://atomic")
    w = Scripted(mcp_doc("mcp_before.json"), mcp_doc("mcp_after.json"), mcp_doc("mcp_after.json"))
    monkeypatch.setattr("kintsua.watchers.run.get_watcher", lambda kind: w)
    assert (await poll_source(session, {"source_id": str(src.id)}))["ok"] is True

    def unreadable(ref):
        raise FileNotFoundError("snapshot file is on the other worker's volume")

    monkeypatch.setattr("kintsua.watchers.run.get_document", unreadable)
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["ok"] is False and "FileNotFoundError" in res["error"]
    session.refresh(src)
    assert count(session, Snapshot) == 1 and src.consecutive_failures == 1

    monkeypatch.undo()
    monkeypatch.setattr("kintsua.watchers.run.get_watcher", lambda kind: w)
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["ok"] is True and res["changed"] is True
    assert count(session, ChangeEventRow) == 1 and count(session, Snapshot) == 2


async def test_a_failure_after_the_snapshot_flush_rolls_the_snapshot_back(session, monkeypatch):
    src = _seeded(session, "fake://diffstore")
    w = Scripted(mcp_doc("mcp_before.json"), mcp_doc("mcp_after.json"), mcp_doc("mcp_after.json"))
    monkeypatch.setattr("kintsua.watchers.run.get_watcher", lambda kind: w)
    await poll_source(session, {"source_id": str(src.id)})

    def s3_down(event):
        raise ConnectionError("S3 unavailable")

    monkeypatch.setattr("kintsua.watchers.run._store_diff", s3_down)
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["ok"] is False and "S3 unavailable" in res["error"]
    assert count(session, Snapshot) == 1 and count(session, Job) == 0

    monkeypatch.undo()
    monkeypatch.setattr("kintsua.watchers.run.get_watcher", lambda kind: w)
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["changed"] is True and count(session, ChangeEventRow) == 1


async def test_a_failed_flush_is_recorded_and_the_job_does_not_crash(session, monkeypatch):
    src = _seeded(session, "fake://nul")
    doc = mcp_doc("mcp_before.json")
    doc.body["tools"]["stable_tool"]["description"] = "bad \x00 byte"  # jsonb rejects NUL
    monkeypatch.setattr("kintsua.watchers.run.get_watcher", lambda kind: Scripted(doc))
    res = await poll_source(session, {"source_id": str(src.id)})  # must not raise
    assert res["ok"] is False
    session.refresh(src)
    assert src.consecutive_failures == 1 and src.last_error
    assert count(session, Snapshot) == 0


# --------------------------------------------------------------------------------------
# watchers-correctness-7
# --------------------------------------------------------------------------------------


def test_concurrent_ingests_of_one_document_emit_one_event(session, db):
    from kintsua.core.db import new_session

    src = find_or_create_source(session, "mcp", "fake://race")
    ingest_canonical_doc(session, src, mcp_doc("mcp_before.json"))
    session.commit()
    after = mcp_doc("mcp_after.json")

    s_a, s_b = new_session(), new_session()
    out: dict[str, object] = {}
    try:
        row_a = ingest_canonical_doc(s_a, s_a.get(Source, src.id), after)
        assert row_a is not None  # A holds the source row lock, uncommitted

        def second() -> None:
            out["b"] = ingest_canonical_doc(s_b, s_b.get(Source, src.id), after)
            s_b.commit()

        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.5)
        assert t.is_alive()  # B waits for A's lock
        s_a.commit()
        t.join(timeout=20)
        assert not t.is_alive()
    finally:
        s_a.close()
        s_b.close()
    assert out["b"] is None
    session.expire_all()
    assert count(session, ChangeEventRow) == 1 and count(session, Snapshot) == 2
    assert (
        session.scalar(select(func.count()).select_from(Job).where(Job.kind == "classify_event"))
        == 1
    )


# --------------------------------------------------------------------------------------
# watchers-correctness-6
# --------------------------------------------------------------------------------------


def test_truncated_preview_keeps_structural_ops_and_a_storage_ref(
    session, settings, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    src = find_or_create_source(session, "mcp", "fake://big-removal")

    def body(suffix: str, with_removed: bool) -> dict:
        tools = {
            f"tool_{i:04d}": {
                "description": f"Tool {i} does something {suffix} " + "x" * 120,
                "inputSchema": {"type": "object"},
            }
            for i in range(1200)
        }
        if with_removed:  # sorts after every tool_* path
            tools["zzz_delete_charge"] = {
                "description": "Delete",
                "inputSchema": {"type": "object"},
            }
        return {"server": {"name": "big", "version": suffix}, "tools": tools}

    ingest_canonical_doc(session, src, CanonicalDoc(version="1", body=body("v1", True)))
    row = ingest_canonical_doc(session, src, CanonicalDoc(version="2", body=body("v2", False)))
    assert row is not None and row.diff["ops_truncated"] is True
    assert row.diff["ops_total"] == 1201 and len(row.diff["ops"]) == 200
    assert row.diff["ops"][0] == {
        "op": "removed",
        "path": "/tools/zzz_delete_charge",
        "before": row.diff["ops"][0]["before"],
        "after": None,
    }
    assert row.diff["storage_ref"]["kind"] == "file"
    cls = classify_by_rules(build_change_event(row))
    assert cls is not None and cls.severity == "breaking" and cls.category == "removal"


# --------------------------------------------------------------------------------------
# watchers-correctness-9 / -10
# --------------------------------------------------------------------------------------


def test_version_only_change_stores_a_snapshot_and_no_event(session):
    src = find_or_create_source(session, "mcp", "fake://version-bump")
    before = mcp_doc("mcp_before.json")
    bumped = json.loads(json.dumps(before.body))
    bumped["server"]["version"] = "1.4.1"
    ingest_canonical_doc(session, src, before)
    assert ingest_canonical_doc(session, src, CanonicalDoc(version="1.4.1", body=bumped)) is None
    assert count(session, Snapshot) == 2 and count(session, ChangeEventRow) == 0


def test_long_stdio_locator_creates_a_source(session):
    locator = (
        "python -m my_agent.mcp_server --config /home/ci/runner/_work/very/long/path/" + "x" * 230
    )
    src = find_or_create_source(session, "mcp", locator, meta={"transport": "stdio"})
    session.commit()
    assert len(locator) > 255 and session.get(Source, src.id).display_name == locator
