"""Dependency -> Source resolution (needs Postgres)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from kintsua.core.models import Source
from kintsua.indexer.resolve import resolve_dependencies
from kintsua.indexer.schema import DependencyInfo

STRIPE_SPEC = "https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json"


def _src(session, sid) -> Source:
    row = session.get(Source, UUID(str(sid)))
    assert row is not None
    return row


def test_sdk_package_gets_registry_and_openapi_sources(session):
    res = resolve_dependencies(session, [DependencyInfo(kind="pypi", name="stripe")])
    dep = res.dependencies[0]
    pkg = _src(session, dep.source_id)
    assert (pkg.kind, pkg.locator, pkg.vendor) == ("pypi", "stripe", "stripe")
    assert pkg.display_name == "stripe (PyPI)"
    spec = _src(session, dep.meta["openapi_source_id"])
    assert (spec.kind, spec.locator, spec.vendor) == ("openapi", STRIPE_SPEC, "stripe")
    assert set(res.new_source_ids) == {pkg.id, spec.id}
    assert dep.vendor == "stripe" and dep.meta["sdk_kind"] == "sdk"
    # second run: nothing new, same rows
    res2 = resolve_dependencies(session, [DependencyInfo(kind="pypi", name="stripe")])
    assert res2.new_source_ids == [] and res2.dependencies[0].source_id == pkg.id


def test_npm_package_and_changelog_only_vendor(session):
    res = resolve_dependencies(
        session,
        [
            DependencyInfo(kind="npm", name="@notionhq/client"),
            DependencyInfo(kind="pypi", name="notion-client"),
        ],
    )
    npm, py = res.dependencies
    assert _src(session, npm.source_id).kind == "npm"
    assert "openapi_source_id" not in npm.meta
    cl = _src(session, npm.meta["changelog_source_id"])
    assert cl.kind == "changelog" and cl.locator == "https://developers.notion.com/page/changelog"
    assert py.meta["changelog_source_id"] == npm.meta["changelog_source_id"]
    assert _src(session, py.source_id).locator == "notion-client"


def test_google_discovery_spec_carries_format_meta(session):
    res = resolve_dependencies(
        session, [DependencyInfo(kind="pypi", name="google-api-python-client")]
    )
    spec = _src(session, res.dependencies[0].meta["openapi_source_id"])
    assert spec.meta["format"] == "discovery"
    assert spec.locator.startswith("https://www.googleapis.com/discovery/v1/apis/")


def test_unknown_and_mcp_server_packages(session):
    res = resolve_dependencies(
        session,
        [
            DependencyInfo(kind="pypi", name="pydantic"),
            DependencyInfo(
                kind="npm",
                name="@modelcontextprotocol/server-github",
                meta={"mcp_server_package": True},
            ),
            DependencyInfo(
                kind="pypi", name="my-server", meta={"mcp_server_package": True, "inferred": True}
            ),
            DependencyInfo(kind="pypi", name="httpx"),
        ],
    )
    lib, server, inferred, http = res.dependencies
    assert lib.source_id is None
    assert _src(session, server.source_id).kind == "npm"
    assert inferred.source_id is None
    assert http.source_id is None


def test_http_hosts(session):
    res = resolve_dependencies(
        session,
        [
            DependencyInfo(kind="http", name="api.stripe.com"),
            DependencyInfo(kind="http", name="api.notion.com"),
            DependencyInfo(kind="http", name="api.weather.gov"),
            DependencyInfo(kind="http", name="dashboard.stripe.com"),
        ],
    )
    stripe, notion, weather, dash = res.dependencies
    assert _src(session, stripe.source_id).locator == STRIPE_SPEC and stripe.vendor == "stripe"
    assert stripe.meta["resolved_via"] == "sdk_map"
    assert (
        _src(session, notion.source_id).kind == "changelog"
        and notion.meta["resolved_via"] == "changelog"
    )
    assert weather.source_id is None and weather.meta["resolved_via"] is None
    # a vendor host that is not the API host: changelog, never the spec
    assert dash.vendor == "stripe" and _src(session, dash.source_id).kind == "changelog"


def test_repo_config_sources_are_created_attached_and_used_for_hosts(session):
    cfg = {
        "sources": [
            {
                "kind": "openapi",
                "url": "https://specs.example/v1.json",
                "hosts": ["api.example.com"],
            },
            {"kind": "changelog", "url": "https://vendor.example/changelog"},
            {"kind": "mcp", "command": "npx -y @acme/mcp"},
        ]
    }
    res = resolve_dependencies(
        session,
        [
            DependencyInfo(kind="http", name="api.example.com"),
            DependencyInfo(kind="http", name="eu.api.example.com"),
            DependencyInfo(kind="http", name="vendor.example"),
        ],
        repo_config=cfg,
    )
    by = {d.key: d for d in res.dependencies}
    spec_dep = by[("openapi", "https://specs.example/v1.json")]
    assert spec_dep.meta["config_file"] == ".kintsua.yml" and spec_dep.meta["hosts"] == [
        "api.example.com"
    ]
    assert _src(session, spec_dep.source_id).meta["configured_by"] == ".kintsua.yml"
    assert by[("http", "api.example.com")].source_id == spec_dep.source_id
    assert by[("http", "api.example.com")].meta["resolved_via"] == ".kintsua.yml"
    assert by[("http", "eu.api.example.com")].source_id == spec_dep.source_id  # subdomain
    cl_dep = by[("changelog", "https://vendor.example/changelog")]
    assert by[("http", "vendor.example")].source_id == cl_dep.source_id  # spec URL host == API host
    mcp_dep = by[("mcp", "npx -y @acme/mcp")]
    mcp_src = _src(session, mcp_dep.source_id)
    assert (
        mcp_src.kind == "mcp"
        and mcp_src.meta["transport"] == "stdio"
        and mcp_src.meta["quiet"] is True
    )
    assert len(res.dependencies) == 6


def test_mcp_sources_keep_transport_and_only_env_names(session):
    res = resolve_dependencies(
        session,
        [
            DependencyInfo(
                kind="mcp",
                name="npx -y @modelcontextprotocol/server-github",
                display_name="github",
                meta={
                    "transport": "stdio",
                    "env_keys": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
                    "server_name": "github",
                },
            ),
            DependencyInfo(
                kind="mcp", name="https://mcp.linear.app/sse", meta={"transport": "sse"}
            ),
            DependencyInfo(
                kind="mcp", name="node off.js", meta={"transport": "stdio", "disabled": True}
            ),
        ],
    )
    gh, linear, off = res.dependencies
    gh_src = _src(session, gh.source_id)
    assert gh_src.display_name == "github"
    assert gh_src.meta == {
        "transport": "stdio",
        "quiet": True,
        "env_keys": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    }
    lin = _src(session, linear.source_id)
    assert lin.meta["transport"] == "sse" and lin.vendor == "linear" and linear.vendor == "linear"
    assert off.source_id is None
    assert session.scalar(select(Source).where(Source.locator == "node off.js")) is None


def test_existing_source_rows_are_reused_not_duplicated(session):
    existing = Source(
        kind="openapi", locator=STRIPE_SPEC, display_name="Stripe API (seed)", vendor="stripe"
    )
    session.add(existing)
    session.flush()
    res = resolve_dependencies(session, [DependencyInfo(kind="http", name="api.stripe.com")])
    assert res.dependencies[0].source_id == existing.id
    assert res.new_source_ids == []
    assert (
        session.scalar(select(Source).where(Source.locator == STRIPE_SPEC)).display_name
        == "Stripe API (seed)"
    )
