"""The public weekly report page (html / md / rss / json), subscriptions and the fallback
Markdown converter."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from kintsua.api.report import invalidate_cache, minimal_markdown, render_inline
from kintsua.core.models import Subscriber
from kintsua.core.util import utcnow


def test_weekly_report_formats_built_on_the_fly_and_cached(
    client, factory, settings, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    src = factory.source()
    event = factory.change_event(src)
    factory.classification(event)

    r = client.get("/report/weekly?format=md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert r.text.startswith("# Kintsua weekly drift report") and "currency" in r.text

    r = client.get("/report/weekly?format=json")
    assert r.status_code == 200
    data = r.json()
    assert data["stats"]["change_events"] == 1 and data["items"][0]["severity"] == "breaking"
    assert data["items"][0]["vendor"] == "stripe"

    r = client.get("/report/weekly?format=rss")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/rss+xml")
    assert "<rss" in r.text and "[breaking] stripe" in r.text

    r = client.get("/report/weekly")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "<h1>Kintsua weekly drift report" in r.text
    assert "<table>" in r.text and "<strong>stripe</strong>" in r.text
    assert "built live" in r.text and 'hx-post="/report/subscribe"' in r.text
    assert "unpkg.com/htmx.org" in r.text

    # cached for ten minutes: a new event is not visible until the cache is dropped
    factory.change_event(src, to_version="2026-10-15")
    assert client.get("/report/weekly?format=json").json()["stats"]["change_events"] == 1
    invalidate_cache()
    assert client.get("/report/weekly?format=json").json()["stats"]["change_events"] == 2

    assert client.get("/report/weekly?format=pdf").status_code == 400
    # Accept negotiation
    r = client.get("/report/weekly", headers={"Accept": "application/rss+xml"})
    assert r.headers["content-type"].startswith("application/rss+xml")
    r = client.get("/report/weekly", headers={"Accept": "application/json"})
    assert r.headers["content-type"].startswith("application/json")


def test_weekly_report_serves_the_published_files(
    client, session, factory, settings, monkeypatch, tmp_path
):
    from kintsua.report.build import build_weekly_report
    from kintsua.report.run import write_report

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    src = factory.source()
    event = factory.change_event(src)
    factory.classification(event, summary="Published summary line")
    until = utcnow()
    report = build_weekly_report(session, until - timedelta(days=7), until)
    report.notes.append("hand-written note from the job")
    write_report(report, tmp_path)

    r = client.get("/report/weekly")
    assert r.status_code == 200 and "hand-written note from the job" in r.text
    assert "Published summary line" in r.text and "published" in r.text
    assert "hand-written note from the job" in client.get("/report/weekly?format=md").text
    assert client.get("/report/weekly?format=json").json()["notes"] == [
        "hand-written note from the job"
    ]
    assert "Published summary line" in client.get("/report/weekly?format=rss").text

    # a rewritten file is picked up (mtime changes)
    report.notes.append("second note")
    write_report(report, tmp_path)
    assert "second note" in client.get("/report/weekly?format=md").text


def test_subscribe_is_validated_and_idempotent(client, session):
    r = client.post("/report/subscribe", json={"email": "Lead@Example.com"})
    assert r.status_code == 200
    assert r.json() == {"subscribed": True, "email": "lead@example.com", "created": True}
    r = client.post("/report/subscribe", json={"email": "lead@example.com "})
    assert r.status_code == 200 and r.json()["created"] is False
    assert session.scalar(select(func.count(Subscriber.id))) == 1
    assert client.post("/report/subscribe", json={"email": "nope"}).status_code == 422
    assert client.post("/report/subscribe", json={}).status_code == 422

    r = client.post(
        "/report/subscribe", data={"email": "cto@example.com"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200 and "Subscribed" in r.text and "cto@example.com" in r.text
    r = client.post(
        "/report/subscribe", data={"email": "x@example.com"}, headers={"Accept": "text/html"}
    )
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert session.scalar(select(func.count(Subscriber.id))) == 3


def test_minimal_markdown_converter():
    md = (
        "# Title\n\n"
        "_sub_ **bold** `code` [link](https://x.example/a) [bad](javascript:alert(1))\n\n"
        "| a | b |\n|---|---|\n| 1 | **2** |\n\n"
        "- one\n  - nested\n- two\n\n"
        "1. first\n2. second\n\n"
        "```\n<raw> & stuff\n```\n\n---\n"
        "## Sub\n\nline one\nline two\n"
    )
    html = minimal_markdown(md)
    flat = html.replace("\n", "")
    assert "<h1>Title</h1>" in html and "<h2>Sub</h2>" in html
    assert (
        "<em>sub</em>" in html and "<strong>bold</strong>" in html and "<code>code</code>" in html
    )
    assert '<a href="https://x.example/a" rel="noopener">link</a>' in html
    assert "javascript:" not in html
    assert (
        "<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td><strong>2</strong></td></tr></tbody></table>"
        in flat
    )
    assert "<ul><li>one<ul><li>nested</li></ul></li><li>two</li></ul>" in flat
    assert "<ol><li>first</li><li>second</li></ol>" in flat
    assert "<pre><code>&lt;raw&gt; &amp; stuff</code></pre>" in html
    assert "<hr>" in html
    assert "<p>line one line two</p>" in html
    # HTML in the source is escaped, never rendered
    assert "<script>" not in minimal_markdown("hello <script>alert(1)</script> there")
    assert render_inline("a <b> `x<y>` c") == "a &lt;b&gt; <code>x&lt;y&gt;</code> c"
    # the real report renders headings, the summary table and the item lists
    from kintsua.report.markdown import ReportItem, WeeklyReport, render_markdown

    now = utcnow()
    report = WeeklyReport(
        since=now - timedelta(days=7),
        until=now,
        generated_at=now,
        items=[
            ReportItem(
                source_name="Stripe API",
                source_kind="openapi",
                vendor="stripe",
                severity="breaking",
                category="required_param",
                summary="POST /v1/charges requires currency",
                detected_at=now,
                affected_paths=["POST /v1/charges"],
                migration_hint="pass currency",
            )
        ],
    )
    out = minimal_markdown(render_markdown(report))
    assert "<h2>🔴 Breaking (1)</h2>" in out and "<li><strong>stripe</strong>" in out
    assert "<td>Sources watched</td>" in out and "<code>POST /v1/charges</code>" in out
