"""Source trust rules in the watcher (docs/CONTRACTS.md, "Source trust, privacy and the
network guard"): no customer stdio command is ever spawned, non-seed fetches go through the
network guard, pushed/observed sources are never polled; plus scheduling fixes (paid hourly
polling, backoff base, unconfigured OAuth sources, the poll time budget)."""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
import respx
from sqlalchemy import select

from kintsua.core.events import CanonicalDoc
from kintsua.core.models import Dependency, Job, Org, Repo, Source
from kintsua.core.netguard import BlockedURL
from kintsua.core.util import utcnow
from kintsua.watchers import packages as pkgmod
from kintsua.watchers._mcp_compat import open_session
from kintsua.watchers.base import SourceRef
from kintsua.watchers.changelog import ChangelogWatcher, clear_robots_cache
from kintsua.watchers.mcp import MCPWatcher
from kintsua.watchers.openapi import fetch_spec
from kintsua.watchers.run import (
    STDIO_REFUSED,
    due_sources,
    failing_sources,
    find_or_create_source,
    is_due,
    poll_all,
    poll_source,
    source_ref_for,
    tick,
)

GUARD = {"_net_guard": True}


# --------------------------------------------------------------------------------------
# rule 2 (watchers-security-1): customer command lines are never executed
# --------------------------------------------------------------------------------------


def _marker_command(tmp_path) -> tuple[str, object]:
    marker = tmp_path / "pwned"
    code = f"open({str(marker)!r}, 'w').write('x')"
    return f'{sys.executable} -c "{code}"', marker


async def test_customer_stdio_source_is_refused_and_deactivated(session, tmp_path):
    locator, marker = _marker_command(tmp_path)
    src = find_or_create_source(session, "mcp", locator, meta={"transport": "stdio"})
    session.commit()
    assert src.origin == "customer"

    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["ok"] is False and res["error"] == STDIO_REFUSED
    assert not marker.exists()  # nothing was spawned
    session.refresh(src)
    assert src.active is False and src.last_error == STDIO_REFUSED
    assert src.consecutive_failures == 0  # a refusal, not an outage

    # poll_all with an explicit id goes through the same gate
    from kintsua.core.db import new_session

    src.active = True
    session.commit()
    results = await poll_all(new_session, source_ids=[str(src.id)])
    assert results[0].ok is False and results[0].error == STDIO_REFUSED
    assert not marker.exists()


async def test_guarded_mcp_open_never_spawns_and_checks_urls(tmp_path):
    locator, marker = _marker_command(tmp_path)
    with pytest.raises(BlockedURL):
        async with open_session(locator, GUARD):
            pass
    assert not marker.exists()
    with pytest.raises(BlockedURL):
        async with open_session("http://127.0.0.1:9/mcp", GUARD):
            pass
    with pytest.raises(BlockedURL):
        async with open_session("http://169.254.169.254/mcp", {**GUARD, "transport": "sse"}):
            pass


async def test_push_and_observed_sources_are_never_polled(session, monkeypatch):
    pushed = find_or_create_source(session, "mcp", f"push:{uuid4()}:mcp:x", origin="push")
    observed = find_or_create_source(session, "mcp", "observed:acme/x:tool", origin="observed")
    session.commit()
    assert {s.id for s in due_sources(session)} == set()
    assert {s.id for s in due_sources(session, only_due=False)} == set()
    assert not is_due(pushed) and not is_due(observed)
    tick(session, {})
    assert session.scalars(select(Job).where(Job.kind == "poll_source")).all() == []

    def boom(kind):
        raise AssertionError("must not fetch")

    monkeypatch.setattr("kintsua.watchers.run.get_watcher", boom)
    out = await poll_source(session, {"source_id": str(pushed.id)})
    assert "not polled" in out["skipped"]


