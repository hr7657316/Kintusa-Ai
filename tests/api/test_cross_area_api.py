"""Cross-area follow-ups owned by the API (review group B): stdio MCP sources are refused
(B1), API settings survive a re-index (B2), MCP transports (B3), the mute endpoint dismisses
what the matcher suppresses (B4), the startup storage guard (B5), tool-name redaction on
ingest (B6) and ``SourceNotShareable`` answers (B7)."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from kintsua.api.schemas import AddSourceBody
from kintsua.core.models import Dependency, Impact, Repo, RuntimeEvent, Source
from kintsua.core.storage import StorageNotConfigured
from kintsua.indexer.config import API_SETTINGS_KEY, ConfigSource
from kintsua.watchers.run import SourceNotShareable
from tests.api.conftest import REPO_FULL_NAME, bearer, mint_session

REPOS = Path(__file__).resolve().parents[1] / "fixtures" / "repos"
STDIO = "npx -y @modelcontextprotocol/server-filesystem /tmp"


def _sources_url(repo: Repo) -> str:
    return f"/v1/repos/{repo.id}/sources"


# --------------------------------------------------------------------------------------
# B1: a stdio MCP command is refused with a clear 422
# --------------------------------------------------------------------------------------


def test_add_source_refuses_a_stdio_mcp_command(client, factory, session):
    org = factory.org()
    repo = factory.repo(org)
    mint_session(client)

    r = client.post(_sources_url(repo), json={"kind": "mcp", "command": STDIO})
    assert r.status_code == 422, r.text
    message = r.json()["error"]
    assert "stdio MCP servers are not run by the hosted watcher" in message
    assert "remote URL" in message and "kintsua watch --push" in message
    # nothing is stored: no source row for the worker to deactivate, no dependency, no
    # config entry, no job
    session.expire_all()
    assert session.scalars(select(Source)).all() == []
    assert session.scalars(select(Dependency).where(Dependency.repo_id == repo.id)).all() == []
    assert "sources" not in (session.get(Repo, repo.id).config or {})
    assert factory.jobs() == []

    # the same through the form field the settings page uses, and as an explicit transport
    r = client.post(
        _sources_url(repo),
        data={"kind": "mcp", "url": "python tools/server.py"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 422 and 'class="notice notice-error"' in r.text
    assert "kintsua watch --push" in r.text
    r = client.post(
        _sources_url(repo), json={"kind": "mcp", "command": STDIO, "transport": "stdio"}
    )
    assert r.status_code == 422

    # an operator-seeded stdio server is run by the worker, so naming it still attaches it
    seeded = factory.source(kind="mcp", locator=STDIO, vendor="filesystem")
    seeded.origin = "seed"
    session.commit()
    r = client.post(_sources_url(repo), json={"kind": "mcp", "command": STDIO})
    assert r.status_code == 201, r.text
    assert r.json()["source_id"] == str(seeded.id) and r.json()["polled"] is True


# --------------------------------------------------------------------------------------
# B2: what the settings API set survives a re-index
# --------------------------------------------------------------------------------------


def test_api_settings_survive_a_reindex(client, factory, session, fake_github):
    from kintsua.indexer.run import index_repo

    org = factory.org()
    repo = factory.repo(org)
    mint_session(client)
    spec = "https://specs.example/weather.json"
    assert client.post(f"/v1/repos/{repo.id}/mute", json={"pattern": "slack:*"}).status_code == 200
    r = client.post(f"/v1/repos/{repo.id}/fix-mode", json={"mode": "issue_only"})
    assert r.status_code == 200
    r = client.post(
        _sources_url(repo), json={"kind": "openapi", "url": spec, "hosts": ["api.weather.gov"]}
    )
    assert r.status_code == 201, r.text

    def clone(url: str, branch: str, dest: Path, *, timeout_s: float = 0, depth: int = 1) -> str:
        shutil.copytree(REPOS / "raw-httpx-agent", dest)
        (dest / ".kintsua.yml").write_text(
            "mute:\n  - stripe:/v1/legacy/*\n"
            "fix:\n  mode: pr\n"
            "sources:\n  - kind: changelog\n    url: https://vendor.example/changelog\n"
        )
        return "a" * 40

    session.expire_all()
    index_repo(session, session.get(Repo, repo.id), clone=clone, allow_network=False)
    session.expire_all()
    cfg = session.get(Repo, repo.id).config
    # the file's entries first, the API's merged back in; the API's fix mode wins
    assert cfg["mute"] == ["stripe:/v1/legacy/*", "slack:*"]
    assert cfg["fix"]["mode"] == "issue_only"
    assert [s.get("url") for s in cfg["sources"]] == ["https://vendor.example/changelog", spec]
    assert cfg[API_SETTINGS_KEY]["mute"] == ["slack:*"]
    assert cfg[API_SETTINGS_KEY]["fix"] == {"mode": "issue_only"}
    assert [s["url"] for s in cfg[API_SETTINGS_KEY]["sources"]] == [spec]
    # the API reads the merged config back
    deps = client.get(f"/v1/repos/{repo.id}/dependencies").json()
    assert deps["fix_mode"] == "issue_only"
    watched = {d["name"]: d for d in deps["dependencies"]}
    assert watched["api.weather.gov"]["watched"] is True

    # deleting an API mute sticks across the next re-index; the file's own mute stays
    r = client.delete(f"/v1/repos/{repo.id}/mute", params={"pattern": "slack:*"})
    assert r.status_code == 200
    session.expire_all()
    index_repo(session, session.get(Repo, repo.id), clone=clone, allow_network=False)
    session.expire_all()
    assert session.get(Repo, repo.id).config["mute"] == ["stripe:/v1/legacy/*"]


# --------------------------------------------------------------------------------------
# B3: MCP transport
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "transport"),
    [
        ({"url": "https://mcp.vendor.example/mcp"}, "http"),
        ({"url": "https://mcp.vendor.example/sse"}, "sse"),
        ({"url": "https://mcp.vendor.example/v1/sse/"}, "sse"),
        ({"url": "https://mcp.vendor.example/events", "transport": "sse"}, "sse"),
        ({"url": "https://mcp.vendor.example/sse", "transport": "http"}, "http"),
        ({"url": "https://mcp.vendor.example/mcp", "transport": "streamable-http"}, "http"),
        ({"url": "https://mcp.vendor.example/mcp", "transport": ""}, "http"),
        ({"command": STDIO}, "stdio"),
    ],
)
def test_add_source_body_mcp_transport(body, transport):
    parsed = AddSourceBody.model_validate({"kind": "mcp", **body})
    assert parsed.transport == transport
    entry = parsed.config_entry()
    assert entry["transport"] == transport
    # the entry is a valid .kintsua.yml source the indexer reads the same way
    assert ConfigSource.model_validate(entry).mcp_transport == transport


def test_add_source_body_transport_validation():
    for bad in (
        {"kind": "mcp", "url": "https://mcp.vendor.example/mcp", "transport": "stdio"},
        {"kind": "mcp", "command": STDIO, "transport": "sse"},
        {"kind": "mcp", "url": "https://mcp.vendor.example/mcp", "transport": "pigeon"},
    ):
        with pytest.raises(ValueError):
            AddSourceBody.model_validate(bad)
    # only MCP sources have a transport
    spec = AddSourceBody.model_validate(
        {"kind": "openapi", "url": "https://x.example/openapi.json", "transport": "sse"}
    )
    assert spec.transport is None and "transport" not in spec.config_entry()


def test_add_source_records_an_sse_transport(client, factory, session):
    org = factory.org()
    repo = factory.repo(org)
    mint_session(client)
    url = "https://mcp.vendor.example/sse"
    r = client.post(_sources_url(repo), json={"kind": "mcp", "url": url})
    assert r.status_code == 201, r.text
    src = session.get(Source, UUID(r.json()["source_id"]))
    dep = session.get(Dependency, UUID(r.json()["dependency_id"]))
    assert src.meta["transport"] == "sse" and dep.meta["transport"] == "sse"
    session.expire_all()
    cfg = session.get(Repo, repo.id).config
    assert cfg["sources"] == [{"kind": "mcp", "url": url, "transport": "sse"}]
    assert cfg[API_SETTINGS_KEY]["sources"] == [{"kind": "mcp", "url": url, "transport": "sse"}]
    r = client.post(
        _sources_url(repo),
        json={"kind": "mcp", "url": "https://mcp.other.example/events", "transport": "sse"},
    )
    assert r.status_code == 201
    assert session.get(Source, UUID(r.json()["source_id"])).meta["transport"] == "sse"
    r = client.post(_sources_url(repo), json={"kind": "mcp", "url": url, "transport": "stdio"})
    assert r.status_code == 422


# --------------------------------------------------------------------------------------
# B4: the mute endpoint dismisses exactly what the matcher suppresses
# --------------------------------------------------------------------------------------


def _site(dependency_kind: str, dependency: str, operation: str, vendor: str) -> dict:
    return {
        "call_site_id": f"{dependency}:{operation}",
        "file_path": "app/agent.py",
        "line": 7,
        "symbol": operation,
        "operation": operation,
        "dependency_id": "d",
        "dependency": dependency,
        "dependency_kind": dependency_kind,
        "vendor": vendor,
    }


def test_mute_dismisses_tool_and_package_muted_impacts(client, factory, session):
    from kintsua.matcher.mute import is_muted

    org = factory.org()
    repo = factory.repo(org)
    mcp_src = factory.source(kind="mcp", locator="https://mcp.browser.example/mcp", vendor="acme")
    tool_impact = factory.impact(
        repo,
        factory.change_event(mcp_src),
        None,
        matched_call_sites=[_site("mcp", mcp_src.locator, "browser.navigate", "acme")],
    )
    pkg_src = factory.source(kind="pypi", locator="openai", vendor="openai")
    pkg_impact = factory.impact(
        repo,
        factory.change_event(pkg_src),
        None,
        matched_call_sites=[
            _site("pypi", "openai", "openai.chat.completions.create", "openai"),
            _site("pypi", "openai", "openai.responses.create", "openai"),
        ],
    )
    # the same dotted name on a package dependency is an SDK symbol, not a tool, and its
    # package is not openai: neither mute covers it
    sdk_impact = factory.impact(
        repo,
        factory.change_event(pkg_src),
        None,
        matched_call_sites=[_site("pypi", "browser", "browser.navigate", "acme")],
    )
    # the matcher's rules, which the endpoint must agree with
    assert is_muted(["tool:browser.navigate"], "acme", "browser.navigate", tool=True)
    assert not is_muted(["tool:browser.navigate"], "acme", "browser.navigate")
    mint_session(client)

    r = client.post(f"/v1/repos/{repo.id}/mute", json={"pattern": "tool:browser.navigate"})
    assert r.status_code == 200, r.text
    assert r.json()["dismissed"] == [str(tool_impact.id)]

    r = client.post(f"/v1/repos/{repo.id}/mute", json={"pattern": "package:openai"})
    assert r.status_code == 200, r.text
    assert r.json()["dismissed"] == [str(pkg_impact.id)]
    session.expire_all()
    assert session.get(Impact, tool_impact.id).status == "dismissed"
    assert session.get(Impact, pkg_impact.id).status == "dismissed"
    assert "package:openai" in session.get(Impact, pkg_impact.id).fix_reason
    assert session.get(Impact, sdk_impact.id).status == "detected"


# --------------------------------------------------------------------------------------
# B5: the app refuses to start in prod without shared storage
# --------------------------------------------------------------------------------------


def test_app_refuses_to_start_in_prod_without_shared_storage(settings, monkeypatch, caplog):
    import kintsua.api.__main__ as api_main
    from kintsua.api.app import create_app

    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "s3_bucket", None)
    monkeypatch.setattr(settings, "data_dir_shared", False)
    with caplog.at_level(logging.ERROR, logger="kintsua.api.app"):
        with pytest.raises(StorageNotConfigured, match="S3_BUCKET"):
            create_app()
    assert any("refusing to start" in rec.getMessage() for rec in caplog.records)

    served: list[str] = []
    monkeypatch.setattr(api_main, "setup_logging", lambda: None)
    monkeypatch.setattr(api_main.uvicorn, "run", lambda *a, **kw: served.append("run"))
    assert api_main.main() == 2 and served == []

    # with S3 (or a shared data directory) it starts
    monkeypatch.setattr(settings, "s3_bucket", "kintsua-docs")
    assert create_app() is not None
    assert api_main.main() == 0 and served == ["run"]


# --------------------------------------------------------------------------------------
# B6: tool names are redacted, HTTP paths collapsed the way the SDK does
# --------------------------------------------------------------------------------------

SLACK_HOOK = "/services/T0123ABCD/B0456EFGH/Zx9yW8vU7tS6rQ5pO4nM3lK2"
TELEGRAM = "/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage"


def test_ingest_redacts_tool_names_and_collapses_credential_path_segments(client, factory, session):
    org = factory.org()
    repo = factory.repo(org)
    key = factory.sdk_key(org)
    events = [
        {"tool_name": f"POST hooks.slack.com{SLACK_HOOK}", "kind": "http", "outcome": "error"},
        # the kind is inferred from the name; an old SDK sent the raw Telegram bot path
        {"tool_name": f"post api.telegram.org{TELEGRAM}?x=1", "outcome": "ok"},
        {
            "tool_name": "GET API.Stripe.com/v1/charges/12345/refunds",
            "kind": "http",
            "outcome": "ok",
        },
        {"tool_name": "GET api.stripe.com/v1/charges/{id}", "kind": "http", "outcome": "ok"},
        {"tool_name": "GET https://user:pw@api.example.com/v1/x", "kind": "http", "outcome": "ok"},
        {"tool_name": "lookup?api_key=abcdef0123456789", "kind": "mcp", "outcome": "ok"},
        {"tool_name": "get_weather", "kind": "mcp", "outcome": "ok"},
    ]
    r = client.post(
        "/v1/ingest", json={"repo": REPO_FULL_NAME, "events": events}, headers=bearer(key)
    )
    assert r.status_code == 200, r.text
    names = [
        row.tool_name
        for row in session.scalars(
            select(RuntimeEvent)
            .where(RuntimeEvent.repo_id == repo.id)
            .order_by(RuntimeEvent.created_at, RuntimeEvent.id)
        )
    ]
    assert sorted(names) == sorted(
        [
            "POST hooks.slack.com/services/{token}/{token}/{token}",
            "POST api.telegram.org/{token}/sendMessage",
            "GET api.stripe.com/v1/charges/{id}/refunds",
            "GET api.stripe.com/v1/charges/{id}",
            "GET https://api.example.com/v1/x",
            "lookup?[REDACTED]",
            "get_weather",
        ]
    )
    joined = " ".join(names)
    for secret in ("T0123ABCD", "Zx9yW8vU7tS6rQ5pO4nM3lK2", "AAHdqTcv", "abcdef0123456789", "pw@"):
        assert secret not in joined


@pytest.mark.parametrize(
    "url",
    [
        f"https://hooks.slack.com{SLACK_HOOK}",
        f"https://api.telegram.org{TELEGRAM}",
        "https://api.stripe.com/v1/charges/ch_3PabcdefGHIJKLMNOP12/refunds",
        "https://api.vendor.example/v1/users/0b8f6c3e-2a51-4c1e-9d2f-6f0a7d1e3b42",
        "https://api.vendor.example/v1/objects/5f1d7c2e9a8b4c3d2e1f0a9b/items?limit=10",
        "https://discord.com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz0123",
        "https://api.vendor.example/v2/chat.postMessage/increment_authorization",
    ],
)
def test_server_collapse_matches_the_sdk(url):
    from kintsua.api.ingest import collapse_http_tool_name
    from kintsua.sdk.wrap import http_tool_name

    target = url.split("://", 1)[1]
    assert collapse_http_tool_name(f"POST {target}") == http_tool_name("POST", url)
    # what the current SDK sends is already collapsed: the server leaves it alone
    sent = http_tool_name("GET", url)
    assert collapse_http_tool_name(sent) == sent


# --------------------------------------------------------------------------------------
# B7: SourceNotShareable answers 422 / 409, never a 500
# --------------------------------------------------------------------------------------


def test_add_source_answers_422_for_a_source_it_may_not_share(client, factory, session):
    org = factory.org()
    repo = factory.repo(org)
    other = factory.org("other", installation_id=4242)
    owned = factory.source(kind="openapi", locator="https://private.example/openapi.json")
    owned.owner_org_id = other.id
    observed = factory.source(kind="mcp", locator="https://observed.example/mcp")
    observed.origin = "observed"
    session.commit()
    mint_session(client)

    for body in (
        {"kind": "openapi", "url": owned.locator},
        {"kind": "mcp", "url": observed.locator},
    ):
        r = client.post(_sources_url(repo), json=body)
        assert r.status_code == 422, r.text
        assert "cannot be added" in r.json()["error"]
    session.expire_all()
    assert session.scalars(select(Dependency).where(Dependency.repo_id == repo.id)).all() == []
    assert "sources" not in (session.get(Repo, repo.id).config or {})
    assert factory.jobs() == []


def test_watch_push_answers_409_when_find_or_create_source_refuses(
    client, factory, session, monkeypatch
):
    org = factory.org()
    factory.repo(org)
    key = factory.sdk_key(org)

    def refuse(_session, kind, locator, **kw):
        raise SourceNotShareable(Source(kind=kind, locator=locator, origin="seed"), origin="push")

    monkeypatch.setattr("kintsua.api.ingest.find_or_create_source", refuse)
    r = client.post(
        "/v1/watch/push",
        json={
            "repo": REPO_FULL_NAME,
            "source": {"kind": "mcp", "locator": "https://mcp.weather.example/mcp"},
            "doc": {"version": "1", "body": {"tools": {}}},
        },
        headers=bearer(key),
    )
    assert r.status_code == 409, r.text
    assert "not one of your pushed sources" in r.json()["error"]
