"""Per-repo settings endpoints behind a signed session cookie."""

from __future__ import annotations

import uuid
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select

from kintsua.core.models import CallSite, Dependency, Impact, Repo, RuntimeEvent, Source
from kintsua.core.util import utcnow
from tests.api.conftest import INSTALLATION_ID, REPO_FULL_NAME, mint_session


def test_endpoints_require_a_session(client, factory):
    org = factory.org()
    repo = factory.repo(org)
    assert client.get(f"/v1/repos/{repo.id}/dependencies").status_code == 401
    assert client.get(f"/v1/repos/{repo.id}/changes").status_code == 401
    assert client.post(f"/v1/repos/{repo.id}/mute", json={"pattern": "x"}).status_code == 401
    assert client.delete(f"/v1/repos/{repo.id}/data").status_code == 401
    # a forged cookie is ignored
    client.cookies.set("kintsua_session", "not-a-signed-token")
    assert client.get(f"/v1/repos/{repo.id}/dependencies").status_code == 401


def test_404_for_repos_outside_the_users_installations(client, factory):
    org = factory.org(installation_id=424242)
    repo = factory.repo(org)
    mint_session(client, [INSTALLATION_ID])
    assert client.get(f"/v1/repos/{repo.id}/dependencies").status_code == 404
    assert client.get(f"/v1/repos/{repo.id}/changes").status_code == 404
    assert client.post(f"/v1/repos/{repo.id}/mute", json={"pattern": "x"}).status_code == 404
    assert client.get("/v1/repos/not-a-uuid/dependencies").status_code == 404
    assert client.get(f"/v1/repos/{uuid.uuid4()}/dependencies").status_code == 404
    # the right installation sees it
    mint_session(client, [424242, INSTALLATION_ID])
    assert client.get(f"/v1/repos/{repo.id}/dependencies").status_code == 200


def test_dependencies_returns_the_map(client, factory):
    org = factory.org()
    repo = factory.repo(org)
    src = factory.source()
    dep = factory.dependency(repo, src)
    factory.call_site(dep)
    factory.call_site(dep, line=50, operation="GET /v1/charges/{id}")
    factory.dependency(
        repo, None, kind="http", name="api.vendor.example", meta={"host": "api.vendor.example"}
    )
    mint_session(client)
    r = client.get(f"/v1/repos/{repo.id}/dependencies")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["repo"] == REPO_FULL_NAME and data["repo_id"] == str(repo.id)
    totals = data["totals"]
    assert totals["watched"] == 1 and totals["unwatched"] == 1 and totals["call_sites"] == 2
    by_name = {d["name"]: d for d in data["dependencies"]}
    assert by_name["stripe"]["watched"] is True and by_name["stripe"]["call_site_count"] == 2
    assert by_name["stripe"]["source_kind"] == "openapi" and by_name["stripe"]["vendor"] == "stripe"
    assert by_name["api.vendor.example"]["watched"] is False
    assert data["fix_mode"] == "issue_only"


def test_changes_are_newest_first(client, factory):
    org = factory.org()
    repo = factory.repo(org)
    src = factory.source()
    older, _, _ = factory.breaking_impact(
        repo,
        source=src,
        status="issued",
        github_issue_number=7,
        created_at=utcnow() - timedelta(minutes=5),
    )
    src2 = factory.source(kind="pypi", locator="openai", vendor="openai")
    e2 = factory.change_event(
        src2,
        ops=[{"op": "deprecated", "path": "/latest", "before": "1", "after": "2"}],
        from_version="1.0.0",
        to_version="2.0.0",
    )
    c2 = factory.classification(
        e2,
        severity="degraded",
        category="deprecation",
        summary="openai 2.0.0 is a major bump",
        affected_paths=["package:openai"],
        rule_hits=["major_bump"],
    )
    newer = factory.impact(repo, e2, c2, status="detected", matched_call_sites=[])
    # an unclassified event still shows up, as unknown / unclassified
    e3 = factory.change_event(src, to_version="2026-10-15")
    unclassified = factory.impact(
        repo, e3, None, status="detected", created_at=utcnow() + timedelta(seconds=1)
    )

    mint_session(client)
    r = client.get(f"/v1/repos/{repo.id}/changes?limit=10")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 3
    changes = data["changes"]
    assert [c["impact_id"] for c in changes] == [str(unclassified.id), str(newer.id), str(older.id)]
    assert changes[0]["severity"] == "unknown" and changes[0]["category"] == "unclassified"
    assert "awaiting classification" in changes[0]["summary"]
    second = changes[1]
    assert second["draft"] is True and second["severity"] == "degraded"
    assert second["vendor"] == "openai" and second["source_kind"] == "pypi"
    assert second["from_version"] == "1.0.0" and second["to_version"] == "2.0.0"
    assert second["rule_hits"] == ["major_bump"] and second["issue_number"] is None
    third = changes[2]
    assert third["draft"] is False and third["status"] == "issued"
    assert third["issue_number"] == 7 and third["issue_url"].endswith("/issues/7")
    assert third["matched_call_sites"][0]["file_path"] == "app/billing.py"
    assert third["summary"].startswith("Stripe POST /v1/charges")
    assert client.get(f"/v1/repos/{repo.id}/changes?limit=1").json()["count"] == 1
    assert client.get(f"/v1/repos/{repo.id}/changes?limit=0").status_code == 422


