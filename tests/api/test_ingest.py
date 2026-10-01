"""SDK ingest (``/v1/ingest``) and CLI snapshot push (``/v1/watch/push``) behind SDK keys."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from kintsua.core.models import ChangeEventRow, Dependency, RuntimeEvent, Snapshot, Source
from kintsua.core.util import utcnow
from tests.api.conftest import REPO_FULL_NAME, bearer

EVENTS = [
    {
        "tool_name": "get_weather",
        "kind": "mcp",
        "schema_fingerprint": "a" * 64,
        "outcome": "error",
        "error_class": "McpError",
        "latency_ms": 120,
        "sample": {"arg_keys": ["city"], "server": "https://mcp.weather.example/mcp"},
        "observed_at": "2026-10-01T12:00:00Z",
    },
    {
        "tool_name": "GET api.stripe.com/v1/charges/{id}",
        "kind": "http",
        "schema_fingerprint": None,
        "outcome": "error",
        "error_class": "HTTP 500",
        "latency_ms": 2500,
        "sample": {
            "status": 500,
            "body_excerpt": "Authorization: Bearer sk_live_abcdefghijklmnop0123",
        },
        "observed_at": "2099-01-01T00:00:00Z",
    },
    {
        "tool_name": "get_weather",
        "outcome": "schema_mismatch",
        "sample": {
            "diff": [
                {
                    "op": "required_added",
                    "path": "/properties/units",
                    "before": None,
                    "after": "units",
                }
            ],
            "previous_fingerprint": "b" * 64,
        },
        "observed_at": "2026-10-01T12:00:05",
    },
]

MCP_V1 = {
    "server": {
        "name": "weather",
        "version": "1.0.0",
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {}},
    },
    "tools": {
        "get_weather": {
            "description": "Weather for a city",
            "inputSchema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    },
    "resources": {},
    "resourceTemplates": {},
    "prompts": {},
}


def _mcp_v2() -> dict:
    v2 = copy.deepcopy(MCP_V1)
    v2["server"]["version"] = "1.1.0"
    schema = v2["tools"]["get_weather"]["inputSchema"]
    schema["properties"]["units"] = {"type": "string", "enum": ["c", "f"]}
    schema["required"] = ["city", "units"]
    return v2


def test_ingest_requires_a_valid_sdk_key(client, factory):
    org = factory.org()
    factory.repo(org)
    body = {"repo": REPO_FULL_NAME, "events": EVENTS[:1]}
    r = client.post("/v1/ingest", json=body)
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"
    assert (
        client.post("/v1/ingest", json=body, headers=bearer("kts_" + "x" * 32)).status_code == 401
    )
    assert (
        client.post("/v1/ingest", json=body, headers={"Authorization": "Token abc"}).status_code
        == 401
    )
    key = factory.sdk_key(org)
    assert client.post("/v1/ingest", json=body, headers=bearer(key)).status_code == 200
    # an inactive org's key no longer works
    org.active = False
    factory.commit()
    assert client.post("/v1/ingest", json=body, headers=bearer(key)).status_code == 401


def test_ingest_writes_runtime_events_and_queues_match_runtime(client, factory, session):
    org = factory.org()
    repo = factory.repo(org)
    key = factory.sdk_key(org)
    before = utcnow()
    r = client.post(
        "/v1/ingest",
        json={"repo": REPO_FULL_NAME, "sdk_version": "0.1.0", "events": EVENTS},
        headers=bearer(key),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 3, "repo_id": str(repo.id)}

    rows = list(
        session.scalars(
            select(RuntimeEvent)
            .where(RuntimeEvent.repo_id == repo.id)
            .order_by(RuntimeEvent.created_at)
        )
    )
    assert len(rows) == 3
    session.expire_all()
    org = session.get(type(org), org.id)
    assert {r.sdk_key_hash for r in rows} == {org.sdk_key_hash}
    mcp, http, mismatch = rows
    assert mcp.tool_name == "get_weather" and mcp.outcome == "error" and mcp.latency_ms == 120
    assert mcp.schema_fingerprint == "a" * 64 and mcp.error_class == "McpError"
    assert mcp.sample == {
        "arg_keys": ["city"],
        "server": "https://mcp.weather.example/mcp",
        "kind": "mcp",
    }
    assert mcp.observed_at == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    # secrets are redacted again server-side, future timestamps are clamped to now
    assert http.sample["kind"] == "http" and http.sample["status"] == 500
    assert (
        "sk_live" not in http.sample["body_excerpt"] and "[REDACTED]" in http.sample["body_excerpt"]
    )
    assert before <= http.observed_at <= utcnow()
    # the kind is inferred when the SDK omits it; naive timestamps are UTC
    assert mismatch.outcome == "schema_mismatch" and mismatch.sample["kind"] == "mcp"
    assert mismatch.sample["diff"][0]["op"] == "required_added"
    assert mismatch.sample["previous_fingerprint"] == "b" * 64
    assert mismatch.observed_at == datetime(2026, 10, 1, 12, 0, 5, tzinfo=UTC)

    jobs = factory.jobs("match_runtime")
    assert len(jobs) == 1
    assert jobs[0].payload == {"repo_id": str(repo.id)}
    assert jobs[0].dedupe_key == f"match_runtime:{repo.id}"
    assert jobs[0].run_after >= before + timedelta(seconds=25)

    # a second batch collapses on the queued job
    r = client.post(
        "/v1/ingest", json={"repo": REPO_FULL_NAME, "events": EVENTS[:1]}, headers=bearer(key)
    )
    assert r.status_code == 200 and r.json()["accepted"] == 1
    assert len(factory.jobs("match_runtime")) == 1

    # an empty batch is accepted and queues nothing new
    r = client.post("/v1/ingest", json={"repo": REPO_FULL_NAME, "events": []}, headers=bearer(key))
    assert r.status_code == 200 and r.json()["accepted"] == 0


def test_ingest_404_for_unknown_repo_and_413_over_batch(client, factory, settings, monkeypatch):
    org = factory.org()
    repo = factory.repo(org)
    key = factory.sdk_key(org)
    r = client.post(
        "/v1/ingest", json={"repo": "acme/not-installed", "events": EVENTS[:1]}, headers=bearer(key)
    )
    assert r.status_code == 404 and "not installed" in r.json()["error"]
    # another org's repo is not visible through this key
    other = factory.org("other", installation_id=5)
    factory.repo(other, "other/app")
    r = client.post(
        "/v1/ingest", json={"repo": "other/app", "events": EVENTS[:1]}, headers=bearer(key)
    )
    assert r.status_code == 404
    # inactive repos still accept events
    repo.active = False
    factory.commit()
    r = client.post(
        "/v1/ingest",
        json={"repo": REPO_FULL_NAME.upper(), "events": EVENTS[:1]},
        headers=bearer(key),
    )
    assert r.status_code == 200

    monkeypatch.setattr(settings, "ingest_max_batch", 2)
    r = client.post(
        "/v1/ingest", json={"repo": REPO_FULL_NAME, "events": EVENTS}, headers=bearer(key)
    )
    assert r.status_code == 413 and "limit of 2" in r.json()["error"]
    # schema validation
    bad = {"repo": REPO_FULL_NAME, "events": [{"tool_name": "x", "outcome": "exploded"}]}
    assert client.post("/v1/ingest", json=bad, headers=bearer(key)).status_code == 422
    assert (
        client.post(
            "/v1/ingest", json={"repo": "no-slash", "events": []}, headers=bearer(key)
        ).status_code
        == 422
    )


def test_watch_push_snapshots_diffs_and_attaches_a_dependency(client, factory, session):
    org = factory.org()
    repo = factory.repo(org)
    key = factory.sdk_key(org)
    locator = "https://mcp.weather.example/mcp"
    body = {
        "repo": REPO_FULL_NAME,
        "source": {
            "kind": "mcp",
            "locator": locator,
            "display_name": "Weather MCP",
            "vendor": "weather",
        },
        "doc": {"version": "1.0.0", "body": MCP_V1},
    }
    assert client.post("/v1/watch/push", json=body).status_code == 401

    r = client.post("/v1/watch/push", json=body, headers=bearer(key))
    assert r.status_code == 200, r.text
    first = r.json()
    assert (
        first["change_event_id"] is None and first["ops"] == 0 and first["first_snapshot"] is True
    )
    src = session.get(Source, first["source_id"])
    assert src.kind == "mcp" and src.locator == locator and src.display_name == "Weather MCP"
    assert (
        src.vendor == "weather"
        and src.meta["transport"] == "http"
        and src.meta["pushed_by"] == "cli"
    )
    assert src.last_polled_at is not None
    dep = session.get(Dependency, first["dependency_id"])
    assert dep.repo_id == repo.id and dep.kind == "mcp" and dep.name == locator
    assert dep.source_id == src.id and dep.discovered_at_runtime is True
    assert dep.meta["vendor"] == "weather" and dep.meta["pushed_by"] == "cli"
    assert session.scalar(select(Snapshot).where(Snapshot.source_id == src.id)) is not None
    assert factory.jobs("classify_event") == []

    # the same body again: no new snapshot, no event
    r = client.post("/v1/watch/push", json=body, headers=bearer(key))
    assert (
        r.status_code == 200
        and r.json()["change_event_id"] is None
        and r.json()["first_snapshot"] is False
    )
    assert len(list(session.scalars(select(Snapshot).where(Snapshot.source_id == src.id)))) == 1

    # a changed schema produces a change event with ops and a classify_event job
    body2 = {**body, "doc": {"version": "1.1.0", "body": _mcp_v2()}}
    r = client.post("/v1/watch/push", json=body2, headers=bearer(key))
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["change_event_id"] and second["ops"] > 0 and second["source_id"] == str(src.id)
    assert second["dependency_id"] == str(dep.id)
    event = session.get(ChangeEventRow, second["change_event_id"])
    assert event.source_id == src.id and event.diff["source_kind"] == "mcp"
    assert event.diff["from_version"] == "1.0.0" and event.diff["to_version"] == "1.1.0"
    ops = {(op["op"], op["path"]) for op in event.diff["ops"]}
    assert any(
        kind == "required_added" and path.startswith("/tools/get_weather/inputSchema")
        for kind, path in ops
    )
    jobs = factory.jobs("classify_event")
    assert [j.payload for j in jobs] == [{"change_event_id": str(event.id)}]
    assert len(session.scalars(select(Dependency).where(Dependency.repo_id == repo.id)).all()) == 1

    # an OpenAPI document works the same way (stdio MCP locators are marked as such)
    spec = {"openapi": "3.0.0", "info": {"title": "t", "version": "1"}, "paths": {}}
    r = client.post(
        "/v1/watch/push",
        json={
            "repo": REPO_FULL_NAME,
            "source": {"kind": "openapi", "locator": "https://api.vendor.example/openapi.json"},
            "doc": {"version": "1", "body": spec},
        },
        headers=bearer(key),
    )
    assert r.status_code == 200 and r.json()["change_event_id"] is None
    r = client.post(
        "/v1/watch/push",
        json={
            "repo": REPO_FULL_NAME,
            "source": {"kind": "mcp", "locator": "npx -y weather-mcp"},
            "doc": {"version": None, "body": MCP_V1},
        },
        headers=bearer(key),
    )
    assert r.status_code == 200
    assert session.get(Source, r.json()["source_id"]).meta["transport"] == "stdio"

    # unknown repo / bad kind
    r = client.post("/v1/watch/push", json={**body, "repo": "acme/nope"}, headers=bearer(key))
    assert r.status_code == 404
    bad = {**body, "source": {"kind": "pypi", "locator": "stripe"}}
    assert client.post("/v1/watch/push", json=bad, headers=bearer(key)).status_code == 422
