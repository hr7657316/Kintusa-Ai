"""Regression tests for the cross-area follow-ups on the indexer (Group C): the resolver uses
the real ``kintsua.watchers.run.find_or_create_source`` (C2), and a repo naming a private
``push:`` / ``observed:`` source, or a row another org owns, gets an unwatched dependency
with a reason instead of a failed index or a dependency on that source (C3). C1's trust
rules for live ``fetch_tools`` are covered by ``test_run_review_fixes.py`` (needs Postgres)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from kintsua.core.models import CallSite, Dependency, Job, Org, Snapshot, Source
from kintsua.indexer import resolve
from kintsua.indexer import run as run_mod
from kintsua.indexer.depmap import dependency_map, render_dependency_table
from kintsua.indexer.resolve import NOT_SHAREABLE, SourceCache, resolve_dependencies
from kintsua.indexer.run import index_repo, known_tools_lookup
from kintsua.indexer.schema import DependencyInfo
from kintsua.watchers import run as watchers_run
from tests.indexer.test_run import SHA1, SHA2, deps_of, fake_clone, make_org_repo

FIXTURE = "raw-httpx-agent"


def _other_org(session) -> Org:
    org = Org(github_installation_id=9001, name="globex", settings={})
    session.add(org)
    session.flush()
    return org


def _pushed(session, org: Org, locator: str, tools: list[str] | None = None) -> Source:
    """Another org's pushed snapshot, as ``POST /v1/watch/push`` stores it."""
    src = Source(
        kind="mcp",
        locator=f"push:{org.id}:mcp:{locator}",
        display_name=locator,
        origin="push",
        owner_org_id=org.id,
        active=False,
        meta={"pushed_by": "cli", "pushed_locator": locator},
    )
    session.add(src)
    session.flush()
    if tools:
        session.add(
            Snapshot(
                source_id=src.id,
                content_hash=uuid4().hex,
                storage_ref={"kind": "inline", "body": {"tools": {t: {} for t in tools}}},
            )
        )
        session.flush()
    return src


def _observed(session, repo_name: str, tool: str) -> Source:
    src = Source(
        kind="mcp",
        locator=f"observed:{repo_name}:{tool}",
        display_name=tool,
        origin="observed",
        active=False,
        meta={"observed": True, "repo": repo_name, "tool": tool},
    )
    session.add(src)
    session.flush()
    return src


# --------------------------------------------------------------------------------------
# C2: the resolver creates sources through watchers.run.find_or_create_source
# --------------------------------------------------------------------------------------


def test_resolver_uses_the_watchers_find_or_create_source_not_a_local_copy(session):
    assert not hasattr(resolve, "_local_find_or_create_source")
    assert resolve.find_or_create_source is watchers_run.find_or_create_source

    seeded = Source(
        kind="mcp",
        locator="https://mcp.seeded.example/mcp",
        display_name="Seeded",
        origin="seed",
        meta={"transport": "http"},
    )
    session.add(seeded)
    session.flush()
    cache = SourceCache(session)
    # rule 1: a seeded row named by a repo is handed back unchanged
    got = cache.get(
        "mcp",
        seeded.locator,
        display_name="mine",
        vendor="acme",
        meta={"transport": "sse", "extra": 1},
    )
    assert got is not None and got.id == seeded.id
    assert got.meta == {"transport": "http"} and got.vendor is None and got.origin == "seed"
    # runtime-only keys are never stored on a new row
    new = cache.get("mcp", "https://mcp.new.example/mcp", meta={"_net_guard": True, "a": 1})
    assert new is not None and new.meta == {"a": 1} and new.origin == "customer"
    assert cache.new_ids == [new.id]


# --------------------------------------------------------------------------------------
# C3: private / unshareable sources from a repo are recorded unwatched
# --------------------------------------------------------------------------------------


