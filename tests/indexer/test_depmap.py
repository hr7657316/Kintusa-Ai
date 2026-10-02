"""Dependency map model and the install-issue renderer."""

from __future__ import annotations

from datetime import UTC, datetime

from kintsua.core.models import CallSite, Dependency, Org, Repo, Source
from kintsua.indexer.depmap import (
    DependencyEntry,
    DependencyMap,
    DependencyTotals,
    dependency_map,
    install_issue_title,
    is_external,
    render_dependency_table,
    render_install_issue,
)


def _repo_stub(full_name="acme/agent"):
    return Repo(full_name=full_name, default_branch="main", config={}, github_repo_id=1)


def _map(**kw) -> DependencyMap:
    deps = [
        DependencyEntry(
            kind="pypi",
            name="stripe",
            vendor="stripe",
            display_name="Stripe",
            watched=True,
            source_kind="pypi",
            openapi_source_id="x",
            call_site_count=5,
        ),
        DependencyEntry(kind="http", name="api.weather.gov", watched=False, call_site_count=1),
        DependencyEntry(
            kind="mcp",
            name="https://mcp.stripe.com",
            server_name="stripe",
            watched=True,
            source_kind="mcp",
            transport="http",
            call_site_count=0,
        ),
        DependencyEntry(
            kind="mcp", name="npx -y @x/mcp", watched=False, transport=None, call_site_count=0
        ),
        DependencyEntry(
            kind="pypi", name="pydantic", external=False, watched=False, call_site_count=0
        ),
        DependencyEntry(
            kind="npm", name="zod", external=False, watched=False, call_site_count=0, dev=True
        ),
        DependencyEntry(
            kind="openapi",
            name="https://specs.example/v1.json",
            watched=True,
            source_kind="openapi",
        ),
    ]
    totals = DependencyTotals(
        dependencies=7,
        external=5,
        library_packages=2,
        call_sites=6,
        watched=3,
        unwatched=2,
        files_parsed=12,
        parse_errors=1,
    )
    return DependencyMap(
        repo="acme/agent",
        head_sha="abcdef1234567890",
        dependencies=deps,
        totals=totals,
        indexed_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        **kw,
    )


def test_title_formula():
    assert install_issue_title(_map()) == "Kintsua is watching 3 of your 5 external dependencies"


def test_table_lists_external_rows_only_with_status_and_counts():
    table = render_dependency_table(_map())
    lines = table.splitlines()
    assert lines[0] == "| Dependency | Kind | Watch status | Call sites |"
    assert "| `stripe` (Stripe) | Python package | 🟢 watched (pypi + openapi) | 5 |" in lines
    assert (
        "| `api.weather.gov` | HTTP API | ⚪ unwatched — no spec or changelog known for this host | 1 |"
        in lines
    )
    assert (
        "| **stripe** `https://mcp.stripe.com` | MCP server | 🟢 watched (mcp + http) | 0 |"
        in lines
    )
    assert "| `npx -y @x/mcp` | MCP server | ⚪ unwatched — disabled in config | 0 |" in lines
    assert "| <https://specs.example/v1.json> | OpenAPI spec | 🟢 watched (openapi) | 0 |" in lines
    assert not any("pydantic" in line or "zod" in line for line in lines)


def test_install_issue_body_sections():
    title, body = render_install_issue(
        _repo_stub(), _map(fix_mode="issue_only", config_error="fix.mode: bad")
    )
    assert title.startswith("Kintsua is watching 3 of your 5")
    assert "at `abcdef1` (2026-10-01 12:00 UTC)" in body
    assert "**5 external dependencies** and **6 call sites** across 12 files" in body
    assert "**3 watched**, **2 unwatched**" in body
    assert "## Dependency map" in body and "| Dependency | Kind |" in body
    assert "1 file(s) could not be fully parsed" in body
    assert "<summary>2 other declared package(s) that are not external APIs</summary>" in body
    assert "`pydantic`, `zod`" in body
    assert (
        "```yaml\nsources:\n  - kind: openapi\n    url: https://api.vendor.com/openapi.json" in body
    )
    assert "  - kind: changelog\n    url: https://vendor.com/changelog" in body
    assert "fix:\n  mode: pr          # pr | issue_only\n  base: main" in body
    assert "Fix mode for this repo is **`issue_only`**" in body and "Set `fix.mode: pr`" in body
    assert "## Configuration error" in body and "fix.mode: bad" in body
    assert "## Privacy" in body and "never values, never source text" in body
    assert "updated on every push to `main`" in body
    _, body_pr = render_install_issue(_repo_stub(), _map(fix_mode="pr"))
    assert "Set `fix.mode: issue_only`" in body_pr and "Configuration error" not in body_pr


