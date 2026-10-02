"""change event -> impact: matching across dialects, dedupe, mutes, policy, handlers."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from kintsua.core.db import new_session
from kintsua.core.models import Impact, Job
from kintsua.core.queue import NonRetryable, drain
from kintsua.matcher.match import (
    handle_match_event,
    handle_open_issue,
    issues_may_open,
    match_change_event,
    maybe_enqueue_fix,
    open_issues_for_pending,
)
from tests.matcher.conftest import STRIPE_SPEC, Factory


def test_breaking_openapi_event_matches_templated_path_and_creates_one_impact(
    session, fake_github, stripe_http_world
):
    w = stripe_http_world
    impacts = match_change_event(session, w["event"])
    assert len(impacts) == 1
    imp = impacts[0]
    assert imp.status == "detected"
    assert imp.repo_id == w["repo"].id and imp.change_event_id == w["event"].id
    assert imp.classification_id == w["cls"].id
    assert [s["call_site_id"] for s in imp.matched_call_sites] == [str(w["hit"].id)]
    site = imp.matched_call_sites[0]
    assert site["file_path"] == "billing/charge.py" and site["line"] == 42
    assert site["symbol"] == "httpx.Client.post" and site["operation"] == "POST /v1/charges/{id}"
    assert site["vendor"] == "stripe"

    # second run: same impact, nothing duplicated, no GitHub traffic (auto_issue is off)
    again = match_change_event(session, w["event"])
    assert [i.id for i in again] == [imp.id]
    assert len(session.scalars(select(Impact)).all()) == 1
    assert fake_github.repos == {}
    assert not [j for j in session.scalars(select(Job)) if j.kind == "open_issue"]


@pytest.mark.parametrize("severity", ["safe", "unknown"])
def test_safe_and_unknown_events_create_nothing(session, f: Factory, severity):
    org = f.org()
    repo = f.repo(org)
    spec = f.source()
    dep = f.dependency(repo, source=spec)
    f.call_site(dep, "POST /v1/charges")
    event = f.event(spec)
    f.classification(event, severity=severity, category="docs_only", summary="nothing")
    session.commit()
    assert match_change_event(session, event) == []
    assert f.impacts() == []


def test_missing_classification_enqueues_classify_event(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    spec = f.source()
    f.call_site(f.dependency(repo, source=spec), "POST /v1/charges")
    event = f.event(spec)
    session.commit()
    assert match_change_event(session, event) == []
    jobs = f.jobs("classify_event")
    assert len(jobs) == 1 and jobs[0].payload == {"change_event_id": str(event.id)}
    assert jobs[0].dedupe_key == f"classify_event:{event.id}"


def test_muted_pattern_skips_call_site(session, f: Factory):
    org = f.org()
    repo = f.repo(org, config={"mute": ["stripe:/v1/charges*"]})
    spec = f.source()
    dep = f.dependency(repo, source=spec, meta={"vendor": "stripe"})
    f.call_site(dep, "POST /v1/charges")
    event = f.event(spec)
    f.classification(event)
    session.commit()
    assert match_change_event(session, event) == []
    assert f.impacts() == []

    # a mute for another path keeps the match
    repo.config = {"mute": ["stripe:/v1/customers/*"]}
    session.commit()
    assert len(match_change_event(session, event)) == 1


def test_mcp_tool_event_matches_bare_tool_call_site_and_respects_tool_mute(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    weather = f.source(
        kind="mcp",
        locator="https://mcp.weather.example/mcp",
        display_name="Weather MCP",
        vendor=None,
    )
    dep = f.dependency(repo, source=weather, kind="mcp", name="https://mcp.weather.example/mcp")
    hit = f.call_site(dep, "get_weather", file_path="agent/tools.py", symbol="call_tool")
    f.call_site(dep, "get_forecast", file_path="agent/tools.py", symbol="call_tool")
    event = f.event(
        weather,
        [
            {
                "op": "removed",
                "path": "/tools/get_weather",
                "before": {"name": "get_weather"},
                "after": None,
            }
        ],
        from_version=None,
        to_version=None,
    )
    f.classification(
        event,
        category="removal",
        summary="Tool get_weather was removed",
        affected_paths=["tool:get_weather"],
    )
    session.commit()
    impacts = match_change_event(session, event)
    assert len(impacts) == 1
    assert [s["call_site_id"] for s in impacts[0].matched_call_sites] == [str(hit.id)]

    # same event for a second repo that mutes get_* tools: no impact
    repo2 = f.repo(org, full_name="acme/other", config={"mute": ["tool:get_*"]})
    dep2 = f.dependency(repo2, source=weather, kind="mcp", name="https://mcp.weather.example/mcp")
    f.call_site(dep2, "get_weather")
    session.commit()
    impacts = match_change_event(session, event)
    assert {i.repo_id for i in impacts} == {repo.id}


def test_package_major_bump_matches_every_call_site_of_the_dependency(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    pypi = f.source(kind="pypi", locator="stripe", display_name="stripe (PyPI)")
    dep = f.dependency(repo, source=pypi, kind="pypi", name="stripe", meta={"vendor": "stripe"})
    a = f.call_site(dep, "stripe.Charge.create", file_path="billing.py")
    b = f.call_site(dep, "stripe.Customer.retrieve", file_path="crm.py")
    # a second repo depends on the package but has no call site: still an impact on a major bump
    repo2 = f.repo(org, full_name="acme/declared-only")
    f.dependency(repo2, source=pypi, kind="pypi", name="stripe")
    event = f.event(
        pypi,
        [{"op": "added", "path": "/versions/12.0.0", "before": None, "after": {}}],
        from_version="11.6.0",
        to_version="12.0.0",
    )
    f.classification(
        event,
        severity="degraded",
        category="deprecation",
        summary="stripe 11.x -> 12.0.0: major version bump",
        affected_paths=["package:stripe"],
        rule_hits=["major_bump"],
    )
    session.commit()
    impacts = {i.repo_id: i for i in match_change_event(session, event)}
    assert set(impacts) == {repo.id, repo2.id}
    assert {s["call_site_id"] for s in impacts[repo.id].matched_call_sites} == {
        str(a.id),
        str(b.id),
    }
    assert impacts[repo2.id].matched_call_sites == []


def test_package_event_without_major_bump_needs_call_sites(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    pypi = f.source(kind="pypi", locator="stripe", display_name="stripe (PyPI)")
    f.dependency(repo, source=pypi, kind="pypi", name="stripe")  # no call sites
    event = f.event(pypi, [], from_version="12.0.0", to_version="12.1.0")
    f.classification(
        event,
        severity="degraded",
        category="behaviour",
        summary="notes say x",
        affected_paths=[],
        rule_hits=["llm"],
    )
    session.commit()
    assert match_change_event(session, event) == []


def test_sdk_symbol_matches_openapi_event_through_openapi_source_id(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    spec = f.source()
    pypi = f.source(kind="pypi", locator="stripe", display_name="stripe (PyPI)")
    dep = f.dependency(
        repo,
        source=pypi,
        kind="pypi",
        name="stripe",
        meta={"vendor": "stripe", "openapi_source_id": str(spec.id)},
    )
    hit = f.call_site(dep, "stripe.Charge.create", file_path="billing.py", line=3)
    f.call_site(dep, "stripe.Customer.retrieve", file_path="crm.py", line=9)
    event = f.event(spec)
    f.classification(event, affected_paths=["POST /v1/charges"])
    session.commit()
    impacts = match_change_event(session, event)
    assert len(impacts) == 1
    assert [s["call_site_id"] for s in impacts[0].matched_call_sites] == [str(hit.id)]
    assert impacts[0].matched_call_sites[0]["operation"] == "stripe.Charge.create"


def test_slack_sdk_symbol_matches_slack_openapi_change(session, f: Factory):
    org = f.org()
    repo = f.repo(org)
    spec = f.source(
        locator="https://api.slack.com/specs/openapi/v2/slack_web.json",
        display_name="Slack API",
        vendor="slack",
    )
    dep = f.dependency(
        repo,
        kind="pypi",
        name="slack-sdk",
        meta={"vendor": "slack", "openapi_source_id": str(spec.id)},
    )
    hit = f.call_site(dep, "slack_sdk.WebClient.chat_postMessage", file_path="notify.py")
    f.call_site(dep, "slack_sdk.WebClient.conversations_list", file_path="notify.py")
    event = f.event(
        spec,
        [
            {
                "op": "required_added",
                "path": "/paths/~1chat.postMessage/post/requestBody/content/application~1x-www-form-urlencoded/schema/required",
                "before": ["channel"],
                "after": ["channel", "text"],
            }
        ],
        from_version=None,
        to_version=None,
    )
    f.classification(
        event,
        summary="chat.postMessage now requires text",
        affected_paths=["POST /chat.postMessage"],
    )
    session.commit()
    impacts = match_change_event(session, event)
    assert len(impacts) == 1
    assert [s["call_site_id"] for s in impacts[0].matched_call_sites] == [str(hit.id)]


def test_inactive_repos_and_orgs_are_ignored(session, f: Factory):
    org = f.org()
    spec = f.source()
    inactive = f.repo(org, full_name="acme/old", active=False)
    f.call_site(f.dependency(inactive, source=spec), "POST /v1/charges")
    dead_org = f.org(name="dead")
    dead_org.active = False
    r2 = f.repo(dead_org, full_name="dead/x")
    f.call_site(f.dependency(r2, source=spec), "POST /v1/charges")
    event = f.event(spec)
    f.classification(event)
    session.commit()
    assert match_change_event(session, event) == []


# --------------------------------------------------------------------------------------
# policy: auto_issue and fix mode
# --------------------------------------------------------------------------------------


def test_auto_issue_off_leaves_detected_and_makes_no_github_call(
    session, fake_github, stripe_http_world
):
    w = stripe_http_world
    assert not issues_may_open(w["org"])
    match_change_event(session, w["event"])
    imp = session.scalar(select(Impact))
    assert imp.status == "detected" and imp.github_issue_number is None
    assert [j.kind for j in session.scalars(select(Job))] == []
    assert fake_github.repos == {}


def test_auto_issue_on_opens_issue_with_labels_and_enqueues_fix_for_fixable_category(
    session, fake_github, stripe_http_world
):
    w = stripe_http_world
    w["org"].settings = {"auto_issue": True, "fix_mode": "pr"}
    session.commit()
    assert issues_may_open(w["org"])
    (imp,) = match_change_event(session, w["event"])
    jobs = session.scalars(select(Job).where(Job.kind == "open_issue")).all()
    assert len(jobs) == 1 and jobs[0].dedupe_key == f"open_issue:{imp.id}"
    assert jobs[0].payload == {"impact_id": str(imp.id)}

    assert drain(new_session, ["open_issue"]) == 1
    session.expire_all()
    imp = session.get(Impact, imp.id)
    assert imp.status == "issued" and imp.github_issue_number == 1
    gh = fake_github.repo("acme/support-agent")
    issue = gh.issues[1]
    assert issue.title.startswith("[Kintsua] POST /v1/charges now requires currency")
    assert {lb.name for lb in issue.labels} == {"kintsua", "severity:breaking"}
    assert set(gh.labels) >= {"kintsua", "severity:breaking"}
    for heading in (
        "## What changed upstream",
        "## Where this repo calls it",
        "## Suggested migration",
        "## Status",
        "Kintsua will open a PR within an hour",
        f"<!-- kintsua:change_event:{w['event'].id} -->",
        "billing/charge.py:42",
        "https://github.com/acme/support-agent/blob/main/billing/charge.py#L42",
        "required\\_added",
        "mute: add `stripe:POST /v1/charges/{id}`",
    ):
        assert heading in issue.body, heading
    assert "crm/sync.py" not in issue.body  # the unmatched call site is not listed
    fix_jobs = session.scalars(select(Job).where(Job.kind == "fix_impact")).all()
    assert len(fix_jobs) == 1 and fix_jobs[0].dedupe_key == f"fix_impact:{imp.id}"
    assert fix_jobs[0].payload == {"impact_id": str(imp.id)}

    # the open_issue job is idempotent: a retry neither opens a second issue nor a second fix job
    handle_open_issue(session, {"impact_id": str(imp.id)})
    assert len(gh.issues) == 1
    assert len(session.scalars(select(Job).where(Job.kind == "fix_impact")).all()) == 1


def test_settings_auto_issue_opens_issues_for_every_org(
    session, settings, fake_github, stripe_http_world, monkeypatch
):
    w = stripe_http_world
    monkeypatch.setattr(settings, "auto_issue", True)
    assert issues_may_open(w["org"])
    match_change_event(session, w["event"])
    assert len(session.scalars(select(Job).where(Job.kind == "open_issue")).all()) == 1


@pytest.mark.parametrize(
    ("category", "fix_mode", "expect_fix"),
    [
        ("removal", "pr", False),
        ("required_param", "issue_only", False),
        ("rename", "pr", True),
        ("deprecation", "pr", True),
    ],
)
def test_fix_impact_only_for_fixable_categories_under_pr_mode(
    session, fake_github, f: Factory, category, fix_mode, expect_fix
):
    org = f.org(auto_issue=True, fix_mode=fix_mode)
    repo = f.repo(org)
    spec = f.source()
    f.call_site(f.dependency(repo, source=spec), "POST /v1/charges")
    event = f.event(spec)
    f.classification(event, category=category, affected_paths=["POST /v1/charges"])
    session.commit()
    (imp,) = match_change_event(session, event)
    out = handle_open_issue(session, {"impact_id": str(imp.id)})
    assert out["issue_number"] == 1 and out["fix_queued"] is expect_fix
    assert bool(session.scalars(select(Job).where(Job.kind == "fix_impact")).all()) is expect_fix
    body = fake_github.repo("acme/support-agent").issues[1].body
    if fix_mode == "pr" and expect_fix:
        assert "Kintsua will open a PR within an hour" in body
    elif fix_mode == "pr":
        assert "not auto-fixed in v0" in body
    else:
        assert "Fix mode is issue_only" in body


def test_repo_fix_mode_overrides_org_default(session, fake_github, f: Factory):
    org = f.org(auto_issue=True, fix_mode="issue_only")
    repo = f.repo(org, config={"fix": {"mode": "pr"}})
    spec = f.source()
    f.call_site(f.dependency(repo, source=spec), "POST /v1/charges")
    event = f.event(spec)
    f.classification(event)
    session.commit()
    (imp,) = match_change_event(session, event)
    handle_open_issue(session, {"impact_id": str(imp.id)})
    assert maybe_enqueue_fix(session, imp) is True  # deduped: still exactly one job
    assert len(session.scalars(select(Job).where(Job.kind == "fix_impact")).all()) == 1


def test_open_issues_for_pending_flips_drafts(session, fake_github, stripe_http_world):
    w = stripe_http_world
    (imp,) = match_change_event(session, w["event"])
    assert imp.status == "detected"
    flipped = open_issues_for_pending(session, w["repo"].id)
    assert [i.id for i in flipped] == [imp.id]
    session.refresh(imp)
    assert imp.status == "issued" and imp.github_issue_number == 1
    assert open_issues_for_pending(session) == []  # nothing left


# --------------------------------------------------------------------------------------
# handlers through the queue
# --------------------------------------------------------------------------------------


def test_match_event_handler_through_the_queue(session, fake_github, stripe_http_world):
    from kintsua.core.queue import enqueue

    w = stripe_http_world
    enqueue(
        session,
        "match_event",
        {"change_event_id": str(w["event"].id)},
        dedupe_key=f"match_event:{w['event'].id}",
    )
    session.commit()
    assert drain(new_session, ["match_event"]) == 1
    s2 = new_session()
    try:
        imps = s2.scalars(select(Impact)).all()
        assert len(imps) == 1 and imps[0].status == "detected"
        job = s2.scalar(select(Job).where(Job.kind == "match_event"))
        assert job.status == "done"
    finally:
        s2.close()


def test_handlers_reject_bad_payloads(session):
    with pytest.raises(NonRetryable):
        handle_match_event(session, {})
    with pytest.raises(NonRetryable):
        handle_match_event(session, {"change_event_id": "nope"})
    with pytest.raises(NonRetryable):
        handle_match_event(session, {"change_event_id": str(uuid.uuid4())})
    with pytest.raises(NonRetryable):
        handle_open_issue(session, {"impact_id": str(uuid.uuid4())})


def test_open_issue_skips_dismissed_impacts(session, fake_github, stripe_http_world):
    w = stripe_http_world
    (imp,) = match_change_event(session, w["event"])
    imp.status = "dismissed"
    session.commit()
    out = handle_open_issue(session, {"impact_id": str(imp.id)})
    assert out["skipped"] == "dismissed"
    assert fake_github.repos == {}


def test_spec_source_locator_is_the_curated_one():
    assert STRIPE_SPEC.endswith("spec3.json")


# --------------------------------------------------------------------------------------
# review regressions
# --------------------------------------------------------------------------------------


def _mcp_world(f: Factory, *, config=None, full_name="acme/support-agent"):
    org = f.org()
    repo = f.repo(org, full_name=full_name, config=config)
    browser = f.source(
        kind="mcp", locator="https://mcp.browser.example/mcp", display_name="Browser", vendor=None
    )
    dep = f.dependency(repo, source=browser, kind="mcp", name="https://mcp.browser.example/mcp")
    site = f.call_site(dep, "browser.navigate", file_path="agent/browse.py", symbol="call_tool")
    return org, repo, browser, dep, site


def test_dotted_mcp_tool_name_matches_its_tool_path_and_tool_mutes(session, f: Factory):
    _org, repo, browser, _dep, site = _mcp_world(f)
    event = f.event(
        browser,
        [{"op": "removed", "path": "/tools/browser.navigate", "before": {}, "after": None}],
        from_version=None,
        to_version=None,
    )
    f.classification(
        event,
        category="removal",
        summary="Tool browser.navigate was removed",
        affected_paths=["tool:browser.navigate"],
    )
    session.commit()
    (imp,) = match_change_event(session, event)
    assert [s["call_site_id"] for s in imp.matched_call_sites] == [str(site.id)]
    assert imp.matched_call_sites[0]["dependency_kind"] == "mcp"

    # a repo muting tool:browser.* gets nothing
    muted = f.repo(imp.repo.org, full_name="acme/muted", config={"mute": ["tool:browser.*"]})
    dep2 = f.dependency(muted, source=browser, kind="mcp", name="https://mcp.browser.example/mcp")
    f.call_site(dep2, "browser.navigate")
    session.commit()
    assert {i.repo_id for i in match_change_event(session, event)} == {repo.id}


def _major_bump_world(f: Factory, *, config=None, with_site=False, **org_settings):
    org = f.org(**org_settings)
    repo = f.repo(org, config=config)
    pypi = f.source(kind="pypi", locator="stripe", display_name="stripe (PyPI)")
    dep = f.dependency(repo, source=pypi, kind="pypi", name="stripe", meta={"vendor": "stripe"})
    if with_site:
        f.call_site(dep, "stripe.Charge.create", file_path="billing.py")
    event = f.event(
        pypi,
        [{"op": "added", "path": "/versions/12.0.0", "before": None, "after": {}}],
        from_version="11.6.0",
        to_version="12.0.0",
    )
    f.classification(
        event,
        severity="degraded",
        category="deprecation",
        summary="stripe 11.x -> 12.0.0: major version bump",
        affected_paths=["package:stripe"],
        rule_hits=["major_bump"],
    )
    return org, repo, event


@pytest.mark.parametrize("mute", ["stripe", "package:stripe", "stripe:*"])
@pytest.mark.parametrize("with_site", [False, True])
def test_muted_package_major_bump_creates_no_impact(session, f: Factory, mute, with_site):
    _org, _repo, event = _major_bump_world(
        f, config={"mute": [mute]}, with_site=with_site, auto_issue=True
    )
    session.commit()
    assert match_change_event(session, event) == []
    assert f.impacts() == [] and f.jobs("open_issue") == []


def test_unmuted_major_bump_still_creates_the_no_call_site_impact(session, f: Factory):
    _org, _repo, event = _major_bump_world(f, config={"mute": ["package:slack-sdk"]})
    session.commit()
    (imp,) = match_change_event(session, event)
    assert imp.matched_call_sites == []


def test_no_fix_job_for_an_impact_without_a_file_located_call_site(session, fake_github, f):
    _org, _repo, event = _major_bump_world(f, auto_issue=True, fix_mode="pr")
    session.commit()
    (imp,) = match_change_event(session, event)
    assert drain(new_session, ["open_issue"]) == 1
    assert f.jobs("fix_impact") == []
    body = fake_github.repo("acme/support-agent").issues[1].body
    assert "will open a PR" not in body
    assert "no call sites were found in this repo's files" in body

    # a runtime-only call site is not a file the fixer can patch either
    session.expire_all()
    imp = session.get(Impact, imp.id)
    imp.matched_call_sites = [{"file_path": "<runtime>", "line": 0, "operation": "x"}]
    session.commit()
    assert maybe_enqueue_fix(session, imp) is False
    imp.matched_call_sites = [{"file_path": "billing.py", "line": 3, "operation": "x"}]
    session.commit()
    assert maybe_enqueue_fix(session, imp) is True


def test_open_issue_jobs_survive_a_long_github_outage(session, stripe_http_world):
    from kintsua.core.queue import fail

    w = stripe_http_world
    w["org"].settings = {"auto_issue": True}
    session.commit()
    (imp,) = match_change_event(session, w["event"])
    (job,) = session.scalars(select(Job).where(Job.kind == "open_issue")).all()
    assert job.max_attempts >= 20
    for _ in range(5):  # five failed attempts used to be the end of it
        job.attempts += 1
        fail(session, job, "GitHub 502")
    session.refresh(job)
    assert job.status == "queued"


def test_requeue_stalled_issues_requeues_exhausted_jobs_once(session, stripe_http_world):
    from datetime import timedelta

    from kintsua.core.queue import fail
    from kintsua.core.util import utcnow
    from kintsua.matcher.match import requeue_stalled_issues

    w = stripe_http_world
    w["org"].settings = {"auto_issue": True}
    session.commit()
    (imp,) = match_change_event(session, w["event"])
    (job,) = session.scalars(select(Job).where(Job.kind == "open_issue")).all()
    job.attempts = job.max_attempts
    fail(session, job, "GitHub 502")
    assert job.status == "failed"

    later = utcnow() + timedelta(hours=2)
    assert requeue_stalled_issues(session, now=utcnow()) == []  # still in the cooldown
    assert [i.id for i in requeue_stalled_issues(session, now=later)] == [imp.id]
    assert requeue_stalled_issues(session, now=later) == []  # a queued job exists now
    queued = session.scalars(
        select(Job).where(Job.kind == "open_issue", Job.status == "queued")
    ).all()
    assert len(queued) == 1 and queued[0].max_attempts >= 20

    # a permanent failure (NonRetryable: attempts left) and a never-queued draft stay put
    queued[0].attempts = 1
    fail(session, queued[0], "repo gone", retry=False)
    assert requeue_stalled_issues(session, now=later + timedelta(hours=2)) == []
    w["org"].settings = {}
    session.commit()
    job.status = "failed"
    session.commit()
    assert requeue_stalled_issues(session, now=later + timedelta(hours=2)) == []