def test_add_source_creates_source_dependency_job_and_config(client, factory, session):
    org = factory.org()
    repo = factory.repo(org)
    mint_session(client)
    url = "https://api.vendor.example/openapi.json"
    r = client.post(
        f"/v1/repos/{repo.id}/sources",
        json={
            "kind": "openapi",
            "url": url,
            "display_name": "Vendor API",
            "hosts": ["api.vendor.example"],
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    src = session.get(Source, UUID(data["source_id"]))
    assert src.kind == "openapi" and src.locator == url and src.display_name == "Vendor API"
    assert src.active is True
    dep = session.get(Dependency, UUID(data["dependency_id"]))
    assert dep.repo_id == repo.id and dep.source_id == src.id and dep.kind == "openapi"
    assert dep.name == url and dep.meta["configured"] is True
    assert dep.meta["hosts"] == ["api.vendor.example"]
    jobs = factory.jobs("poll_source")
    assert len(jobs) == 1 and jobs[0].payload == {"source_id": str(src.id)}
    assert jobs[0].dedupe_key == f"poll_source:{src.id}"
    session.expire_all()
    assert session.get(Repo, repo.id).config["sources"] == [
        {"kind": "openapi", "url": url, "name": "Vendor API", "hosts": ["api.vendor.example"]}
    ]

    # adding the same URL again is idempotent
    r = client.post(f"/v1/repos/{repo.id}/sources", json={"kind": "openapi", "url": url})
    assert r.status_code == 201 and r.json()["source_id"] == str(src.id)
    assert len(factory.jobs("poll_source")) == 1
    session.expire_all()
    assert len(session.get(Repo, repo.id).config["sources"]) == 1

    # an MCP stdio command is a locator too
    r = client.post(
        f"/v1/repos/{repo.id}/sources",
        json={"kind": "mcp", "command": "npx -y @modelcontextprotocol/server-filesystem /tmp"},
    )
    assert r.status_code == 201, r.text
    mcp = session.get(Source, UUID(r.json()["source_id"]))
    assert (
        mcp.kind == "mcp" and mcp.locator == "npx -y @modelcontextprotocol/server-filesystem /tmp"
    )
    assert mcp.meta["transport"] == "stdio"
    session.expire_all()
    assert session.get(Repo, repo.id).config["sources"][1] == {
        "kind": "mcp",
        "command": "npx -y @modelcontextprotocol/server-filesystem /tmp",
    }

    # bad inputs
    assert (
        client.post(
            f"/v1/repos/{repo.id}/sources", json={"kind": "openapi", "url": "ftp://x"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/v1/repos/{repo.id}/sources", json={"kind": "rss", "url": "https://x.example"}
        ).status_code
        == 422
    )
    assert (
        client.post(f"/v1/repos/{repo.id}/sources", json={"kind": "changelog"}).status_code == 422
    )

    # the HTMX form answers an HTML fragment and asks the page to refresh
    r = client.post(
        f"/v1/repos/{repo.id}/sources",
        data={
            "kind": "changelog",
            "url": "https://vendor.example/changelog",
            "hosts": "a.example, b.example",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert (
        r.headers.get("HX-Refresh") == "true"
        and "Watching https://vendor.example/changelog" in r.text
    )
    session.expire_all()
    entry = session.get(Repo, repo.id).config["sources"][2]
    assert entry["kind"] == "changelog" and entry["hosts"] == ["a.example", "b.example"]


def test_mute_add_remove_and_dismiss_covered_impacts(client, factory, session, fake_github):
    org = factory.org()
    repo = factory.repo(org)
    gh_repo = fake_github.repo(REPO_FULL_NAME)
    issue = gh_repo.create_issue(title="Stripe", body="x", labels=["kintsua"])
    impact, _, _ = factory.breaking_impact(repo, status="issued", github_issue_number=issue.number)
    slack = factory.source(
        kind="openapi", locator="https://slack.example/openapi.json", vendor="slack"
    )
    e2 = factory.change_event(slack)
    c2 = factory.classification(
        e2,
        summary="Slack chat.postMessage requires thread_ts",
        affected_paths=["POST /chat.postMessage"],
    )
    other = factory.impact(
        repo,
        e2,
        c2,
        status="detected",
        matched_call_sites=[
            {
                "call_site_id": "x",
                "file_path": "app/slack.py",
                "line": 3,
                "symbol": "client.chat_postMessage",
                "operation": "POST /chat.postMessage",
                "vendor": "slack",
            }
        ],
    )
    mint_session(client)

    r = client.post(f"/v1/repos/{repo.id}/mute", json={"pattern": "stripe:/v1/charges*"})
    assert r.status_code == 200, r.text
    assert r.json()["mute"] == ["stripe:/v1/charges*"] and r.json()["dismissed"] == [str(impact.id)]
    session.expire_all()
    assert session.get(Impact, impact.id).status == "dismissed"
    assert session.get(Impact, impact.id).resolved_at is not None
    assert session.get(Impact, other.id).status == "detected"
    assert issue.state == "closed" and "muted" in issue.comments[-1].body
    assert session.get(Repo, repo.id).config["mute"] == ["stripe:/v1/charges*"]

    # duplicates collapse, new patterns append
    r = client.post(f"/v1/repos/{repo.id}/mute", json={"pattern": "stripe:/v1/charges*"})
    assert r.json()["mute"] == ["stripe:/v1/charges*"] and r.json()["dismissed"] == []
    r = client.post(f"/v1/repos/{repo.id}/mute", json={"pattern": "tool:get_*"})
    assert r.json()["mute"] == ["stripe:/v1/charges*", "tool:get_*"]
    assert client.post(f"/v1/repos/{repo.id}/mute", json={"pattern": "  "}).status_code == 422

    # remove one
    r = client.delete(f"/v1/repos/{repo.id}/mute", params={"pattern": "stripe:/v1/charges*"})
    assert r.status_code == 200 and r.json()["mute"] == ["tool:get_*"]
    session.expire_all()
    assert session.get(Repo, repo.id).config["mute"] == ["tool:get_*"]

    # the HTMX form answers the mutes fragment; a bare vendor mutes the slack draft (no issue to close)
    r = client.post(
        f"/v1/repos/{repo.id}/mute", data={"pattern": "slack"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200 and 'id="mutes"' in r.text and "slack" in r.text
    assert "1 open impact(s) dismissed" in r.text
    session.expire_all()
    assert session.get(Impact, other.id).status == "dismissed"
    r = client.delete(
        f"/v1/repos/{repo.id}/mute", params={"pattern": "slack"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200 and "slack" not in r.text.split('id="mutes"')[1].split("<form")[0]


def test_fix_mode_switch(client, factory, session):
    org = factory.org()
    repo = factory.repo(org)
    mint_session(client)
    r = client.post(f"/v1/repos/{repo.id}/fix-mode", json={"mode": "pr"})
    assert r.status_code == 200 and r.json() == {
        "repo_id": str(repo.id),
        "fix_mode": "pr",
        "effective": "pr",
    }
    session.expire_all()
    assert session.get(Repo, repo.id).config["fix"]["mode"] == "pr"
    assert client.post(f"/v1/repos/{repo.id}/fix-mode", json={"mode": "yolo"}).status_code == 422
    r = client.post(
        f"/v1/repos/{repo.id}/fix-mode", data={"mode": "issue_only"}, headers={"HX-Request": "true"}
    )
    assert (
        r.status_code == 200
        and 'id="fix-mode"' in r.text
        and "<strong>issue_only</strong>" in r.text
    )
    session.expire_all()
    assert session.get(Repo, repo.id).config["fix"] == {"mode": "issue_only"}


def test_open_issue_flips_a_draft_into_a_github_issue(client, factory, session, fake_github):
    org = factory.org()
    repo = factory.repo(org, config={"fix": {"mode": "pr"}})
    src = factory.source()
    dep = factory.dependency(repo, src)
    factory.call_site(dep)
    impact, event, _ = factory.breaking_impact(repo, source=src)
    mint_session(client)

    r = client.post(f"/v1/repos/{repo.id}/issues/open", json={"impact_id": str(impact.id)})
    assert r.status_code == 200, r.text
    opened = r.json()["opened"]
    assert opened == [{"impact_id": str(impact.id), "issue_number": 1, "status": "issued"}]
    issue = fake_github.repo(REPO_FULL_NAME).issues[1]
    assert "currency" in issue.title
    assert f"kintsua:change_event:{event.id}" in issue.body
    assert {lb.name for lb in issue.labels} >= {"kintsua", "severity:breaking"}
    session.expire_all()
    row = session.get(Impact, impact.id)
    assert row.status == "issued" and row.github_issue_number == 1
    # fix mode pr + a fixable category queues the fixer
    fix_jobs = factory.jobs("fix_impact")
    assert [j.payload for j in fix_jobs] == [{"impact_id": str(impact.id)}]

    # idempotent
    r = client.post(f"/v1/repos/{repo.id}/issues/open", json={"impact_id": str(impact.id)})
    assert r.status_code == 200 and r.json()["opened"][0]["issue_number"] == 1
    assert len(fake_github.repo(REPO_FULL_NAME).issues) == 1

    # unknown / foreign impacts
    assert (
        client.post(
            f"/v1/repos/{repo.id}/issues/open", json={"impact_id": str(uuid.uuid4())}
        ).status_code
        == 404
    )
    other_repo = factory.repo(org, "acme/other", github_repo_id=77)
    foreign, _, _ = factory.breaking_impact(other_repo, source=src)
    assert (
        client.post(
            f"/v1/repos/{repo.id}/issues/open", json={"impact_id": str(foreign.id)}
        ).status_code
        == 404
    )

    # "open all drafts" without an impact id
    src2 = factory.source(kind="pypi", locator="openai", vendor="openai")
    draft2, _, _ = factory.breaking_impact(repo, source=src2)
    r = client.post(f"/v1/repos/{repo.id}/issues/open", data={}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'id="changes"' in r.text and "Opened 1 issue(s)" in r.text
    assert "Open issue" not in r.text
    session.expire_all()
    assert session.get(Impact, draft2.id).status == "issued"


def test_delete_data_reports_counts_and_keeps_the_repo(client, factory, session):
    org = factory.org()
    repo = factory.repo(org)
    src = factory.source()
    dep = factory.dependency(repo, src)
    factory.call_site(dep)
    factory.call_site(dep, line=9)
    factory.breaking_impact(repo, source=src)
    factory.runtime_event(repo)
    factory.runtime_event(
        repo, tool_name="GET api.stripe.com/v1/charges", outcome="ok", sample={"kind": "http"}
    )
    repo2 = factory.repo(org, "acme/other", github_repo_id=5)
    dep2 = factory.dependency(repo2, src)
    factory.call_site(dep2)
    factory.runtime_event(repo2)
    repo.last_indexed_sha = "abc"
    session.commit()
    mint_session(client)

    r = client.delete(f"/v1/repos/{repo.id}/data")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == {
        "runtime_events": 2,
        "impacts": 1,
        "call_sites": 2,
        "dependencies": 1,
    }
    session.expire_all()
    kept = session.get(Repo, repo.id)
    assert kept is not None and kept.last_indexed_sha is None and kept.active is True

    def count(model, *where):
        return session.scalar(select(func.count(model.id)).where(*where))

    assert count(Dependency, Dependency.repo_id == repo.id) == 0
    assert count(Impact, Impact.repo_id == repo.id) == 0
    assert count(RuntimeEvent, RuntimeEvent.repo_id == repo.id) == 0
    assert count(Dependency, Dependency.repo_id == repo2.id) == 1
    assert count(CallSite, CallSite.dependency_id == dep2.id) == 1
    assert count(RuntimeEvent, RuntimeEvent.repo_id == repo2.id) == 1
    assert session.get(Source, src.id) is not None

    r = client.delete(f"/v1/repos/{repo.id}/data", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Data deleted" in r.text


def test_cross_origin_writes_are_refused(client, factory):
    org = factory.org()
    repo = factory.repo(org)
    mint_session(client)
    r = client.post(
        f"/v1/repos/{repo.id}/fix-mode",
        json={"mode": "pr"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    r = client.post(
        f"/v1/repos/{repo.id}/fix-mode",
        json={"mode": "pr"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
