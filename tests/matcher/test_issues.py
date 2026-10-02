"""Issue body rendering, idempotent creation, labels, comments and closing."""

from __future__ import annotations

from kintsua.core.models import Impact
from kintsua.matcher.issues import (
    build_issue_body,
    close_impact_issue,
    comment_on_impact,
    create_issue_for_impact,
    plain,
    upsert_impact_comment,
)
from kintsua.matcher.match import match_change_event
from tests.matcher.conftest import CHARGES_BODY, Factory


def test_plain_neutralises_markdown_and_html():
    s = plain("<script>alert(1)</script> **bold** [x](http://evil) # heading\n- item `code`")
    assert "<" not in s and ">" not in s
    assert "**" not in s and "[x](" not in s and "\\[x\\](http:\u200b//evil)" in s
    assert "`code`" not in s and "\\`code\\`" in s
    assert "\n" not in s
    assert s.startswith("&lt;script&gt;")
    assert plain("- leading dash") == "\\- leading dash"
    assert plain("x" * 50, max_len=10).endswith("…") and len(plain("x" * 50, max_len=10)) == 10


def test_plain_defuses_mentions_and_autolinks():
    # GitHub release notes quoted in an issue must not ping upstream contributors or plant
    # clickable links in the customer's repo
    s = plain("Fix retries by @octocat in https://x.example/y see www.z.example or WWW.Q.example")
    for live in ("@octocat", "https://", "www.z", "WWW.Q"):
        assert live not in s, live
    # the text still reads the same (zero-width spaces are invisible)
    assert s.replace("\u200b", "") == (
        "Fix retries by @octocat in https://x.example/y see www.z.example or WWW.Q.example"
    )
    assert plain("dev@example.com") == "dev@\u200bexample.com"


def _issued_world(session, f: Factory, **cls_kw):
    org = f.org(auto_issue=True, fix_mode="pr")
    repo = f.repo(org, default_branch="develop")
    spec = f.source()
    dep = f.dependency(repo, source=spec, meta={"vendor": "stripe"})
    f.call_site(dep, "POST /v1/charges", file_path="src/pay me.py", line=12, symbol="requests.post")
    ops = [
        {
            "op": "required_added",
            "path": f"{CHARGES_BODY}/required",
            "before": ["amount"],
            "after": ["amount", "currency"],
        },
        {
            "op": "text_changed",
            "path": "/paths/~1v1~1customers/get/description",
            "before": "a",
            "after": "b",
        },
    ] + [
        {
            "op": "added",
            "path": f"{CHARGES_BODY}/properties/extra{i}",
            "before": None,
            "after": {"type": "string"},
        }
        for i in range(20)
    ]
    event = f.event(spec, ops, text="<b>release</b> notes _here_")
    kw = {
        "summary": "**bold** <img src=x onerror=alert(1)> currency required",
        "migration_hint": "Add `currency` to every [call](http://x) <br>",
    }
    kw.update(cls_kw)
    cls = f.classification(event, **kw)
    session.commit()
    (impact,) = match_change_event(session, event)
    return org, repo, spec, event, cls, impact


def test_build_issue_body_sections_and_escaping(session, fake_github, f: Factory):
    org, repo, spec, event, cls, impact = _issued_world(session, f)
    impact.runtime_error_count = 12
    title, body = build_issue_body(impact, event, cls, repo, source=spec, org=org)

    assert title == "[Kintsua] **bold** <img src=x onerror=alert(1)> currency required"
    assert body.startswith(f"<!-- kintsua:change_event:{event.id} -->")
    # headings in order
    idx = [
        body.index(h)
        for h in (
            "## What changed upstream",
            "## Where this repo calls it",
            "## Suggested migration",
            "## Runtime evidence",
            "## Status",
        )
    ]
    assert idx == sorted(idx)
    # untrusted text is inert
    assert "<img" not in body and "<b>" not in body and "<br>" not in body
    assert "\\*\\*bold\\*\\*" in body and "&lt;img src=x onerror=alert(1)&gt;" in body
    assert "[call](http://x)" not in body and "\\[call\\](http:\u200b//x)" in body
    # upstream facts
    assert "**Stripe API** (openapi) · `2024-06-20` → `2024-09-30` · detected" in body
    assert "required\\_added at" in body and '`["amount"]` → `["amount","currency"]`' in body
    # only the ops for the matched operation are shown (21 of 22), capped at 15 + "+N more"
    assert "~1v1~1customers" not in body
    assert "_+6 more_" in body
    # call-site table with a link to the default branch and the path url-encoded
    assert (
        "| [src/pay me.py:12](https://github.com/acme/support-agent/blob/develop/src/pay%20me.py#L12) | `requests.post` | `POST /v1/charges` |"
        in body
    )
    assert "**12** failed calls in the last hour" in body
    assert "Kintsua will open a PR within an hour." in body
    assert "mute: add `stripe:POST /v1/charges` to .kintsua.yml" in body
    assert f"change event `{event.id}`" in body


