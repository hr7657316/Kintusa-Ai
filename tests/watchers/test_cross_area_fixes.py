"""Regression tests for the cross-area follow-ups owned by the watcher (Group A): the tick
re-queues stalled ``open_issue`` jobs (A1), backoff / poll budget / per-request timeout come
from Settings (A2), the seed loader revives a promoted row (A3) and ``find_or_create_source``
never hands out another caller's pushed or observed source (A4)."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
import respx
from sqlalchemy import select

from kintsua.core.events import CanonicalDoc
from kintsua.core.models import ChangeEventRow, Impact, Job, Org, Repo, Snapshot, Source
from kintsua.core.util import utcnow
from kintsua.seed.load import SeedEntry, upsert_sources
from kintsua.watchers.base import SourceRef
from kintsua.watchers.mcp import MCPWatcher
from kintsua.watchers.run import (
    STDIO_REFUSED,
    SourceNotShareable,
    find_or_create_source,
    is_due,
    poll_source,
    source_ref_for,
    tick,
)

# --------------------------------------------------------------------------------------
# A1: the tick re-queues open_issue jobs that ran out of attempts
# --------------------------------------------------------------------------------------


def _stalled_impact(session) -> Impact:
    """A draft impact whose ``open_issue`` job gave up two hours ago (a GitHub outage)."""
    org = Org(github_installation_id=4101, name="acme", settings={"auto_issue": True})
    session.add(org)
    session.flush()
    repo = Repo(org_id=org.id, github_repo_id=4102, full_name="acme/agent")
    src = Source(
        kind="openapi",
        locator="https://spec.vendor.example/a.json",
        display_name="A",
        origin="seed",
    )
    session.add_all([repo, src])
    session.flush()
    snaps = [
        Snapshot(
            source_id=src.id,
            content_hash=c * 64,
            version=c,
            storage_ref={"kind": "inline", "body": {}},
        )
        for c in ("a", "b")
    ]
    session.add_all(snaps)
    session.flush()
    event = ChangeEventRow(
        source_id=src.id,
        from_snapshot_id=snaps[0].id,
        to_snapshot_id=snaps[1].id,
        diff={"source_kind": "openapi", "ops": []},
        detected_at=utcnow(),
    )
    session.add(event)
    session.flush()
    impact = Impact(repo_id=repo.id, change_event_id=event.id, status="detected")
    session.add(impact)
    session.flush()
    session.add(
        Job(
            kind="open_issue",
            payload={"impact_id": str(impact.id)},
            status="failed",
            attempts=24,
            max_attempts=24,
            finished_at=utcnow() - timedelta(hours=2),
            last_error="GitHub 502",
            dedupe_key=f"open_issue:{impact.id}",
        )
    )
    session.commit()
    return impact


def _queued_open_issue(session) -> list[Job]:
    return session.scalars(
        select(Job).where(Job.kind == "open_issue", Job.status == "queued")
    ).all()


def test_tick_requeues_stalled_open_issue_jobs(session):
    impact = _stalled_impact(session)
    out = tick(session, {})
    assert out["issues_requeued"] == 1
    (job,) = _queued_open_issue(session)
    assert job.payload == {"impact_id": str(impact.id)}
    # the next tick finds a queued job and leaves it alone
    assert tick(session, {})["issues_requeued"] == 0
    assert len(_queued_open_issue(session)) == 1


def test_a_matcher_error_never_stops_the_tick(session, monkeypatch, caplog):
    due = find_or_create_source(session, "mcp", "https://due.vendor.example/mcp", origin="seed")
    session.commit()

    def boom(session, *, now=None):
        session.execute(select(Job.id).limit(1))
        raise RuntimeError("matcher exploded")

    monkeypatch.setattr("kintsua.matcher.match.requeue_stalled_issues", boom)
    with caplog.at_level(logging.ERROR, logger="kintsua.watchers.run"):
        out = tick(session, {})
    assert out["polls_enqueued"] == 1 and out["issues_requeued"] == 0
    assert any("requeue_stalled_issues failed" in r.getMessage() for r in caplog.records)
    # the tick's own work was committed before, and the session is still usable
    session.expire_all()
    kinds = {(j.kind, j.status) for j in session.scalars(select(Job))}
    assert ("tick", "queued") in kinds and ("poll_source", "queued") in kinds
    poll = session.scalar(select(Job).where(Job.kind == "poll_source"))
    assert poll.payload == {"source_id": str(due.id)}


# --------------------------------------------------------------------------------------
# A2: backoff base, poll budget and the per-request HTTP timeout are Settings
# --------------------------------------------------------------------------------------


class _Failing(MCPWatcher):
    async def fetch(self, source: SourceRef) -> CanonicalDoc:
        raise RuntimeError("503 from upstream")


async def test_backoff_base_comes_from_settings(session, settings, monkeypatch):
    monkeypatch.setattr(settings, "source_backoff_base_s", 60)
    src = find_or_create_source(
        session, "openapi", "https://spec.vendor.example/openapi.json", origin="seed"
    )
    session.commit()
    monkeypatch.setattr("kintsua.watchers.run.get_watcher", lambda kind: _Failing())
    assert (await poll_source(session, {"source_id": str(src.id)}))["backoff_s"] == 60
    assert (await poll_source(session, {"source_id": str(src.id)}))["backoff_s"] == 120
    monkeypatch.setattr(settings, "source_backoff_ceiling_s", 150)
    assert (await poll_source(session, {"source_id": str(src.id)}))["backoff_s"] == 150


async def test_poll_budget_comes_from_settings_and_meta_still_overrides(
    session, settings, monkeypatch
):
    import asyncio

    class Slow(MCPWatcher):
        async def fetch(self, source: SourceRef) -> CanonicalDoc:
            await asyncio.sleep(0.5)
            return CanonicalDoc(version="1", body={"server": {}, "tools": []})

    monkeypatch.setattr(settings, "poll_timeout_s", 0.1)
    monkeypatch.setattr("kintsua.watchers.run.get_watcher", lambda kind: Slow())
    src = find_or_create_source(session, "mcp", "https://slow.vendor.example/mcp", origin="seed")
    session.commit()
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["ok"] is False and res["error"].startswith("PollTimeout")
    assert "within 0s" in res["error"]  # the settings budget, not the 10 min default

    src.meta = {"poll_timeout_s": 5}  # the per-source override wins
    session.commit()
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["ok"] is True


def test_source_ref_carries_the_per_request_http_timeout(session, settings, monkeypatch):
    monkeypatch.setattr(settings, "poll_http_timeout_s", 7.5)
    src = find_or_create_source(session, "openapi", "https://spec.vendor.example/a.json")
    assert source_ref_for(src).meta["timeout_s"] == 7.5
    assert "timeout_s" not in (src.meta or {})  # a runtime default, never stored
    src.meta = {"timeout_s": 3}
    assert source_ref_for(src).meta["timeout_s"] == 3  # meta.timeout_s overrides


@respx.mock
async def test_adapters_use_settings_poll_http_timeout(session, settings, monkeypatch):
    monkeypatch.setattr(settings, "poll_http_timeout_s", 7.5)
    url = "https://spec.vendor.example/openapi.json"
    seen: list[dict] = []
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Vendor", "version": "1"},
        "paths": {"/x": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.extensions.get("timeout") or {}))
        return httpx.Response(
            200, content=json.dumps(spec), headers={"content-type": "application/json"}
        )

    respx.get(url).mock(side_effect=handler)
    src = find_or_create_source(session, "openapi", url, origin="seed")
    session.commit()
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["ok"] is True, res
    assert seen and all(t.get("read") == 7.5 for t in seen)


# --------------------------------------------------------------------------------------
# A3: seeding a row the worker deactivated brings it back
# --------------------------------------------------------------------------------------


async def test_seeding_a_refused_customer_stdio_locator_reactivates_it(session):
    locator = "npx -y @vendor/weather-mcp"
    src = find_or_create_source(session, "mcp", locator, meta={"transport": "stdio"})
    session.commit()
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["error"] == STDIO_REFUSED
    session.refresh(src)
    assert src.active is False and src.last_error == STDIO_REFUSED
    src.consecutive_failures = 2
    src.backoff_until = utcnow() + timedelta(hours=3)
    session.commit()

    entry = SeedEntry(
        kind="mcp", locator=locator, display_name="Weather", meta={"transport": "stdio"}
    )
    result = upsert_sources(session, [entry])
    assert result.updated == [f"mcp:{locator}"]
    session.commit()
    session.refresh(src)
    assert src.origin == "seed" and src.trusted
    assert src.active is True and src.last_error is None
    assert src.consecutive_failures == 0 and src.backoff_until is None
    assert is_due(src)
    # re-seeding an operator row never reactivates one the operator switched off
    src.active = False
    session.commit()
    assert upsert_sources(session, [entry]).unchanged == [f"mcp:{locator}"]
    session.refresh(src)
    assert src.active is False


# --------------------------------------------------------------------------------------
# A4: pushed / observed / org-owned rows are never handed to another caller
# --------------------------------------------------------------------------------------


def _org(session, n: int) -> Org:
    org = Org(github_installation_id=n, name=f"org{n}", settings={})
    session.add(org)
    session.flush()
    return org


def test_find_or_create_source_never_shares_another_orgs_pushed_source(session):
    owner, other = _org(session, 5101), _org(session, 5102)
    locator = f"push:{owner.id}:mcp:https://mcp.weather.example/mcp"
    pushed = find_or_create_source(
        session,
        "mcp",
        locator,
        meta={"pushed_locator": "https://mcp.weather.example/mcp"},
        origin="push",
        owner_org_id=owner.id,
    )
    session.commit()
    before = dict(pushed.meta)

    # a repo's .mcp.json naming it (indexer, customer origin), the curated map (seed), runtime
    for origin in ("customer", "seed", "runtime"):
        with pytest.raises(SourceNotShareable) as exc:
            find_or_create_source(
                session, "mcp", locator, display_name="x", meta={"evil": 1}, origin=origin
            )
        assert isinstance(exc.value, ValueError)
        assert exc.value.source_id == pushed.id and exc.value.source_origin == "push"
    # another org's push under the same locator
    with pytest.raises(SourceNotShareable):
        find_or_create_source(session, "mcp", locator, origin="push", owner_org_id=other.id)
    with pytest.raises(SourceNotShareable):
        find_or_create_source(session, "mcp", locator, origin="push")
    session.refresh(pushed)
    assert pushed.meta == before and pushed.display_name == locator

    # the owning org's own push gets its row (as a str id too)
    assert (
        find_or_create_source(session, "mcp", locator, origin="push", owner_org_id=owner.id).id
        == pushed.id
    )
    assert (
        find_or_create_source(session, "mcp", locator, origin="push", owner_org_id=str(owner.id)).id
        == pushed.id
    )


def test_find_or_create_source_never_shares_observed_or_org_owned_rows(session):
    observed = Source(
        kind="mcp", locator="observed:acme/agent:get_x", display_name="get_x", origin="observed"
    )
    session.add(observed)
    session.flush()
    with pytest.raises(SourceNotShareable):
        find_or_create_source(session, "mcp", observed.locator)
    assert find_or_create_source(session, "mcp", observed.locator, origin="observed").id == (
        observed.id
    )

    org = _org(session, 5201)
    owned = find_or_create_source(
        session, "openapi", "https://x.example/private.json", owner_org_id=org.id
    )
    session.flush()
    with pytest.raises(SourceNotShareable):
        find_or_create_source(session, "openapi", owned.locator)
    with pytest.raises(SourceNotShareable):
        find_or_create_source(session, "openapi", owned.locator, owner_org_id=uuid4())
    assert find_or_create_source(session, "openapi", owned.locator, owner_org_id=org.id).id == (
        owned.id
    )

    # shared rows (customer, runtime, seed) are still found by anyone
    shared = find_or_create_source(session, "mcp", "https://shared.example/mcp", origin="runtime")
    assert find_or_create_source(session, "mcp", shared.locator).id == shared.id
