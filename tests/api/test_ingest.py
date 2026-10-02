"""SDK ingest (``/v1/ingest``) and CLI snapshot push (``/v1/watch/push``) behind SDK keys."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from kintsua.api.ingest import MAX_SAMPLE_BYTES
from kintsua.core.db import new_session
from kintsua.core.events import CanonicalDoc, canonical_json
from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Dependency,
    Impact,
    Repo,
    RuntimeEvent,
    Snapshot,
    Source,
)
from kintsua.core.queue import drain
from kintsua.core.util import utcnow
from kintsua.watchers.run import ingest_canonical_doc
from kintsua.worker import register_all_handlers
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


def test_oversized_schema_mismatch_keeps_a_compacted_diff(client, factory, session):
    """api-correctness-10: a large-tool mismatch keeps its ops (clipped) instead of losing
    the diff, so runtime promotion still says which property changed."""
    org = factory.org()
    repo = factory.repo(org)
    key = factory.sdk_key(org)
    big_schema = {
        "type": "object",
        "description": "x" * 400,
        "properties": {f"field_{i}": {"type": "string", "description": "y" * 60} for i in range(8)},
    }
    diff = [
        {
            "op": "required_added" if i == 0 else "added",
            "path": f"/properties/prop_{i}",
            "before": None,
            "after": big_schema,
        }
        for i in range(60)
    ]
    event = {
        "tool_name": "create_pull_request",
        "kind": "mcp",
        "outcome": "schema_mismatch",
        "schema_fingerprint": "c" * 64,
        "sample": {"diff": diff, "previous_fingerprint": "d" * 64, "server": "https://x.example"},
    }
    r = client.post(
        "/v1/ingest", json={"repo": REPO_FULL_NAME, "events": [event]}, headers=bearer(key)
    )
    assert r.status_code == 200, r.text
    row = session.scalar(select(RuntimeEvent).where(RuntimeEvent.repo_id == repo.id))
    sample = row.sample
    assert sample["truncated"] is True and sample["previous_fingerprint"] == "d" * 64
    assert len(canonical_json(sample).encode("utf-8")) <= MAX_SAMPLE_BYTES
    kept = sample["diff"]
    assert kept and kept[0]["op"] == "required_added" and kept[0]["path"] == "/properties/prop_0"
    assert all(set(op) == {"op", "path", "before", "after"} for op in kept)
    assert sample["ops_dropped"] == 60 - len(kept) > 0
    assert isinstance(kept[0]["after"], str) and len(kept[0]["after"]) <= 515


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
    # org-scoped: its own locator, origin push, never polled, never trusted
    assert src.kind == "mcp" and src.locator == f"push:{org.id}:mcp:{locator}"
    assert src.origin == "push" and src.owner_org_id == org.id
    assert src.pollable is False and src.trusted is False and src.public is False
    assert src.display_name == "Weather MCP" and src.vendor == "weather"
    assert src.meta["transport"] == "http" and src.meta["pushed_by"] == "cli"
    assert src.meta["pushed_locator"] == locator
    assert src.last_polled_at is not None
    # nothing was written under the bare locator
    assert session.scalar(select(Source).where(Source.locator == locator)) is None
    dep = session.get(Dependency, first["dependency_id"])
    assert dep.repo_id == repo.id and dep.kind == "mcp" and dep.name == src.locator
    assert dep.source_id == src.id and dep.discovered_at_runtime is True
    assert dep.meta["vendor"] == "weather" and dep.meta["pushed_by"] == "cli"
    assert dep.meta["pushed_locator"] == locator and dep.meta["server_name"] == "Weather MCP"
    sites = list(session.scalars(select(CallSite).where(CallSite.dependency_id == dep.id)))
    assert [(s.operation, s.symbol, s.discovered_at_runtime) for s in sites] == [
        ("get_weather", "kintsua watch --push", True)
    ]
    assert session.scalar(select(Snapshot).where(Snapshot.source_id == src.id)) is not None
    assert factory.jobs("classify_event") == []
    assert factory.jobs("poll_source") == []

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

    # an OpenAPI document works the same way; its operations become call sites
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/v1/things": {"get": {"responses": {}}, "post": {"responses": {}}}},
    }
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
    spec_dep = session.get(Dependency, r.json()["dependency_id"])
    assert sorted(
        session.scalars(select(CallSite.operation).where(CallSite.dependency_id == spec_dep.id))
    ) == ["GET /v1/things", "POST /v1/things"]
    # a stdio command line is accepted: pushed sources are never polled, so never run
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
    stdio = session.get(Source, r.json()["source_id"])
    assert stdio.meta["transport"] == "stdio" and stdio.origin == "push"
    assert stdio.locator == f"push:{org.id}:mcp:npx -y weather-mcp" and not stdio.pollable
    assert factory.jobs("poll_source") == []

    # unknown repo / bad kind
    r = client.post("/v1/watch/push", json={**body, "repo": "acme/nope"}, headers=bearer(key))
    assert r.status_code == 404
    bad = {**body, "source": {"kind": "pypi", "locator": "stripe"}}
    assert client.post("/v1/watch/push", json=bad, headers=bearer(key)).status_code == 422


def _weather_without_tool() -> dict:
    v2 = copy.deepcopy(MCP_V1)
    v2["server"]["version"] = "2.0.0"
    v2["tools"] = {}
    return v2


def test_watch_push_never_touches_a_source_another_org_depends_on(client, factory, session):
    """sdk-cli-security-2: org B pushing a fabricated document for a locator org A watches
    must not change A's source, emit a change event on it, or reach A's repo."""
    locator = "https://mcp.weather.example/mcp"
    org_a = factory.org("acme")
    repo_a = factory.repo(org_a)
    shared = factory.source(
        kind="mcp", locator=locator, vendor="weather", meta={"transport": "http"}
    )
    shared.origin = "seed"
    session.commit()
    dep_a = factory.dependency(repo_a, shared, kind="mcp", name=locator)
    factory.call_site(dep_a, file_path="agent/weather.py", line=3, operation="get_weather")
    ingest_canonical_doc(session, shared, CanonicalDoc(version="1.0.0", body=MCP_V1))
    session.commit()
    polled_at = session.get(Source, shared.id).last_polled_at
    snapshots_before = len(
        list(session.scalars(select(Snapshot).where(Snapshot.source_id == shared.id)))
    )

    org_b = factory.org("mallory", installation_id=777)
    repo_b = factory.repo(org_b, "mallory/agent")
    key_b = factory.sdk_key(org_b)
    for doc in (MCP_V1, _weather_without_tool()):
        r = client.post(
            "/v1/watch/push",
            json={
                "repo": "mallory/agent",
                "source": {"kind": "mcp", "locator": locator},
                "doc": {"version": doc["server"]["version"], "body": doc},
            },
            headers=bearer(key_b),
        )
        assert r.status_code == 200, r.text
    pushed = session.get(Source, r.json()["source_id"])
    assert pushed.id != shared.id and pushed.owner_org_id == org_b.id
    session.expire_all()
    # the shared (seeded) source is untouched: no snapshot, no event, no poll-clock move
    assert (
        len(list(session.scalars(select(Snapshot).where(Snapshot.source_id == shared.id))))
        == snapshots_before
    )
    assert (
        session.scalars(select(ChangeEventRow).where(ChangeEventRow.source_id == shared.id)).all()
        == []
    )
    assert session.get(Source, shared.id).last_polled_at == polled_at
    # the change event lives on B's pushed source, and only B's repo depends on it
    events = session.scalars(select(ChangeEventRow).where(ChangeEventRow.source_id == pushed.id))
    assert len(events.all()) == 1
    deps = session.scalars(select(Dependency).where(Dependency.source_id == pushed.id)).all()
    assert [d.repo_id for d in deps] == [repo_b.id]
    assert (
        session.scalars(
            select(Dependency).where(
                Dependency.repo_id == repo_a.id, Dependency.source_id == pushed.id
            )
        ).all()
        == []
    )


def test_watch_push_refuses_rows_it_does_not_own(client, factory, session):
    org = factory.org()
    repo = factory.repo(org)
    key = factory.sdk_key(org)
    locator = "https://mcp.weather.example/mcp"
    body = {
        "repo": REPO_FULL_NAME,
        "source": {"kind": "mcp", "locator": locator},
        "doc": {"version": "1", "body": MCP_V1},
    }
    # a customer row squatting on this org's push locator (e.g. from a repo's MCP config)
    squatter = factory.source(kind="mcp", locator=f"push:{org.id}:mcp:{locator}", vendor=None)
    r = client.post("/v1/watch/push", json=body, headers=bearer(key))
    assert r.status_code == 409, r.text
    session.expire_all()
    assert session.scalars(select(Snapshot).where(Snapshot.source_id == squatter.id)).all() == []
    assert "pushed_by" not in (session.get(Source, squatter.id).meta or {})
    session.delete(session.get(Source, squatter.id))
    session.commit()

    # this org's push source that another org's repo has been attached to
    r = client.post("/v1/watch/push", json=body, headers=bearer(key))
    assert r.status_code == 200, r.text
    pushed_id = r.json()["source_id"]
    other = factory.org("other", installation_id=31337)
    other_repo = factory.repo(other, "other/app")
    session.add(
        Dependency(repo_id=other_repo.id, source_id=pushed_id, kind="mcp", name="x", meta={})
    )
    session.commit()
    v2 = {**body, "doc": {"version": "2", "body": _mcp_v2()}}
    r = client.post("/v1/watch/push", json=v2, headers=bearer(key))
    assert r.status_code == 409
    session.expire_all()
    assert (
        session.scalars(select(ChangeEventRow).where(ChangeEventRow.source_id == pushed_id)).all()
        == []
    )
    assert session.get(Repo, repo.id).active is True


def test_watch_push_change_opens_an_impact_on_the_pushing_repo(client, factory, session):
    """api-correctness-6: a breaking change pushed from CI reaches the repo as an impact."""
    register_all_handlers()
    org = factory.org()
    repo = factory.repo(org)
    key = factory.sdk_key(org)
    base = {"repo": REPO_FULL_NAME, "source": {"kind": "mcp", "locator": "npx -y weather-mcp"}}
    r = client.post(
        "/v1/watch/push",
        json={**base, "doc": {"version": "1", "body": MCP_V1}},
        headers=bearer(key),
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/v1/watch/push",
        json={**base, "doc": {"version": "2", "body": _weather_without_tool()}},
        headers=bearer(key),
    )
    assert r.status_code == 200 and r.json()["change_event_id"], r.text
    assert drain(new_session, ["classify_event", "match_event"]) >= 2
    session.expire_all()
    impacts = session.scalars(select(Impact).where(Impact.repo_id == repo.id)).all()
    assert len(impacts) == 1
    impact = impacts[0]
    assert str(impact.change_event_id) == r.json()["change_event_id"]
    assert [s["operation"] for s in impact.matched_call_sites] == ["get_weather"]