def test_seeded_rows_are_returned_unchanged_and_runtime_keys_never_stored(session):
    seeded = find_or_create_source(
        session, "mcp", "npx -y @vendor/server", meta={"transport": "stdio"}, origin="seed"
    )
    again = find_or_create_source(
        session,
        "mcp",
        "npx -y @vendor/server",
        display_name="mine",
        meta={"env": {"NODE_OPTIONS": "--require /tmp/x.js"}, "args": ["--evil"]},
    )
    assert again.id == seeded.id and again.origin == "seed"
    assert again.meta == {"transport": "stdio"}
    cust = find_or_create_source(
        session, "openapi", "https://c.example/spec.json", meta={"_net_guard": False, "a": 1}
    )
    assert cust.meta == {"a": 1}


# --------------------------------------------------------------------------------------
# rule 3 (watchers-security-4, core-security-8): the network guard
# --------------------------------------------------------------------------------------


def test_source_ref_for_guards_every_non_seed_source(session, settings, monkeypatch):
    cust = find_or_create_source(session, "openapi", "https://c.example/spec.json")
    cust.meta = {"_net_guard": False}  # a persisted flag is never honoured
    seed = find_or_create_source(session, "openapi", "https://s.example/spec.json", origin="seed")
    assert source_ref_for(cust).meta["_net_guard"] is True
    assert "_net_guard" not in source_ref_for(seed).meta
    monkeypatch.setattr(settings, "net_guard", False)
    assert "_net_guard" not in source_ref_for(cust).meta


async def test_customer_source_on_a_private_address_is_a_recorded_failure(session):
    src = find_or_create_source(session, "openapi", "http://127.0.0.1:9/spec.json")
    session.commit()
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["ok"] is False and res["error"].startswith("BlockedURL")
    session.refresh(src)
    assert src.consecutive_failures == 1 and "not a public address" in src.last_error


@respx.mock
async def test_redirects_to_private_addresses_are_blocked():
    url = "http://93.184.216.34/spec.json"  # a public literal: no DNS lookup in the test
    respx.get(url).mock(
        return_value=httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
    )
    meta = respx.get("http://169.254.169.254/latest").mock(
        return_value=httpx.Response(200, text='{"openapi": "3.0.0", "info": {}, "paths": {}}')
    )
    with pytest.raises(BlockedURL):
        await fetch_spec(url, GUARD)
    assert not meta.called
    # unguarded (the CLI) follows it
    assert (await fetch_spec(url, {}))["openapi"] == "3.0.0"
    with pytest.raises(BlockedURL):
        await fetch_spec("http://localhost:8080/spec.json", GUARD)


@respx.mock
async def test_changelog_and_package_fetches_are_guarded(monkeypatch):
    clear_robots_cache()
    page = "http://93.184.216.34/changelog"
    respx.get("http://93.184.216.34/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(page).mock(
        return_value=httpx.Response(302, headers={"Location": "http://10.0.0.5/admin"})
    )
    with pytest.raises(BlockedURL):
        await ChangelogWatcher().fetch(SourceRef(kind="changelog", locator=page, meta=GUARD))
    with pytest.raises(BlockedURL):
        await ChangelogWatcher().fetch(
            SourceRef(kind="changelog", locator="http://[::1]/changelog", meta=GUARD)
        )
    clear_robots_cache()

    monkeypatch.setattr(pkgmod, "PYPI_URL", "http://93.184.216.34/pypi/{name}/json")
    respx.get("http://93.184.216.34/pypi/acme/json").mock(
        return_value=httpx.Response(302, headers={"Location": "http://192.168.1.1/x"})
    )
    with pytest.raises(BlockedURL):
        await pkgmod.PyPIWatcher().fetch(SourceRef(kind="pypi", locator="acme", meta=GUARD))


# --------------------------------------------------------------------------------------
# watchers-correctness-12: seeded OAuth MCP sources without a token are "unconfigured"
# --------------------------------------------------------------------------------------