def test_title_is_truncated_to_240_chars(session, fake_github, f: Factory):
    org, repo, spec, event, cls, impact = _issued_world(session, f)
    cls.summary = "x" * 500
    title, _ = build_issue_body(impact, event, cls, repo, source=spec, org=org)
    assert len(title) == 240 and title.startswith("[Kintsua] xxx") and title.endswith("…")


def test_issue_only_status_line_and_none_yet_hint(session, fake_github, f: Factory):
    org, repo, spec, event, cls, impact = _issued_world(session, f, migration_hint=None)
    org.settings = {"fix_mode": "issue_only"}
    _, body = build_issue_body(impact, event, cls, repo, source=spec, org=org)
    assert "Fix mode is issue_only; switch it in .kintsua.yml or the settings page." in body
    assert "## Suggested migration\n\nnone yet" in body
    assert "## Runtime evidence" not in body


def test_create_issue_is_idempotent_and_reuses_marker(session, fake_github, f: Factory):
    org, repo, spec, event, cls, impact = _issued_world(session, f)
    n1 = create_issue_for_impact(session, impact)
    n2 = create_issue_for_impact(session, impact)
    gh = fake_github.repo("acme/support-agent")
    assert n1 == n2 == 1 and len(gh.issues) == 1
    assert impact.status == "issued" and impact.github_issue_number == 1
    assert gh.labels["kintsua"].name == "kintsua" and "severity:breaking" in gh.labels

    # a crash after GitHub answered but before the commit: the marker prevents a duplicate
    impact.github_issue_number = None
    impact.status = "detected"
    session.commit()
    assert create_issue_for_impact(session, impact) == 1
    assert len(gh.issues) == 1 and impact.status == "issued"


def test_degraded_gets_its_own_label(session, fake_github, f: Factory):
    org, repo, spec, event, cls, impact = _issued_world(
        session, f, severity="degraded", category="deprecation"
    )
    create_issue_for_impact(session, impact)
    issue = fake_github.repo("acme/support-agent").issues[1]
    assert {lb.name for lb in issue.labels} == {"kintsua", "severity:degraded"}


def test_comment_noop_without_issue_then_comment_and_upsert(session, fake_github, f: Factory):
    org, repo, spec, event, cls, impact = _issued_world(session, f)
    comment_on_impact(session, impact, "hello")  # no issue yet: nothing happens
    assert fake_github.repos == {}
    create_issue_for_impact(session, impact)
    comment_on_impact(session, impact, "hello")
    issue = fake_github.repo("acme/support-agent").issues[1]
    assert [c.body for c in issue.comments] == ["hello"]
    assert upsert_impact_comment(session, impact, "<!-- m -->", "first") == "created"
    assert upsert_impact_comment(session, impact, "<!-- m -->", "second") == "edited"
    assert [c.body for c in issue.comments] == ["hello", "<!-- m -->\nsecond"]


def test_close_impact_issue_comments_closes_and_dismisses(session, fake_github, f: Factory):
    org, repo, spec, event, cls, impact = _issued_world(session, f)
    create_issue_for_impact(session, impact)
    close_impact_issue(session, impact, "call site removed in <abc123>")
    issue = fake_github.repo("acme/support-agent").issues[1]
    assert issue.state == "closed"
    assert issue.comments[-1].body == "Kintsua closed this: call site removed in &lt;abc123&gt;"
    assert impact.status == "dismissed" and impact.resolved_at is not None
    assert impact.fix_reason == "call site removed in <abc123>"

    # without an issue it only changes the row
    other = Impact(repo_id=repo.id, change_event_id=event.id, status="detected")
    close_impact_issue(session, other, "muted")
    assert other.status == "dismissed" and other.resolved_at is not None
