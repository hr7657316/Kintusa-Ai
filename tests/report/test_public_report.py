"""The public weekly report (docs/CONTRACTS.md rule 5; watchers-security-2,
core-security-7): only operator-seeded sources, no customer repo names or issue/PR links,
redacted errors; the job and ``read_latest`` only ever serve that build."""

from __future__ import annotations

import json
from datetime import timedelta

from kintsua.core.models import ChangeEventRow, Classification, Impact, Org, Repo, Snapshot, Source
from kintsua.core.util import utcnow
from kintsua.report.build import build_weekly_report
from kintsua.report.markdown import render_markdown, render_rss
from kintsua.report.run import main as report_main
from kintsua.report.run import read_latest, weekly_report, write_report

SECRET = "ghp_" + "a" * 36


def _event(session, source, when, summary):
    snaps = [
        Snapshot(
            source_id=source.id,
            content_hash=c * 64,
            version=v,
            storage_ref={"kind": "inline", "body": {}},
        )
        for c, v in (("a", "1"), ("b", "2"))
    ]
    session.add_all(snaps)
    session.flush()
    row = ChangeEventRow(
        source_id=source.id,
        from_snapshot_id=snaps[0].id,
        to_snapshot_id=snaps[1].id,
        diff={"source_kind": source.kind, "ops": [{"op": "removed", "path": "/paths/~1x/get"}]},
        detected_at=when,
    )
    session.add(row)
    session.flush()
    cls = Classification(
        change_event_id=row.id, severity="breaking", category="removal", summary=summary
    )
    session.add(cls)
    session.flush()
    return row, cls


def world(session):
    now = utcnow()
    org = Org(github_installation_id=9, name="acme", settings={})
    session.add(org)
    session.flush()
    repo = Repo(org_id=org.id, github_repo_id=10, full_name="acme/secret-repo", private=True)
    session.add(repo)
    vendor = Source(
        kind="openapi",
        locator="https://raw.vendor.example/openapi.json",
        display_name="Vendor API",
        vendor="vendor",
        origin="seed",
        consecutive_failures=3,
        last_error=f"HTTPStatusError: 500 (token {SECRET})",
        last_polled_at=now - timedelta(days=9),
    )
    internal = Source(
        kind="openapi",
        locator="https://internal.acme.corp/spec.json",
        display_name="https://internal.acme.corp/spec.json",
        origin="customer",
        consecutive_failures=4,
        last_error="ConnectError: internal.acme.corp refused",
        last_polled_at=now - timedelta(days=10),
    )
    observed = Source(
        kind="mcp", locator="observed:acme/secret-repo:get_x", display_name="x", origin="observed"
    )
    session.add_all([vendor, internal, observed])
    session.flush()
    v_row, v_cls = _event(session, vendor, now - timedelta(days=1), "GET /x was removed")
    c_row, c_cls = _event(session, internal, now - timedelta(days=1), "internal billing removed")
    _event(session, observed, now - timedelta(hours=3), "get_x drifted at runtime")
    for row, cls, n in ((v_row, v_cls, 3), (c_row, c_cls, 4)):
        session.add(
            Impact(
                repo_id=repo.id,
                change_event_id=row.id,
                classification_id=cls.id,
                status="issued",
                github_issue_number=n,
            )
        )
    session.flush()
    return now


def test_public_build_has_only_seeded_sources_and_no_customer_links(session):
    now = world(session)
    report = build_weekly_report(session, now - timedelta(days=7), now + timedelta(minutes=1))
    assert report.public is True
    assert [it.summary for it in report.items] == ["GET /x was removed"]
    assert all(it.url is None for it in report.items)
    assert [f.display_name for f in report.failing_sources] == ["Vendor API"]
    assert SECRET not in (report.failing_sources[0].last_error or "")
    assert report.stats.change_events == 1 and report.stats.breaking == 1
    for text in (
        render_markdown(report),
        render_rss(report, "https://kintsua.example"),
        json.dumps(report.model_dump(mode="json")),
    ):
        assert "internal.acme.corp" not in text and "acme/secret-repo" not in text
        assert "internal billing removed" not in text and "drifted at runtime" not in text
        assert SECRET not in text

    # the operator's unfiltered view still has everything
    full = build_weekly_report(
        session, now - timedelta(days=7), now + timedelta(minutes=1), public_only=False
    )
    assert full.public is False and len(full.items) == 3
    assert any(it.url == "https://github.com/acme/secret-repo/issues/4" for it in full.items)
    assert {f.display_name for f in full.failing_sources} == {
        "Vendor API",
        "https://internal.acme.corp/spec.json",
    }


def test_job_publishes_the_public_build_and_private_builds_never_replace_it(
    session, settings, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    now = world(session)
    weekly_report(session, {})
    text, data = read_latest(tmp_path)
    assert data["public"] is True and "internal.acme.corp" not in text
    assert "acme/secret-repo" not in json.dumps(data)

    private = build_weekly_report(
        session, now - timedelta(days=7), now + timedelta(minutes=1), public_only=False
    )
    paths = write_report(private, tmp_path)
    assert paths["dated"].name.endswith(".private.md")
    text, data = read_latest(tmp_path)
    assert data["public"] is True and "internal.acme.corp" not in text


def test_read_latest_refuses_a_report_not_marked_public(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "latest.md").write_text("# old report with https://internal.acme.corp\n", "utf-8")
    (reports / "latest.json").write_text(json.dumps({"items": []}), "utf-8")
    assert read_latest(tmp_path) == (None, None)


def test_report_cli_is_public_unless_asked(session, db, capsys):
    world(session)
    session.commit()
    assert report_main(["--since", "7d"]) == 0
    out = capsys.readouterr().out
    assert "Vendor API" in out and "internal.acme.corp" not in out
    assert report_main(["--since", "7d", "--include-private"]) == 0
    assert "internal.acme.corp" in capsys.readouterr().out