def test_is_external_rules():
    assert is_external("http", None, {})
    assert is_external("mcp", None, {})
    assert is_external("pypi", "stripe", {})
    assert is_external("npm", None, {"mcp_server_package": True})
    assert not is_external("pypi", None, {"declared": True})


def test_dependency_map_from_tables(session):
    org = Org(github_installation_id=7, name="acme", settings={"fix_mode": "pr"})
    session.add(org)
    session.flush()
    repo = Repo(
        org_id=org.id,
        github_repo_id=11,
        full_name="acme/agent",
        default_branch="dev",
        last_indexed_sha="f" * 40,
        config={},
    )
    session.add(repo)
    session.flush()
    pkg = Source(kind="pypi", locator="stripe", display_name="stripe (PyPI)", vendor="stripe")
    spec = Source(
        kind="openapi", locator="https://spec", display_name="Stripe API", vendor="stripe"
    )
    dead = Source(kind="changelog", locator="https://dead", display_name="dead", active=False)
    session.add_all([pkg, spec, dead])
    session.flush()
    d1 = Dependency(
        repo_id=repo.id,
        source_id=pkg.id,
        kind="pypi",
        name="stripe",
        first_seen_sha="a" * 40,
        meta={"vendor": "stripe", "openapi_source_id": str(spec.id), "sdk_kind": "sdk"},
    )
    d2 = Dependency(
        repo_id=repo.id,
        source_id=None,
        kind="http",
        name="api.weather.gov",
        meta={"host": "api.weather.gov"},
    )
    d3 = Dependency(repo_id=repo.id, source_id=dead.id, kind="http", name="dead.example", meta={})
    d4 = Dependency(
        repo_id=repo.id,
        source_id=None,
        kind="pypi",
        name="pydantic",
        meta={"declared": True, "config_file": "pyproject.toml"},
    )
    d5 = Dependency(
        repo_id=repo.id,
        source_id=None,
        kind="mcp",
        name="https://rt.example/mcp",
        discovered_at_runtime=True,
        meta={"transport": "http"},
    )
    session.add_all([d1, d2, d3, d4, d5])
    session.flush()
    session.add_all(
        [
            CallSite(
                dependency_id=d1.id,
                file_path="a.py",
                line=1,
                symbol="stripe.Charge.create",
                operation="stripe.Charge.create",
            ),
            CallSite(
                dependency_id=d1.id,
                file_path="a.py",
                line=2,
                symbol="stripe.Charge.list",
                operation="stripe.Charge.list",
            ),
            CallSite(
                dependency_id=d2.id,
                file_path="b.py",
                line=3,
                symbol="httpx.get",
                operation="GET /points/{id}",
            ),
            CallSite(
                dependency_id=d5.id,
                file_path="?",
                line=0,
                symbol=None,
                operation="get_forecast",
                discovered_at_runtime=True,
            ),
        ]
    )
    session.flush()
    m = dependency_map(session, repo)
    assert m.repo == "acme/agent" and m.head_sha == "f" * 40 and m.default_branch == "dev"
    assert m.fix_mode == "pr"
    by = {e.name: e for e in m.dependencies}
    assert (
        by["stripe"].watched
        and by["stripe"].source_kind == "pypi"
        and by["stripe"].openapi_source_id == str(spec.id)
    )
    assert by["stripe"].call_site_count == 2 and by["stripe"].first_seen_sha == "a" * 40
    assert not by["api.weather.gov"].watched and by["api.weather.gov"].call_site_count == 1
    assert not by["dead.example"].watched  # inactive source counts as unwatched
    assert not by["pydantic"].external and by["pydantic"].config_file == "pyproject.toml"
    assert (
        by["https://rt.example/mcp"].discovered_at_runtime and by["https://rt.example/mcp"].external
    )
    assert m.totals == DependencyTotals(
        dependencies=5,
        external=4,
        library_packages=1,
        call_sites=4,
        watched=1,
        unwatched=3,
        runtime_discovered=1,
    )
    # watched first, then kind order, then most call sites
    assert [e.name for e in m.dependencies][:2] == ["stripe", "https://rt.example/mcp"]
    assert m.dependencies[-1].name == "pydantic"
    title, body = render_install_issue(repo, m)
    assert title == "Kintsua is watching 1 of your 4 external dependencies"
    assert "⚪ unwatched, seen at runtime" in body
    assert "1 dependency was first seen by the runtime SDK" in body
