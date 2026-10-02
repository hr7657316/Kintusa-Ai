"""build_weekly_report numbers and items from seeded rows."""

from __future__ import annotations

from datetime import timedelta

from kintsua.core.models import (
    ChangeEventRow,
    Classification,
    Impact,
    Org,
    Repo,
    RuntimeEvent,
    Snapshot,
    Source,
)
from kintsua.core.util import utcnow
from kintsua.report.build import build_weekly_report
from kintsua.report.markdown import render_markdown, render_rss


def _event(session, source, when, ops, text=None, from_v="1", to_v="2"):
    s1 = Snapshot(
        source_id=source.id,
        content_hash="a" * 64,
        version=from_v,
        storage_ref={"kind": "inline", "body": {}},
    )
    s2 = Snapshot(
        source_id=source.id,
        content_hash="b" * 64,
        version=to_v,
        storage_ref={"kind": "inline", "body": {}},
    )
    session.add_all([s1, s2])
    session.flush()
    row = ChangeEventRow(
        source_id=source.id,
        from_snapshot_id=s1.id,
        to_snapshot_id=s2.id,
        diff={
            "source_kind": source.kind,
            "from_version": from_v,
            "to_version": to_v,
            "ops": ops,
            "raw_diff_ref": "inline",
            "text": text,
        },
        detected_at=when,
    )
    session.add(row)
    session.flush()
    return row


def _classify(session, row, severity, category, summary, **kw):
    c = Classification(
        change_event_id=row.id, severity=severity, category=category, summary=summary, **kw
    )
    session.add(c)
    session.flush()
    return c


def seed(session):
    now = utcnow()
    org = Org(github_installation_id=42, name="acme", settings={})
    session.add(org)
    session.flush()
    repo = Repo(
        org_id=org.id, github_repo_id=7, full_name="acme/agent", indexed_at=now - timedelta(days=1)
    )
    inactive_repo = Repo(org_id=org.id, github_repo_id=8, full_name="acme/old", active=False)
    session.add_all([repo, inactive_repo])
    stripe = Source(
        kind="openapi",
        locator="https://stripe/spec.json",
        display_name="Stripe API",
        vendor="stripe",
        origin="seed",
    )
    mcp = Source(
        kind="mcp",
        locator="https://weather/mcp",
        display_name="Weather MCP",
        vendor="weather",
        last_polled_at=now - timedelta(hours=1),
        origin="seed",
    )
    pypi = Source(
        kind="pypi",
        locator="openai",
        display_name="openai (PyPI)",
        vendor="openai",
        last_polled_at=now - timedelta(days=30),
        origin="seed",
    )
    dead = Source(
        kind="changelog",
        locator="https://gone/changelog",
        display_name="Gone changelog",
        vendor="gone",
        consecutive_failures=12,
        last_error="HTTPStatusError: 404",
        last_polled_at=now - timedelta(days=9),
        origin="seed",
    )
    off = Source(kind="npm", locator="left-pad", display_name="left-pad", active=False)
    session.add_all([stripe, mcp, pypi, dead, off])
    session.flush()

    breaking = _event(
        session,
        stripe,
        now - timedelta(days=1),
        [{"op": "removed", "path": "/paths/~1v1~1charges/post"}],
        from_v="2026-01",
        to_v="2026-02",
    )
    _classify(
        session,
        breaking,
        "breaking",
        "removal",
        "POST /v1/charges was removed",
        affected_paths=["POST /v1/charges"],
        rule_hits=["removed_operation"],
        migration_hint="Use /v1/payment_intents",
    )
    safe = _event(session, mcp, now - timedelta(days=2), [{"op": "added", "path": "/tools/lookup"}])
    _classify(
        session, safe, "safe", "docs_only", "A new tool lookup was added", rule_hits=["added_only"]
    )
    human = _event(
        session,
        pypi,
        now - timedelta(days=3),
        [{"op": "text_changed", "path": "/latest", "before": "1.0.0", "after": "1.1.0"}],
        text="release notes",
    )
    _classify(
        session,
        human,
        "unknown",
        "behaviour",
        "Release notes mention retries",
        llm_model="fake",
        llm_confidence=0.4,
        needs_human=True,
    )
    unclassified = _event(
        session,
        mcp,
        now - timedelta(hours=5),
        [
            {"op": "required_added", "path": "/tools/get_weather/inputSchema/properties/units"},
            {"op": "text_changed", "path": "/tools/get_weather/description"},
        ],
    )
    _event(
        session, stripe, now - timedelta(days=10), [{"op": "removed", "path": "/paths/~1old"}]
    )  # outside the window

    impact = Impact(
        repo_id=repo.id,
        change_event_id=breaking.id,
        classification_id=breaking.classification.id,
        status="merged",
        github_issue_number=11,
        github_pr_number=12,
        pr_outcome="merged_as_is",
        resolved_at=now - timedelta(hours=2),
    )
    draft = Impact(repo_id=repo.id, change_event_id=safe.id, status="detected")
    session.add_all([impact, draft])
    session.add_all(
        [
            RuntimeEvent(
                repo_id=repo.id,
                sdk_key_hash="k",
                tool_name="get_weather",
                outcome="schema_mismatch",
                observed_at=now - timedelta(hours=1),
            ),
            RuntimeEvent(
                repo_id=repo.id,
                sdk_key_hash="k",
                tool_name="get_weather",
                outcome="ok",
                observed_at=now - timedelta(hours=1),
            ),
            RuntimeEvent(
                repo_id=repo.id,
                sdk_key_hash="k",
                tool_name="get_weather",
                outcome="error",
                observed_at=now - timedelta(days=20),
            ),
        ]
    )
    session.flush()
    return {"now": now, "repo": repo, "breaking": breaking, "unclassified": unclassified}


