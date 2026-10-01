"""match_runtime: runtime discovery, promotion of schema mismatches, error-spike comments."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from kintsua.core.db import new_session
from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Classification,
    Dependency,
    Impact,
    Job,
    Source,
)
from kintsua.core.queue import drain, enqueue
from kintsua.core.util import utcnow
from kintsua.matcher.issues import RUNTIME_COMMENT_MARKER, create_issue_for_impact
from kintsua.matcher.runtime import (
    http_host,
    match_runtime,
    observed_locator,
    runtime_kind,
    runtime_operation,
    synthesize_observed_event,
)
from tests.matcher.conftest import Factory

MISMATCH_SAMPLE = {
    "diff": [
        {
            "op": "required_added",
            "path": "/required",
            "before": ["location"],
            "after": ["location", "units"],
        },
        {
            "op": "type_changed",
            "path": "/properties/location/type",
            "before": "string",
            "after": "object",
        },
    ],
    "previous_fingerprint": "f" * 64,
}


def _deps(session, repo):
    return {
        (d.kind, d.name): d
        for d in session.scalars(select(Dependency).where(Dependency.repo_id == repo.id))
    }


def _sites(session, repo):
    return list(
        session.scalars(
            select(CallSite)
            .join(Dependency, Dependency.id == CallSite.dependency_id)
            .where(Dependency.repo_id == repo.id)
        )
    )


def test_event_shape_helpers(f: Factory, session):
    org = f.org()
    repo = f.repo(org)
    http = f.runtime_event(repo, "GET api.stripe.com/v1/charges/ch_1Nxyz123456789")
    mcp = f.runtime_event(repo, "get_weather")
    assert runtime_kind(http) == "http" and runtime_kind(mcp) == "mcp"
    assert (
        runtime_operation(http) == "GET /v1/charges/{id}"
        and runtime_operation(mcp) == "get_weather"
    )
    assert http_host("GET https://api.stripe.com/v1/charges") == "api.stripe.com"
    assert http_host("get_weather") is None
    explicit = f.runtime_event(repo, "weird name", sample={"kind": "http"})
    assert runtime_kind(explicit) == "http"


def test_discovery_creates_runtime_dependencies_and_call_sites_once(
    session, fake_github, f: Factory
):
    org = f.org()
    repo = f.repo(org)
    # the indexer already knows this tool: no new rows for it
    known_src = f.source(
        kind="mcp", locator="https://mcp.known.example/mcp", display_name="Known", vendor=None
    )
    known = f.dependency(repo, source=known_src, kind="mcp", name="https://mcp.known.example/mcp")
    f.call_site(known, "search_docs")
    now = utcnow()
    f.runtime_event(repo, "search_docs", observed_at=now)
    f.runtime_event(
        repo, "get_weather", observed_at=now, sample={"arg_keys": ["location", "units"]}
    )
    f.runtime_event(
        repo, "list_rooms", observed_at=now, sample={"server": "https://mcp.rooms.example/mcp"}
    )
    f.runtime_event(repo, "GET api.stripe.com/v1/charges/ch_1Nxyz123456789", observed_at=now)
    f.runtime_event(
        repo, "GET api.stripe.com/v1/charges/ch_2Nxyz123456789", observed_at=now
    )  # same op
    f.runtime_event(repo, "POST api.stripe.com/v1/charges", observed_at=now)
    f.runtime_event(repo, "GET api.unknown-vendor.example/v2/things", observed_at=now)
    session.commit()

    res = match_runtime(session, repo, now=now)
    assert res.events_seen == 7
    assert res.dependencies_created == 4 and res.call_sites_created == 5
    deps = _deps(session, repo)
    assert set(deps) == {
        ("mcp", "https://mcp.known.example/mcp"),
        ("mcp", "get_weather"),
        ("mcp", "https://mcp.rooms.example/mcp"),
        ("http", "api.stripe.com"),
        ("http", "api.unknown-vendor.example"),
    }
    weather = deps[("mcp", "get_weather")]
    assert weather.discovered_at_runtime and weather.source_id is None
    rooms = deps[("mcp", "https://mcp.rooms.example/mcp")]
    assert rooms.source_id is not None
    rooms_src = session.get(Source, rooms.source_id)
    assert (
        rooms_src.kind == "mcp"
        and rooms_src.locator == "https://mcp.rooms.example/mcp"
        and rooms_src.active
    )
    stripe = deps[("http", "api.stripe.com")]
    assert stripe.meta["vendor"] == "stripe" and stripe.meta["host"] == "api.stripe.com"
    stripe_src = session.get(Source, stripe.source_id)
    assert stripe_src.kind == "openapi" and stripe_src.vendor == "stripe"
    assert deps[("http", "api.unknown-vendor.example")].source_id is None

    runtime_sites = [cs for cs in _sites(session, repo) if cs.discovered_at_runtime]
    assert {(cs.dependency_id, cs.operation) for cs in runtime_sites} == {
        (weather.id, "get_weather"),
        (rooms.id, "list_rooms"),
        (stripe.id, "GET /v1/charges/{id}"),
        (stripe.id, "POST /v1/charges"),
        (deps[("http", "api.unknown-vendor.example")].id, "GET /v2/things"),
    }
    ws = next(cs for cs in runtime_sites if cs.operation == "get_weather")
    assert ws.file_path == "<runtime>" and ws.line == 0 and ws.symbol == "get_weather"
    assert ws.args_shape == {"keys": ["location", "units"]}
    assert all(
        not cs.discovered_at_runtime
        for cs in _sites(session, repo)
        if cs.operation == "search_docs"
    )

    # idempotent
    again = match_runtime(session, repo, now=now)
    assert again.dependencies_created == 0 and again.call_sites_created == 0
    assert len(_sites(session, repo)) == 6


def test_promotion_creates_observed_event_and_impact_exactly_once(
    session, fake_github, f: Factory, settings
):
    org = f.org()
    repo = f.repo(org)
    now = utcnow()
    f.runtime_event(
        repo,
        "get_weather",
        outcome="ok",
        fingerprint="f" * 64,
        observed_at=now - timedelta(hours=5),
    )
    for i in range(settings.runtime_promotion_threshold):
        f.runtime_event(
            repo,
            "get_weather",
            outcome="schema_mismatch",
            fingerprint="a" * 64,
            sample=MISMATCH_SAMPLE,
            observed_at=now - timedelta(minutes=30 - i),
        )
    # below the threshold: not promoted
    for i in range(settings.runtime_promotion_threshold - 1):
        f.runtime_event(
            repo,
            "get_forecast",
            outcome="schema_mismatch",
            fingerprint="b" * 64,
            observed_at=now - timedelta(minutes=i + 1),
        )
    session.commit()

    res = match_runtime(session, repo, now=now)
    assert len(res.promoted) == 1 and len(res.impacts) == 1
    row = session.get(ChangeEventRow, res.promoted[0])
    assert row.diff["source_kind"] == "observed"
    assert row.diff["from_version"] == "f" * 64 and row.diff["to_version"] == "a" * 64
    paths = [op["path"] for op in row.diff["ops"]]
    assert paths == [
        "/tools/get_weather/inputSchema/required",
        "/tools/get_weather/inputSchema/properties/location/type",
    ]
    src = session.get(Source, row.source_id)
    assert (
        src.kind == "mcp"
        and src.locator == observed_locator(repo, "get_weather")
        and not src.active
    )
    cls = session.scalar(select(Classification).where(Classification.change_event_id == row.id))
    assert cls.severity == "breaking" and cls.category == "behaviour"
    assert cls.rule_hits == ["runtime_promotion"] and cls.affected_paths == ["tool:get_weather"]
    assert (
        cls.summary
        == "Tool get_weather answers with a different schema than yesterday (3 mismatches in 24h, undocumented upstream change)"
    )
    imp = session.get(Impact, res.impacts[0])
    assert (
        imp.status == "detected"
        and imp.change_event_id == row.id
        and imp.classification_id == cls.id
    )
    assert [s["file_path"] for s in imp.matched_call_sites] == ["<runtime>"]
    assert imp.matched_call_sites[0]["operation"] == "get_weather"
    dep = _deps(session, repo)[("mcp", "get_weather")]
    assert dep.source_id == src.id  # the runtime dependency now points at the observed source
    assert (
        session.scalars(select(Job).where(Job.kind == "open_issue")).all() == []
    )  # auto_issue off

    # a second run (more mismatches with the same fingerprint) promotes nothing new
    f.runtime_event(
        repo,
        "get_weather",
        outcome="schema_mismatch",
        fingerprint="a" * 64,
        sample=MISMATCH_SAMPLE,
        observed_at=now,
    )
    session.commit()
    again = match_runtime(session, repo, now=now + timedelta(minutes=1))
    assert again.promoted == [] and again.impacts == []
    assert len(session.scalars(select(ChangeEventRow)).all()) == 1
    assert len(session.scalars(select(Impact)).all()) == 1


def test_promotion_skipped_when_a_documented_change_exists(
    session, fake_github, f: Factory, settings
):
    org = f.org()
    repo = f.repo(org)
    weather = f.source(
        kind="mcp",
        locator="https://mcp.weather.example/mcp",
        display_name="Weather MCP",
        vendor=None,
    )
    dep = f.dependency(repo, source=weather, kind="mcp", name="https://mcp.weather.example/mcp")
    f.call_site(dep, "get_weather")
    now = utcnow()
    f.event(
        weather,
        [{"op": "removed", "path": "/tools/get_weather", "before": {}, "after": None}],
        detected_at=now - timedelta(hours=2),
    )
    for i in range(settings.runtime_promotion_threshold):
        f.runtime_event(
            repo,
            "get_weather",
            outcome="schema_mismatch",
            fingerprint="a" * 64,
            observed_at=now - timedelta(minutes=i),
        )
    session.commit()
    res = match_runtime(session, repo, now=now)
    assert res.promoted == [] and res.impacts == []
    assert len(session.scalars(select(ChangeEventRow)).all()) == 1  # only the documented one


def test_promotion_opens_issue_when_auto_issue_is_on(session, fake_github, f: Factory, settings):
    org = f.org(auto_issue=True)
    repo = f.repo(org)
    now = utcnow()
    for i in range(settings.runtime_promotion_threshold):
        f.runtime_event(
            repo,
            "get_weather",
            outcome="schema_mismatch",
            fingerprint="a" * 64,
            sample={"server": "https://mcp.weather.example/mcp"},
            observed_at=now - timedelta(minutes=i),
        )
    session.commit()
    enqueue(
        session, "match_runtime", {"repo_id": str(repo.id)}, dedupe_key=f"match_runtime:{repo.id}"
    )
    session.commit()
    assert drain(new_session, ["match_runtime"]) == 1
    assert drain(new_session, ["open_issue"]) == 1
    s2 = new_session()
    try:
        imp = s2.scalar(select(Impact))
        assert imp.status == "issued" and imp.github_issue_number == 1
        src = s2.scalar(select(Source).where(Source.kind == "mcp"))
        assert (
            src.locator == "https://mcp.weather.example/mcp" and src.active
        )  # a real server is watched
    finally:
        s2.close()
    issue = fake_github.repo("acme/support-agent").issues[1]
    assert (
        issue.title
        == "[Kintsua] Tool get_weather answers with a different schema than yesterday (3 mismatches in 24h, undocumented upstream change)"
    )
    assert "(observed at runtime)" in issue.body and "runtime (observed by the SDK)" in issue.body
    assert {lb.name for lb in issue.labels} == {"kintsua", "severity:breaking"}


def test_synthesize_without_sample_diffs_uses_text_changed(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    evs = [
        f.runtime_event(repo, "get_weather", outcome="schema_mismatch", fingerprint="c" * 64)
        for _ in range(3)
    ]
    row, cls = synthesize_observed_event(session, repo, "get_weather", evs)
    assert row.diff["ops"] == [
        {
            "op": "text_changed",
            "path": "/tools/get_weather/inputSchema",
            "before": None,
            "after": "c" * 64,
        }
    ]
    assert row.diff["to_version"] == "c" * 64 and cls.severity == "breaking"


def test_error_spike_comment_is_created_then_edited(session, fake_github, f: Factory):
    org = f.org()
    repo = f.repo(org)
    spec = f.source()
    dep = f.dependency(repo, source=spec, meta={"vendor": "stripe"})
    f.call_site(dep, "POST /v1/charges", file_path="billing.py", line=3)
    event = f.event(spec)
    f.classification(event)
    session.commit()
    from kintsua.matcher.match import match_change_event

    (imp,) = match_change_event(session, event)
    create_issue_for_impact(session, imp)
    issue = fake_github.repo("acme/support-agent").issues[1]
    now = utcnow()
    for i in range(5):
        f.runtime_event(
            repo,
            "POST api.stripe.com/v1/charges",
            outcome="error",
            error_class="HTTPStatusError",
            observed_at=now - timedelta(minutes=i + 1),
            sample={"status": 400},
        )
    f.runtime_event(
        repo,
        "POST api.stripe.com/v1/charges",
        outcome="error",
        error_class="Timeout",
        observed_at=now - timedelta(hours=3),
    )  # too old
    f.runtime_event(
        repo,
        "GET api.stripe.com/v1/customers",
        outcome="error",
        error_class="HTTPStatusError",
        observed_at=now,
    )  # other op
    session.commit()

    res = match_runtime(session, repo, now=now)
    assert res.error_spikes == {str(imp.id): 5}
    session.refresh(imp)
    assert imp.runtime_error_count == 5
    assert len(issue.comments) == 1
    assert (
        issue.comments[0].body
        == f"{RUNTIME_COMMENT_MARKER}\n**Runtime evidence:** 5 failed calls in the last hour (latest error class: HTTPStatusError)"
    )

    # unchanged count: no GitHub write
    match_runtime(session, repo, now=now)
    assert len(issue.comments) == 1

    # more failures: the same comment is edited, no second comment
    for i in range(2):
        f.runtime_event(
            repo,
            "POST api.stripe.com/v1/charges",
            outcome="error",
            error_class="ValidationError<x>",
            observed_at=now + timedelta(seconds=i + 1),
        )
    session.commit()
    res = match_runtime(session, repo, now=now + timedelta(seconds=5))
    assert res.error_spikes == {str(imp.id): 7}
    assert len(issue.comments) == 1
    assert issue.comments[0].body.endswith(
        "7 failed calls in the last hour (latest error class: ValidationError&lt;x&gt;)"
    )
    session.refresh(imp)
    assert imp.runtime_error_count == 7

    # the spike subsides: the count drops to 0 and the comment is left alone
    res = match_runtime(session, repo, now=now + timedelta(hours=2))
    assert res.error_spikes == {}
    session.refresh(imp)
    assert imp.runtime_error_count == 0 and len(issue.comments) == 1
