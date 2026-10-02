"""Regression tests for the runtime-matching review findings: source trust for runtime
samples, privacy of promoted events, mutes on promotion, per-dependency coverage, error
spikes scoped to the host, and malformed / over-long runtime data."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from kintsua.api.billing import watched_integration_count
from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Dependency,
    Impact,
    Job,
    RuntimeEvent,
    Source,
)
from kintsua.core.util import utcnow
from kintsua.matcher.issues import create_issue_for_impact
from kintsua.matcher.match import match_change_event
from kintsua.matcher.runtime import (
    match_runtime,
    observed_locator,
    server_key,
    watchable_server_url,
)
from kintsua.watchers.run import due_sources
from tests.matcher.conftest import Factory


def _deps(session, repo) -> dict[tuple[str, str], Dependency]:
    return {
        (d.kind, d.name): d
        for d in session.scalars(select(Dependency).where(Dependency.repo_id == repo.id))
    }


def _sites(session, dep: Dependency) -> list[CallSite]:
    return list(session.scalars(select(CallSite).where(CallSite.dependency_id == dep.id)))


def _mismatches(f: Factory, repo, tool: str, n: int, *, server: str | None = None, fp="a"):
    now = utcnow()
    for i in range(n):
        sample: dict = {"diff": [{"op": "added", "path": "/properties/x", "after": {}}]}
        if server:
            sample["server"] = server
        f.runtime_event(
            repo,
            tool,
            outcome="schema_mismatch",
            fingerprint=fp * 64,
            sample=sample,
            observed_at=now - timedelta(minutes=i + 1),
        )


# --------------------------------------------------------------------------------------
# security-1: runtime samples never become stdio / internal sources
# --------------------------------------------------------------------------------------


def test_sdk_label_server_creates_no_source_and_nothing_polled_or_billed(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    now = utcnow()
    # what the SDK reports for a stdio ClientSession wrapped without server=
    f.runtime_event(repo, "get_weather", observed_at=now, sample={"server": "mcp:weather-server"})
    f.runtime_event(repo, "get_forecast", observed_at=now, sample={"server": "mcp:weather-server"})
    session.commit()

    res = match_runtime(session, repo, now=now)
    assert res.dependencies_created == 1 and res.call_sites_created == 2
    assert session.scalars(select(Source)).all() == []
    dep = _deps(session, repo)[("mcp", "mcp:weather-server")]
    assert dep.source_id is None and dep.discovered_at_runtime
    assert dep.meta["server_name"] == "mcp:weather-server"
    assert "transport" not in dep.meta and dep.meta["server_unwatched"]
    assert {cs.operation for cs in _sites(session, dep)} == {"get_weather", "get_forecast"}
    assert due_sources(session, now, only_due=False) == []
    assert watched_integration_count(session, org) == 0


@pytest.mark.parametrize(
    "server",
    [
        "/usr/bin/touch /tmp/kintsua-pwned-marker",
        "npx -y @evil/server",
        "python -c 'import os; os.system(\"id\")'",
        "http://169.254.169.254/mcp",
        "http://127.0.0.1:8080/mcp",
        "http://localhost:3000/mcp",
        "http://10.1.2.3/mcp",
        "http://[::1]/mcp",
        "http://metadata.google.internal/computeMetadata/v1/",
        "https://mcp.corp.internal/mcp",
        "https://user:secret@mcp.example.com/mcp",
        "file:///etc/passwd",
    ],
)
def test_untrusted_server_values_never_become_sources(session, f: Factory, server):
    org = f.org()
    repo = f.repo(org)
    now = utcnow()
    f.runtime_event(repo, "list_rooms", observed_at=now, sample={"server": server})
    session.commit()

    res = match_runtime(session, repo, now=now)
    assert res.dependencies_created == 1
    assert session.scalars(select(Source)).all() == []  # nothing to poll or spawn
    (dep,) = _deps(session, repo).values()
    assert dep.kind == "mcp" and dep.source_id is None
    assert dep.meta.get("transport") != "stdio" and "quiet" not in dep.meta
    assert watchable_server_url(server) is None


def test_public_https_server_becomes_a_runtime_origin_source(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    now = utcnow()
    url = "https://mcp.rooms.example/mcp"
    f.runtime_event(repo, "list_rooms", observed_at=now, sample={"server": url})
    session.commit()
    match_runtime(session, repo, now=now)
    src = session.scalar(select(Source).where(Source.locator == url))
    assert src.kind == "mcp" and src.active and src.origin == "runtime"
    assert src.pollable and not src.trusted and not src.public
    assert src.meta["transport"] == "http"
    assert _deps(session, repo)[("mcp", url)].source_id == src.id
    assert watchable_server_url(url) == url


def test_a_seeded_server_named_at_runtime_keeps_its_seed_row(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    url = "https://mcp.seeded.example/mcp"
    seeded = Source(kind="mcp", locator=url, display_name="Seeded", origin="seed", meta={})
    session.add(seeded)
    now = utcnow()
    f.runtime_event(repo, "lookup", observed_at=now, sample={"server": url})
    session.commit()
    match_runtime(session, repo, now=now)
    session.refresh(seeded)
    assert seeded.origin == "seed" and seeded.meta == {}  # untouched
    assert _deps(session, repo)[("mcp", url)].source_id == seeded.id


def test_promotion_with_a_label_server_hangs_off_the_inactive_observed_source(
    session, f: Factory, settings
):
    org = f.org()
    repo = f.repo(org)
    _mismatches(f, repo, "get_weather", settings.runtime_promotion_threshold, server="mcp:x")
    session.commit()
    res = match_runtime(session, repo)
    (row_id,) = res.promoted
    row = session.get(ChangeEventRow, row_id)
    src = session.get(Source, row.source_id)
    assert src.locator == observed_locator(repo, "get_weather")
    assert src.origin == "observed" and not src.active and not src.pollable
    assert session.scalars(select(Source).where(Source.locator == "mcp:x")).all() == []
    assert due_sources(session, utcnow(), only_due=False) == []


def test_host_fallback_never_borrows_another_orgs_private_source(session, f: Factory):
    other_org = f.org(name="other")
    other_repo = f.repo(other_org, full_name="other/agent")
    private = Source(
        kind="openapi",
        locator="https://specs.other.example/private.json?token=abc",
        display_name="Other private spec",
        origin="customer",
        meta={},
    )
    session.add(private)
    session.flush()
    f.dependency(other_repo, source=private, kind="http", name="api.shared-host.example")

    org = f.org()
    repo = f.repo(org)
    now = utcnow()
    f.runtime_event(repo, "GET api.shared-host.example/v1/things", observed_at=now)
    session.commit()
    match_runtime(session, repo, now=now)
    dep = _deps(session, repo)[("http", "api.shared-host.example")]
    assert dep.source_id is None

    # a source the same org already uses for the host is reused
    repo2 = f.repo(org, full_name="acme/second")
    f.runtime_event(repo2, "GET api.shared-host.example/v1/things", observed_at=now)
    own = Source(
        kind="openapi",
        locator="https://specs.acme.example/shared.json",
        display_name="Acme spec",
        origin="customer",
        meta={},
    )
    session.add(own)
    session.flush()
    dep.source_id = own.id
    session.commit()
    match_runtime(session, repo2, now=now)
    assert _deps(session, repo2)[("http", "api.shared-host.example")].source_id == own.id


# --------------------------------------------------------------------------------------
# security-3: promoted events are private to the repo and never shared across tenants
# --------------------------------------------------------------------------------------


def test_two_tenants_with_the_same_drift_each_get_their_own_private_promotion(
    session, f: Factory, settings
):
    url = "https://mcp.weather.example/mcp"
    org_a, org_b = f.org(name="a"), f.org(name="b")
    repo_a = f.repo(org_a, full_name="a/agent")
    repo_b = f.repo(org_b, full_name="b/agent")
    for repo in (repo_a, repo_b):
        _mismatches(
            f, repo, "get_customer_ledger", settings.runtime_promotion_threshold, server=url
        )
    session.commit()

    res_a = match_runtime(session, repo_a)
    res_b = match_runtime(session, repo_b)
    assert len(res_a.promoted) == 1 and len(res_a.impacts) == 1
    assert len(res_b.promoted) == 1 and len(res_b.impacts) == 1  # not blocked by A's event
    row_a = session.get(ChangeEventRow, res_a.promoted[0])
    row_b = session.get(ChangeEventRow, res_b.promoted[0])
    assert row_a.source_id != row_b.source_id
    for row, repo in ((row_a, repo_a), (row_b, repo_b)):
        src = session.get(Source, row.source_id)
        assert src.origin == "observed" and not src.public
        assert row.diff["private"] is True and row.diff["repo_id"] == str(repo.id)
    # the shared, real server source carries no runtime evidence
    server_src = session.scalar(select(Source).where(Source.locator == url))
    assert (
        session.scalars(
            select(ChangeEventRow).where(ChangeEventRow.source_id == server_src.id)
        ).all()
        == []
    )


def test_a_new_fingerprint_promotes_again_despite_an_earlier_promotion(
    session, f: Factory, settings
):
    org = f.org()
    repo = f.repo(org)
    n = settings.runtime_promotion_threshold
    _mismatches(f, repo, "get_weather", n, fp="a")
    session.commit()
    assert len(match_runtime(session, repo).promoted) == 1
    _mismatches(f, repo, "get_weather", n, fp="b")
    session.commit()
    # the earlier promotion is not a "documented change" that hides the new drift
    assert len(match_runtime(session, repo).promoted) == 1
    assert len(session.scalars(select(ChangeEventRow)).all()) == 2


# --------------------------------------------------------------------------------------
# matcher-correctness-5: .kintsua.yml mutes apply to promotion
# --------------------------------------------------------------------------------------


def test_muted_tool_is_not_promoted_but_an_unmuted_one_is(session, f: Factory, settings):
    org = f.org(auto_issue=True)
    repo = f.repo(org, config={"mute": ["tool:get_*"]})
    n = settings.runtime_promotion_threshold
    _mismatches(f, repo, "get_weather", n)
    _mismatches(f, repo, "set_location", n)
    session.commit()
    res = match_runtime(session, repo)
    assert len(res.promoted) == 1 and len(res.impacts) == 1
    imp = session.get(Impact, res.impacts[0])
    assert [s["operation"] for s in imp.matched_call_sites] == ["set_location"]
    rows = session.scalars(select(ChangeEventRow)).all()
    assert [r.diff["ops"][0]["path"].split("/")[2] for r in rows] == ["set_location"]
    jobs = session.scalars(select(Job).where(Job.kind == "open_issue")).all()
    assert [j.payload["impact_id"] for j in jobs] == [str(imp.id)]


def test_vendor_mute_on_the_server_dependency_blocks_promotion(session, f: Factory, settings):
    org = f.org()
    url = "https://mcp.weather.example/mcp"
    repo = f.repo(org, config={"mute": [f"{url}:*"]})
    _mismatches(f, repo, "get_weather", settings.runtime_promotion_threshold, server=url)
    session.commit()
    res = match_runtime(session, repo)
    assert res.promoted == [] and res.impacts == []


# --------------------------------------------------------------------------------------
# matcher-correctness-6 / -8: coverage is per dependency (host / server / kind)
# --------------------------------------------------------------------------------------


def test_new_host_with_the_same_path_is_discovered(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    openai = f.dependency(repo, kind="http", name="api.openai.com", meta={"vendor": "openai"})
    f.call_site(openai, "POST /v1/chat/completions", file_path="llm.py")
    now = utcnow()
    f.runtime_event(repo, "POST api.together.xyz/v1/chat/completions", observed_at=now)
    f.runtime_event(repo, "POST api.openai.com/v1/chat/completions", observed_at=now)
    session.commit()

    res = match_runtime(session, repo, now=now)
    assert res.dependencies_created == 1 and res.call_sites_created == 1
    together = _deps(session, repo)[("http", "api.together.xyz")]
    assert together.discovered_at_runtime
    assert [(cs.file_path, cs.operation) for cs in _sites(session, together)] == [
        ("<runtime>", "POST /v1/chat/completions")
    ]
    # the indexed host stays covered by its own call site
    assert [cs.file_path for cs in _sites(session, openai)] == ["llm.py"]


def test_sdk_call_ending_in_a_tool_name_neither_covers_nor_absorbs_the_mcp_tool(
    session, f: Factory, settings
):
    org = f.org()
    repo = f.repo(org)
    notion = f.dependency(repo, kind="pypi", name="notion-client", meta={"vendor": "notion"})
    f.call_site(notion, "notion_client.Client.search", file_path="kb.py", line=11)
    url = "https://mcp.docs.example/mcp"
    now = utcnow()
    f.runtime_event(repo, "search", observed_at=now - timedelta(hours=2), sample={"server": url})
    _mismatches(f, repo, "search", settings.runtime_promotion_threshold, server=url)
    session.commit()

    res = match_runtime(session, repo, now=now)
    assert res.dependencies_created == 1 and res.call_sites_created == 1
    docs = _deps(session, repo)[("mcp", url)]
    assert docs.source_id is not None
    (imp_id,) = res.impacts
    sites = session.get(Impact, imp_id).matched_call_sites
    assert [(s["file_path"], s["dependency_id"]) for s in sites] == [("<runtime>", str(docs.id))]


def test_label_server_falls_back_to_the_indexed_mcp_call_site(session, f: Factory, settings):
    """``mcp:<serverInfo.name>`` rarely equals the config entry name: the indexed site of
    an ``mcp`` dependency still covers the tool and is what the promoted impact lists."""
    org = f.org()
    repo = f.repo(org)
    dep = f.dependency(
        repo, kind="mcp", name="npx -y weather-server", meta={"server_name": "weather"}
    )
    site = f.call_site(dep, "get_weather", file_path="agent/tools.py", line=5)
    _mismatches(f, repo, "get_weather", settings.runtime_promotion_threshold, server="mcp:wx")
    session.commit()
    res = match_runtime(session, repo)
    assert res.dependencies_created == 0 and res.call_sites_created == 0
    sites = session.get(Impact, res.impacts[0]).matched_call_sites
    assert [s["call_site_id"] for s in sites] == [str(site.id)]


def test_known_label_server_gets_new_tools_on_its_own_dependency(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    dep = f.dependency(
        repo, kind="mcp", name="npx -y weather-server", meta={"server_name": "weather"}
    )
    f.call_site(dep, "get_weather", file_path="agent/tools.py")
    now = utcnow()
    f.runtime_event(repo, "get_alerts", observed_at=now, sample={"server": "mcp:weather"})
    session.commit()
    res = match_runtime(session, repo, now=now)
    assert res.dependencies_created == 0 and res.call_sites_created == 1
    assert {cs.operation for cs in _sites(session, dep)} == {"get_weather", "get_alerts"}
    assert session.scalars(select(Source)).all() == []


def test_server_key():
    assert server_key("HTTPS://MCP.Example.com/Path/") == "https://mcp.example.com/Path"
    assert server_key("mcp:Weather") == "weather" and server_key("langchain:docs") == "docs"
    assert server_key(None) == ""


# --------------------------------------------------------------------------------------
# matcher-correctness-7: error spikes only count calls to the impact's own host
# --------------------------------------------------------------------------------------


def test_errors_on_another_host_with_the_same_path_are_not_attached(
    session, fake_github, f: Factory
):
    org = f.org()
    repo = f.repo(org)
    spec = f.source(locator="https://a.example/openapi.json", display_name="A", vendor="a")
    dep = f.dependency(repo, source=spec, kind="http", name="api.a.example", meta={"vendor": "a"})
    f.call_site(dep, "GET /v1/users/{id}", file_path="users.py", line=4)
    event = f.event(spec)
    f.classification(event, affected_paths=["GET /v1/users/{id}"])
    session.commit()
    (imp,) = match_change_event(session, event)
    create_issue_for_impact(session, imp)
    issue = fake_github.repo("acme/support-agent").issues[1]
    now = utcnow()
    for i in range(4):
        f.runtime_event(
            repo,
            f"GET api.b.example/v1/users/{1000 + i}",
            outcome="error",
            error_class="HTTPStatusError",
            observed_at=now - timedelta(minutes=i + 1),
        )
    session.commit()
    res = match_runtime(session, repo, now=now)
    assert res.error_spikes == {}
    assert issue.comments == []
    session.refresh(imp)
    assert not imp.runtime_error_count

    # the same failures on the impact's own host do count
    for i in range(2):
        f.runtime_event(
            repo,
            f"GET api.a.example/v1/users/{2000 + i}",
            outcome="error",
            error_class="HTTPStatusError",
            observed_at=now - timedelta(minutes=i + 1),
        )
    session.commit()
    res = match_runtime(session, repo, now=now)
    assert res.error_spikes == {str(imp.id): 2}
    assert len(issue.comments) == 1


# --------------------------------------------------------------------------------------
# security-4: over-long or malformed runtime data never stops the run
# --------------------------------------------------------------------------------------


def test_over_long_names_and_malformed_urls_do_not_break_the_run(session, f: Factory, settings):
    org = f.org()
    repo = f.repo(org)
    now = utcnow()
    long_tool = "t" * 600
    long_server = "https://mcp.example.com/mcp?" + "c" * 300
    f.runtime_event(repo, long_tool, observed_at=now)
    f.runtime_event(repo, "lookup", observed_at=now, sample={"server": long_server})
    f.runtime_event(repo, "GET http://[::1/x", observed_at=now)
    f.runtime_event(repo, "get_weather", observed_at=now)
    session.commit()

    res = match_runtime(session, repo, now=now)
    deps = _deps(session, repo)
    assert ("mcp", "get_weather") in deps  # the normal event is still discovered
    long_dep = next(d for (k, n), d in deps.items() if n.startswith("ttt"))
    assert len(long_dep.name) == 512 and "#" in long_dep.name
    assert [cs.operation for cs in _sites(session, long_dep)] == [long_tool]
    assert deps[("mcp", long_server)].source_id is not None
    assert res.events_seen == 4
    # a second run is idempotent for the long name too
    again = match_runtime(session, repo, now=now)
    assert again.dependencies_created == 0 and again.call_sites_created == 0
    # and a long tool name can still be promoted
    _mismatches(f, repo, long_tool, settings.runtime_promotion_threshold)
    session.commit()
    res = match_runtime(session, repo)
    assert len(res.promoted) == 1 and len(res.impacts) == 1


def test_one_failing_event_is_skipped_and_the_rest_discovered(session, f: Factory, monkeypatch):
    import kintsua.matcher.runtime as rt

    org = f.org()
    repo = f.repo(org)
    now = utcnow()
    f.runtime_event(repo, "explode", observed_at=now)
    f.runtime_event(repo, "get_weather", observed_at=now)
    session.commit()
    real = rt._ensure_runtime_site

    def flaky(session, dep, operation, symbol, keys):
        if operation == "explode":
            raise RuntimeError("boom")
        return real(session, dep, operation, symbol, keys)

    monkeypatch.setattr(rt, "_ensure_runtime_site", flaky)
    res = match_runtime(session, repo, now=now)
    assert res.dependencies_created == 1 and res.call_sites_created == 1
    assert set(_deps(session, repo)) == {("mcp", "get_weather")}  # the failed one rolled back
    assert len(session.scalars(select(RuntimeEvent)).all()) == 2