def test_build_weekly_report_numbers_and_items(session):
    ctx = seed(session)
    until = ctx["now"] + timedelta(minutes=1)
    report = build_weekly_report(session, until - timedelta(days=7), until)
    st = report.stats
    assert st.sources_watched == 4  # the inactive npm source is excluded
    assert st.sources_polled == 1  # only the MCP source was polled inside the window
    assert st.change_events == 4
    assert (st.breaking, st.degraded, st.safe, st.unknown) == (1, 0, 1, 2)
    assert st.needs_human == 1
    assert st.repos_installed == 1 and st.repos_active == 1
    assert st.issues_opened == 1 and st.prs_opened == 1
    assert st.prs_merged == 1 and st.prs_merged_as_is == 1 and st.merged_as_is_rate == 1.0
    assert st.runtime_events == 2 and st.runtime_mismatches == 1

    assert len(report.items) == 4
    by_id = {it.change_event_id: it for it in report.items}
    b = by_id[str(ctx["breaking"].id)]
    assert b.severity == "breaking" and b.vendor == "stripe" and b.source_kind == "openapi"
    assert b.from_version == "2026-01" and b.to_version == "2026-02" and b.op_count == 1
    assert b.url is None  # the public build never links a customer repo's issue / PR
    assert report.public is True
    assert b.migration_hint == "Use /v1/payment_intents" and b.rule_hits == ["removed_operation"]
    u = by_id[str(ctx["unclassified"].id)]
    assert u.severity == "unknown" and u.category == "unclassified"
    assert "awaiting classification" in u.summary and u.op_count == 2
    assert u.affected_paths == [
        "/tools/get_weather/inputSchema/properties/units",
        "/tools/get_weather/description",
    ]
    assert [it.detected_at for it in report.items] == sorted(
        (it.detected_at for it in report.items), reverse=True
    )
    assert report.items[0].change_event_id == str(ctx["unclassified"].id)  # newest first
    assert sum(1 for it in report.items if it.needs_human) == 1

    assert [f.display_name for f in report.failing_sources] == ["Gone changelog"]
    assert report.failing_sources[0].consecutive_failures == 12
    assert any("not yet classified" in n for n in report.notes)
    assert any("failing for 7+ days" in n for n in report.notes)

    md = render_markdown(report)
    assert "## 🔴 Breaking (1)" in md and "POST /v1/charges was removed" in md
    assert "## 🟢 Safe (1)" in md
    assert "Needs a human look (1)" in md and "Release notes mention retries" in md
    assert "Gone changelog" in md and "HTTPStatusError: 404" in md
    assert "| Merged as-is rate | 100% |" in md
    rss = render_rss(report, "https://kintsua.example")
    assert "<item>" in rss and "[breaking] stripe" in rss


def test_empty_window_is_empty(session):
    seed(session)
    until = utcnow() - timedelta(days=60)
    report = build_weekly_report(session, until - timedelta(days=7), until)
    assert report.items == [] and report.stats.change_events == 0 and report.stats.breaking == 0
    assert report.stats.sources_watched == 4
    assert "Change events | 0" in render_markdown(report)