def test_a_repo_naming_another_orgs_pushed_or_observed_source_stays_unwatched(session):
    other = _other_org(session)
    pushed = _pushed(session, other, "https://mcp.globex.example/mcp", ["secret_tool"])
    observed = _observed(session, "globex/agent", "lookup")
    before = {s.id: (dict(s.meta or {}), s.display_name, s.vendor) for s in (pushed, observed)}

    res = resolve_dependencies(
        session,
        [
            DependencyInfo(kind="mcp", name=pushed.locator, meta={"transport": "http"}),
            DependencyInfo(kind="mcp", name=observed.locator, meta={"transport": "stdio"}),
        ],
        repo_config={"sources": [{"kind": "mcp", "command": pushed.locator}]},
    )
    by = {d.name: d for d in res.dependencies}
    assert set(by) == {pushed.locator, observed.locator}
    for dep in by.values():
        assert dep.source_id is None and dep.meta["reason"] == NOT_SHAREABLE
    assert res.new_source_ids == [] and res.sources == {}
    session.flush()
    # the private rows were neither attached nor touched
    for src in (pushed, observed):
        session.refresh(src)
        assert (dict(src.meta or {}), src.display_name, src.vendor) == before[src.id]


def test_private_locators_never_become_customer_sources(session):
    """The prefix guard: a ``push:`` / ``observed:`` locator nobody created yet is not
    squatted as a customer row (a later push would otherwise land on it)."""
    squat = f"push:{uuid4()}:mcp:https://mcp.victim.example/mcp"
    res = resolve_dependencies(
        session,
        [
            DependencyInfo(kind="mcp", name=squat, meta={"transport": "http"}),
            DependencyInfo(kind="mcp", name="observed:victim/agent:tool"),
        ],
        repo_config={
            "sources": [
                {"kind": "mcp", "command": "observed:victim/agent:other"},
                {"kind": "mcp", "url": "https://mcp.ok.example/mcp"},
            ]
        },
    )
    assert [s.locator for s in session.scalars(select(Source))] == ["https://mcp.ok.example/mcp"]
    reasons = {d.name: d.meta.get("reason") for d in res.dependencies}
    assert reasons == {
        squat: NOT_SHAREABLE,
        "observed:victim/agent:tool": NOT_SHAREABLE,
        "observed:victim/agent:other": NOT_SHAREABLE,
        "https://mcp.ok.example/mcp": None,
    }


def test_a_row_owned_by_another_org_is_refused_by_find_or_create_source(session):
    """``SourceNotShareable`` from ``watchers.run.find_or_create_source`` (no prefix to give
    it away) is caught: unwatched with the reason, the other sources still resolve."""
    other = _other_org(session)
    owned = Source(
        kind="mcp",
        locator="https://mcp.private.example/mcp",
        display_name="private",
        origin="customer",
        owner_org_id=other.id,
        meta={},
    )
    session.add(owned)
    session.flush()
    res = resolve_dependencies(
        session,
        [
            DependencyInfo(kind="mcp", name=owned.locator, meta={"transport": "http"}),
            DependencyInfo(kind="pypi", name="stripe"),
        ],
        repo_config={"sources": [{"kind": "openapi", "url": owned.locator}]},
    )
    by = {(d.kind, d.name): d for d in res.dependencies}
    mcp = by[("mcp", owned.locator)]
    assert mcp.source_id is None and mcp.meta["reason"] == NOT_SHAREABLE
    assert by[("pypi", "stripe")].source_id is not None
    spec = by[("openapi", owned.locator)]
    assert spec.source_id is not None  # a different (kind, locator): its own customer row
    assert session.get(Source, spec.source_id).owner_org_id is None