async def test_unset_auth_env_is_unconfigured_not_failing(session, monkeypatch):
    monkeypatch.delenv("LINEAR_MCP_TOKEN_TEST", raising=False)
    src = find_or_create_source(
        session,
        "mcp",
        "https://mcp.linear.example/mcp",
        meta={"auth_env": "LINEAR_MCP_TOKEN_TEST"},
        origin="seed",
    )
    src.created_at = utcnow() - timedelta(days=30)
    session.commit()
    assert not is_due(src) and due_sources(session) == []

    def boom(kind):
        raise AssertionError("must not fetch")

    monkeypatch.setattr("kintsua.watchers.run.get_watcher", boom)
    res = await poll_source(session, {"source_id": str(src.id)})
    assert res["ok"] is False and res["error"].startswith("unconfigured: ")
    session.refresh(src)
    assert src.consecutive_failures == 0 and src.backoff_until is None and src.active
    assert failing_sources(session) == [] and failing_sources(session, public_only=False) == []

    monkeypatch.setenv("LINEAR_MCP_TOKEN_TEST", "tok")
    assert is_due(src) and [s.id for s in due_sources(session)] == [src.id]


# --------------------------------------------------------------------------------------
# watchers-security-8: hourly polling for MCP sources a paying org depends on
# --------------------------------------------------------------------------------------


def _paid_world(session, plan: str) -> Source:
    org = Org(github_installation_id=777, name="acme", plan=plan, settings={})
    session.add(org)
    session.flush()
    repo = Repo(org_id=org.id, github_repo_id=778, full_name="acme/agent")
    session.add(repo)
    src = find_or_create_source(session, "mcp", "https://mcp.vendor.example/mcp", origin="seed")
    src.last_polled_at = utcnow() - timedelta(hours=2)
    session.flush()
    session.add(Dependency(repo_id=repo.id, source_id=src.id, kind="mcp", name=src.locator))
    session.commit()
    assert src.poll_interval_s == 6 * 3600
    return src


def test_paid_org_mcp_dependency_is_polled_hourly(session):
    src = _paid_world(session, "pro")
    assert tick(session, {})["polls_enqueued"] == 1
    job = session.scalar(select(Job).where(Job.kind == "poll_source"))
    assert job.payload == {"source_id": str(src.id)}


def test_free_org_mcp_dependency_keeps_the_six_hour_interval(session):
    _paid_world(session, "free")
    assert tick(session, {})["polls_enqueued"] == 0


# --------------------------------------------------------------------------------------
# watchers-security-9 / -5: backoff base and the poll time budget
# --------------------------------------------------------------------------------------


class _Failing(MCPWatcher):
    async def fetch(self, source: SourceRef) -> CanonicalDoc:
        raise RuntimeError("503 from upstream")


async def test_daily_source_retries_within_minutes_after_one_failure(session, monkeypatch):
    src = find_or_create_source(
        session, "openapi", "https://spec.vendor.example/openapi.json", origin="seed"
    )
    session.commit()
    assert src.poll_interval_s == 24 * 3600
    monkeypatch.setattr("kintsua.watchers.run.get_watcher", lambda kind: _Failing())
    assert (await poll_source(session, {"source_id": str(src.id)}))["backoff_s"] == 900
    assert (await poll_source(session, {"source_id": str(src.id)}))["backoff_s"] == 1800


async def test_a_hung_fetch_is_cut_off_and_recorded(session, monkeypatch):
    class Hung(MCPWatcher):
        async def fetch(self, source: SourceRef) -> CanonicalDoc:
            await asyncio.sleep(30)
            raise AssertionError("not reached")

    src = find_or_create_source(session, "mcp", "https://slow.example/mcp", origin="seed")
    src.meta = {"poll_timeout_s": 0.2}
    session.commit()
    monkeypatch.setattr("kintsua.watchers.run.get_watcher", lambda kind: Hung())
    res = await asyncio.wait_for(poll_source(session, {"source_id": str(src.id)}), timeout=10)
    assert res["ok"] is False and res["error"].startswith("PollTimeout")
    session.refresh(src)
    assert src.consecutive_failures == 1 and src.backoff_until is not None
