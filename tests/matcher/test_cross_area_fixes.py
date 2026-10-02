"""Regression tests for the cross-area follow-ups on the matcher (Group C): runtime server
URLs compare without credentials / query / fragment (C5), pushed call sites are labelled
and pseudo paths never get a blob link (C6). C4 (runtime discovery creates
``origin="runtime"`` rows for public http(s) URLs only) is covered by
``test_runtime_review.py`` (needs Postgres)."""

from __future__ import annotations

from sqlalchemy import select

from kintsua.core.models import CallSite, Dependency, Impact, Source
from kintsua.core.util import utcnow
from kintsua.matcher.issues import (
    PUSH_SITE_SYMBOL,
    blob_url,
    build_issue_body,
    has_file_located_site,
)
from kintsua.matcher.runtime import match_runtime, server_key
from tests.matcher.conftest import Factory

# --------------------------------------------------------------------------------------
# C5: server identity ignores userinfo, query string and fragment
# --------------------------------------------------------------------------------------


def test_server_key_drops_userinfo_query_and_fragment():
    sdk = "https://mcp.rooms.example/mcp"
    for configured in (
        "https://mcp.rooms.example/mcp?api_key=SECRET",
        "https://mcp.rooms.example/mcp/?api_key=SECRET#tools",
        "https://user:pw@MCP.Rooms.example/mcp",
        "HTTPS://mcp.rooms.example/mcp#frag",
    ):
        assert server_key(configured) == server_key(sdk) == sdk, configured
    assert server_key("https://mcp.rooms.example:8443/mcp?k=v") == (
        "https://mcp.rooms.example:8443/mcp"
    )
    assert server_key("https://mcp.rooms.example:8443/mcp") != server_key(sdk)
    assert server_key("https://mcp.rooms.example/other") != server_key(sdk)
    assert server_key("http://[::1/x") == "http://[::1/x"  # unparsable: compared as text


def test_runtime_calls_to_a_configured_url_with_a_key_are_the_indexed_server(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    configured = "https://mcp.rooms.example/mcp?api_key=SECRET"
    src = f.source("mcp", configured, display_name="rooms", vendor=None)
    dep = f.dependency(repo, source=src, kind="mcp", name=configured, meta={"transport": "http"})
    f.call_site(dep, "list_rooms", file_path="agent.py", line=3)
    now = utcnow()
    sample = {"server": "https://mcp.rooms.example/mcp"}  # what the SDK sends
    f.runtime_event(repo, "list_rooms", observed_at=now, sample=sample)
    f.runtime_event(repo, "book_room", observed_at=now, sample=sample)
    session.commit()

    res = match_runtime(session, repo, now=now)
    # list_rooms is covered by the indexed call site; book_room is a new tool of the same
    # server, so it lands on the indexed dependency, not on a new runtime one
    assert res.dependencies_created == 0 and res.call_sites_created == 1
    deps = list(session.scalars(select(Dependency).where(Dependency.repo_id == repo.id)))
    assert [d.id for d in deps] == [dep.id]
    ops = {cs.operation for cs in session.scalars(select(CallSite))}
    assert ops == {"list_rooms", "book_room"}
    assert [s.locator for s in session.scalars(select(Source))] == [configured]


# --------------------------------------------------------------------------------------
# C6: pushed call sites and pseudo paths in the issue body
# --------------------------------------------------------------------------------------


def test_pushed_sites_are_labelled_and_pseudo_paths_have_no_blob_link(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    src = f.source("mcp", f"push:{org.id}:mcp:https://mcp.acme.example/mcp", vendor=None)
    event = f.event(src, [], source_kind="mcp")
    cls = f.classification(event, category="removed", summary="get_invoice removed")
    impact = Impact(
        repo_id=repo.id,
        change_event_id=event.id,
        status="detected",
        matched_call_sites=[
            {
                "file_path": "<runtime>",
                "line": 0,
                "symbol": PUSH_SITE_SYMBOL,
                "operation": "get_invoice",
            },
            {"file_path": "<generated>", "line": 7, "symbol": "x", "operation": "list_invoices"},
            {"file_path": "<runtime>", "line": 0, "symbol": "get_rates", "operation": "get_rates"},
        ],
    )
    _, body = build_issue_body(impact, event, cls, repo, source=src, org=org)
    assert "| pushed by `kintsua watch --push` | - | `get_invoice` |" in body
    assert "| &lt;generated&gt;:7 | `x` | `list_invoices` |" in body
    assert "| runtime (observed by the SDK) | `get_rates` | `get_rates` |" in body
    assert "github.com" not in body.split("## Where this repo calls it")[1].split("##")[0]

    assert blob_url(repo, "<generated>", 7) is None and blob_url(repo, "<runtime>", 0) is None
    assert blob_url(repo, "src/a.py", 3) == (
        "https://github.com/acme/support-agent/blob/main/src/a.py#L3"
    )
    assert not has_file_located_site(impact.matched_call_sites)
    assert has_file_located_site([{"file_path": "src/a.py"}, {"file_path": "<runtime>"}])