def test_index_repo_records_unshareable_servers_unwatched_and_does_not_fail(session, fake_github):
    org, repo = make_org_repo(session)
    other = _other_org(session)
    pushed = _pushed(session, other, "https://mcp.globex.example/mcp", ["secret_tool"])
    observed = _observed(session, "globex/agent", "lookup")
    session.commit()

    def add_configs(root: Path) -> None:
        (root / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "theirs": {"url": pushed.locator},
                        "remote": {"url": "https://mcp.example.com/mcp"},
                    }
                }
            )
        )
        (root / ".kintsua.yml").write_text(
            f"sources:\n  - kind: mcp\n    command: {observed.locator}\n"
        )

    res = index_repo(
        session, repo, clone=fake_clone(FIXTURE, SHA1, add_configs), allow_network=False
    )
    assert res.error is None and repo.last_index_error is None
    deps = deps_of(session, repo)
    for locator in (pushed.locator, observed.locator):
        dep = deps[("mcp", locator)]
        assert dep.source_id is None and dep.meta["reason"] == NOT_SHAREABLE
    assert deps[("mcp", "https://mcp.example.com/mcp")].source_id is not None
    # nothing of this org's points at the private rows, nothing polls them
    assert (
        session.scalar(
            select(Dependency.id).where(Dependency.source_id.in_([pushed.id, observed.id]))
        )
        is None
    )
    polled = {j.payload["source_id"] for j in session.scalars(select(Job))}
    assert str(pushed.id) not in polled and str(observed.id) not in polled
    # the map / install issue says why
    depmap = dependency_map(session, repo)
    entry = next(e for e in depmap.dependencies if e.name == pushed.locator)
    assert not entry.watched and entry.reason == NOT_SHAREABLE
    assert "names a private source" in render_dependency_table(depmap)
    assert "names a private source" in fake_github.repo(repo.full_name).issues[1].body


def test_known_tools_lookup_never_reads_a_private_sources_snapshot(session, monkeypatch):
    other = _other_org(session)
    pushed = _pushed(session, other, "https://mcp.globex.example/mcp", ["secret_tool"])
    owned = Source(
        kind="mcp",
        locator="https://mcp.private.example/mcp",
        display_name="private",
        origin="customer",
        owner_org_id=other.id,
        meta={},
    )
    session.add(owned)
    session.flush()
    session.add(
        Snapshot(
            source_id=owned.id,
            content_hash="owned",
            storage_ref={"kind": "inline", "body": {"tools": {"private_tool": {}}}},
        )
    )
    session.flush()

    def fetch(locator, meta):
        raise AssertionError(f"fetch_tools must not run for {locator}")

    monkeypatch.setattr(run_mod, "_fetch_tool_names", fetch)
    lookup = known_tools_lookup(session, allow_network=True, budget=run_mod.Budget(600))
    assert lookup(pushed.locator) is None
    assert lookup(owned.locator) is None
    assert lookup(f"observed:globex/agent:{uuid4().hex}") is None


def test_naming_the_orgs_own_pushed_source_keeps_the_push_dependency(session, fake_github):
    """The push owns its dependency row: a repo that also names the locator in its config
    must not detach it from the pushed source."""
    org, repo = make_org_repo(session)
    pushed = _pushed(session, org, "https://mcp.acme.example/mcp", ["get_invoice"])
    dep = Dependency(
        repo_id=repo.id,
        source_id=pushed.id,
        kind="mcp",
        name=pushed.locator,
        discovered_at_runtime=True,
        meta={"pushed_locator": "https://mcp.acme.example/mcp"},
    )
    session.add(dep)
    session.flush()
    session.add(
        CallSite(
            dependency_id=dep.id,
            file_path="<runtime>",
            line=0,
            symbol="kintsua watch --push",
            operation="get_invoice",
            args_shape={},
            discovered_at_runtime=True,
        )
    )
    session.commit()

    def add_mcp_json(root: Path) -> None:
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"mine": {"url": pushed.locator}}})
        )

    for sha in (SHA1, SHA2):
        res = index_repo(
            session, repo, clone=fake_clone(FIXTURE, sha, add_mcp_json), allow_network=False
        )
        assert res.error is None
        kept = deps_of(session, repo)[("mcp", pushed.locator)]
        assert kept.id == dep.id and kept.source_id == pushed.id and kept.discovered_at_runtime
        ops = {
            cs.operation
            for cs in session.scalars(select(CallSite).where(CallSite.dependency_id == dep.id))
        }
        assert ops == {"get_invoice"}
