"""GitHub webhook handling: signature checks, install flow, push debounce, PR / issue / CI
events, idempotent redelivery."""

from __future__ import annotations

import copy
import json
from datetime import timedelta

from sqlalchemy import func, select

from kintsua.core.models import Dependency, Impact, Org, Repo, Source
from kintsua.core.util import utcnow
from tests.api.conftest import (
    INSTALLATION_ID,
    REPO_FULL_NAME,
    REPO_GITHUB_ID,
    STRIPE_SPEC,
    bearer,
    load_payload,
    post_webhook,
)

SENDER = {"login": "octocat", "id": 1, "type": "User"}
ACCOUNT = {"login": "acme", "id": 583231, "type": "Organization"}


def test_rejects_bad_or_missing_signature(client):
    payload = load_payload("installation_created")
    r = post_webhook(client, "installation", payload, signature="sha256=" + "0" * 64)
    assert r.status_code == 401 and r.json()["error"]
    r = post_webhook(client, "installation", payload, secret="not-the-secret")
    assert r.status_code == 401
    body = json.dumps(payload).encode()
    r = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "installation", "Content-Type": "application/json"},
    )
    assert r.status_code == 401
    assert client.post("/webhooks/github", content=body).status_code == 401


def test_503_when_no_secret_configured(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", None)
    r = post_webhook(client, "ping", {"zen": "Keep it logically awesome"}, secret="whatever")
    assert r.status_code == 503
    assert "GITHUB_WEBHOOK_SECRET" in r.json()["error"]


def test_ping_and_unknown_events(client):
    r = post_webhook(client, "ping", {"zen": "Keep it logically awesome", "hook_id": 1})
    assert r.status_code == 200 and r.json() == {
        "ok": True,
        "handled": "ping",
        "zen": "Keep it logically awesome",
    }
    r = post_webhook(client, "star", {"action": "created"})
    assert r.status_code == 202 and r.json() == {"ok": True, "ignored": "star"}
    r = post_webhook(
        client, "installation", {"action": "new_permissions_accepted", "installation": {"id": 1}}
    )
    assert r.status_code == 202 and r.json()["action"] == "new_permissions_accepted"
    r = client.post(
        "/webhooks/github",
        content=b"not json",
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": _sig(b"not json")},
    )
    assert r.status_code == 400


def _sig(body: bytes) -> str:
    from tests.api.conftest import sign

    return sign(body)


def test_installation_created_creates_org_repos_and_index_jobs(client, session, factory):
    payload = load_payload("installation_created")
    r = post_webhook(client, "installation", payload, delivery="delivery-1")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["handled"] == "installation" and data["action"] == "created" and data["repos"] == 2

    org = session.scalar(select(Org).where(Org.github_installation_id == INSTALLATION_ID))
    assert org is not None
    assert org.name == "acme" and org.account_type == "Organization" and org.plan == "free"
    assert org.active is True
    assert org.settings == {
        "fix_mode": "issue_only",
        "auto_issue": False,
        "github_account_id": 583231,
    }
    repos = {r.full_name: r for r in session.scalars(select(Repo).where(Repo.org_id == org.id))}
    assert set(repos) == {"acme/support-agent", "acme/billing-bot"}
    assert repos["acme/support-agent"].private is True
    assert repos["acme/billing-bot"].private is False
    assert repos["acme/support-agent"].github_repo_id == REPO_GITHUB_ID
    assert repos["acme/support-agent"].default_branch == "main"
    assert all(r.active for r in repos.values())

    jobs = factory.jobs("index_repo")
    assert len(jobs) == 2
    assert {j.payload["reason"] for j in jobs} == {"install"}
    assert {j.payload["repo_id"] for j in jobs} == {str(r.id) for r in repos.values()}
    assert {j.dedupe_key for j in jobs} == {f"index_repo:{r.id}" for r in repos.values()}

    # the same delivery again is acknowledged without touching anything
    r = post_webhook(client, "installation", payload, delivery="delivery-1")
    assert r.status_code == 200 and r.json()["duplicate"] is True
    # a replay under a new delivery id is idempotent on rows and jobs
    r = post_webhook(client, "installation", payload, delivery="delivery-2")
    assert r.status_code == 200 and r.json()["repos"] == 2
    session.expire_all()
    assert session.scalar(select(func.count(Repo.id))) == 2
    assert session.scalar(select(func.count(Org.id))) == 1
    assert len(factory.jobs("index_repo")) == 2


def test_installation_deleted_deactivates_org_repos_and_orphan_sources(client, session, factory):
    org = factory.org()
    repo = factory.repo(org)
    other_org = factory.org("other", installation_id=999)
    other_repo = factory.repo(other_org, "other/app")
    orphan = factory.source(
        kind="changelog", locator="https://vendor.example/changelog", vendor="v"
    )
    shared = factory.source(
        kind="openapi", locator="https://shared.example/openapi.json", vendor="s"
    )
    seeded = factory.source(kind="openapi", locator=STRIPE_SPEC)  # in kintsua/seed/sources.yml
    factory.dependency(repo, orphan, kind="changelog", name=orphan.locator)
    factory.dependency(repo, shared, kind="openapi", name=shared.locator)
    factory.dependency(repo, seeded, kind="pypi", name="stripe")
    factory.dependency(other_repo, shared, kind="openapi", name=shared.locator)

    payload = {
        "action": "deleted",
        "installation": {"id": INSTALLATION_ID, "account": ACCOUNT},
        "repositories": [{"id": REPO_GITHUB_ID, "full_name": REPO_FULL_NAME}],
        "sender": SENDER,
    }
    r = post_webhook(client, "installation", payload)
    assert r.status_code == 200, r.text
    assert r.json()["repos"] == 1 and r.json()["sources_deactivated"] == 1
    session.expire_all()
    assert session.get(Org, org.id).active is False
    assert session.get(Repo, repo.id).active is False
    assert session.get(Source, orphan.id).active is False
    assert session.get(Source, shared.id).active is True  # the other org still depends on it
    assert session.get(Source, seeded.id).active is True  # seeded sources keep running
    assert session.get(Org, other_org.id).active is True
    assert session.get(Repo, other_repo.id).active is True

    # suspend / unsuspend toggle the org
    r = post_webhook(client, "installation", {**payload, "action": "suspend"})
    assert r.status_code == 200 and r.json()["active"] is False
    r = post_webhook(client, "installation", {**payload, "action": "unsuspend"})
    assert r.status_code == 200 and r.json()["active"] is True
    session.expire_all()
    assert session.get(Org, org.id).active is True
    # a fresh install reactivates the repos
    created = load_payload("installation_created")
    r = post_webhook(client, "installation", created)
    assert r.status_code == 200
    session.expire_all()
    assert session.get(Repo, repo.id).active is True


def test_installation_repositories_added_and_removed(client, session, factory):
    org = factory.org()
    factory.repo(org)
    base = {
        "installation": {"id": INSTALLATION_ID, "account": ACCOUNT},
        "repository_selection": "selected",
        "sender": SENDER,
    }
    added = {
        **base,
        "action": "added",
        "repositories_added": [
            {"id": 186853009, "full_name": "acme/new-agent", "private": False},
        ],
        "repositories_removed": [],
    }
    r = post_webhook(client, "installation_repositories", added)
    assert r.status_code == 200 and r.json()["added"] == 1
    new = session.scalar(select(Repo).where(Repo.github_repo_id == 186853009))
    assert new is not None and new.full_name == "acme/new-agent" and new.org_id == org.id
    jobs = factory.jobs("index_repo")
    assert [j.payload for j in jobs] == [{"repo_id": str(new.id), "reason": "install"}]

    removed = {
        **base,
        "action": "removed",
        "repositories_added": [],
        "repositories_removed": [
            {"id": 186853009, "full_name": "acme/new-agent", "private": False}
        ],
    }
    r = post_webhook(client, "installation_repositories", removed)
    assert r.status_code == 200 and r.json()["removed"] == 1
    session.expire_all()
    assert session.get(Repo, new.id).active is False


def test_removing_one_repo_keeps_sources_its_siblings_still_use(client, session, factory):
    """api-correctness-1: removing repo B keeps polling a source repo A still depends on."""
    org = factory.org()
    repo_a = factory.repo(org)
    repo_b = factory.repo(org, "acme/other", github_repo_id=186853010)
    shared = factory.source(
        kind="openapi", locator="https://api.vendor.example/openapi.json", vendor="v"
    )
    only_b = factory.source(kind="changelog", locator="https://vendor.example/changelog")
    factory.dependency(repo_a, shared, kind="openapi", name=shared.locator)
    factory.dependency(repo_b, shared, kind="openapi", name=shared.locator)
    factory.dependency(repo_b, only_b, kind="changelog", name=only_b.locator)
    removed = {
        "action": "removed",
        "installation": {"id": INSTALLATION_ID, "account": ACCOUNT},
        "repositories_added": [],
        "repositories_removed": [{"id": 186853010, "full_name": "acme/other"}],
        "sender": SENDER,
    }
    r = post_webhook(client, "installation_repositories", removed)
    assert r.status_code == 200 and r.json()["sources_deactivated"] == 1, r.text
    session.expire_all()
    assert session.get(Repo, repo_b.id).active is False
    assert session.get(Source, shared.id).active is True  # repo A still depends on it
    assert session.get(Source, only_b.id).active is False

    # adding the repo back polls its source again
    added = {
        **removed,
        "action": "added",
        "repositories_added": [{"id": 186853010, "full_name": "acme/other"}],
        "repositories_removed": [],
    }
    r = post_webhook(client, "installation_repositories", added)
    assert r.status_code == 200 and r.json()["sources_reactivated"] == 1, r.text
    session.expire_all()
    assert session.get(Source, only_b.id).active is True
    assert session.get(Repo, repo_a.id).active is True


def test_reinstall_reuses_the_org_of_the_same_account(client, session, factory):
    """api-correctness-3: a reinstall (new installation id) keeps the org, its plan, SDK key
    and sources instead of creating a second, empty org."""
    created = load_payload("installation_created")
    assert post_webhook(client, "installation", created).status_code == 200
    org = session.scalar(select(Org).where(Org.github_installation_id == INSTALLATION_ID))
    org.plan = "pro"
    org.settings = {**org.settings, "stripe_subscription_id": "sub_123"}
    session.commit()
    key = factory.sdk_key(org)
    repo = session.scalar(select(Repo).where(Repo.github_repo_id == REPO_GITHUB_ID))
    custom = factory.source(kind="openapi", locator="https://api.vendor.example/openapi.json")
    stdio = factory.source(kind="mcp", locator="python tools/server.py", vendor=None)
    factory.dependency(repo, custom, kind="openapi", name=custom.locator)
    factory.dependency(repo, stdio, kind="mcp", name=stdio.locator)
    stdio.active = False  # the worker refuses customer stdio commands
    session.commit()

    deleted = {**created, "action": "deleted"}
    r = post_webhook(client, "installation", deleted)
    assert r.status_code == 200 and r.json()["sources_deactivated"] == 1
    session.expire_all()
    assert session.get(Source, custom.id).active is False

    reinstalled = copy.deepcopy(created)
    reinstalled["installation"]["id"] = INSTALLATION_ID + 1
    r = post_webhook(client, "installation", reinstalled)
    assert r.status_code == 200, r.text
    assert r.json()["org_id"] == str(org.id) and r.json()["sources_reactivated"] == 1
    session.expire_all()
    assert session.scalar(select(func.count(Org.id))) == 1
    org = session.get(Org, org.id)
    assert org.github_installation_id == INSTALLATION_ID + 1 and org.active is True
    assert org.plan == "pro" and org.settings["stripe_subscription_id"] == "sub_123"
    assert session.get(Repo, repo.id).org_id == org.id and session.get(Repo, repo.id).active
    assert session.get(Source, custom.id).active is True
    assert session.get(Source, stdio.id).active is False  # never revived: not runnable
    # the customer's existing SDK key keeps working
    r = client.post(
        "/v1/ingest",
        json={"repo": REPO_FULL_NAME, "events": []},
        headers=bearer(key),
    )
    assert r.status_code == 200, r.text

    # a different account installing gets its own org
    stranger = copy.deepcopy(created)
    stranger["installation"]["id"] = 99
    stranger["installation"]["account"] = {"login": "acme", "id": 1, "type": "Organization"}
    stranger["repositories"] = []
    r = post_webhook(client, "installation", stranger)
    assert r.status_code == 200 and r.json()["org_id"] != str(org.id)


def test_push_to_default_branch_debounces_and_reschedules(client, session, factory, settings):
    org = factory.org()
    repo = factory.repo(org)
    payload = load_payload("push")
    t0 = utcnow()

    r = post_webhook(client, "push", payload)
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "push" and r.json()["debounce_s"] == settings.push_debounce_s
    jobs = factory.jobs("index_repo")
    assert len(jobs) == 1
    first = jobs[0]
    assert first.payload == {"repo_id": str(repo.id), "reason": "push"}
    assert first.dedupe_key == f"index_repo:{repo.id}"
    assert first.run_after >= t0 + timedelta(seconds=settings.push_debounce_s - 5)
    run_after_1 = first.run_after

    # a second push moves the queued job later instead of adding one
    r = post_webhook(client, "push", payload)
    assert r.status_code == 200
    jobs = factory.jobs("index_repo")
    assert len(jobs) == 1 and jobs[0].id == first.id
    assert jobs[0].run_after >= run_after_1

    # .kintsua.yml in the changed files marks the reason "config"
    jobs[0].status = "done"
    session.commit()
    cfg = copy.deepcopy(payload)
    cfg["head_commit"]["modified"].append(".kintsua.yml")
    r = post_webhook(client, "push", cfg)
    assert r.status_code == 200 and r.json()["reason"] == "config"
    jobs = factory.jobs("index_repo")
    assert len(jobs) == 1 and jobs[0].payload["reason"] == "config"

    # a push to another branch does nothing
    other = copy.deepcopy(payload)
    other["ref"] = "refs/heads/feature/new-thing"
    r = post_webhook(client, "push", other)
    assert r.status_code == 200 and r.json()["skipped"] == "not the default branch"
    assert len(factory.jobs("index_repo")) == 1

    # a branch deletion does nothing
    gone = copy.deepcopy(payload)
    gone["deleted"] = True
    assert post_webhook(client, "push", gone).json()["skipped"] == "branch deleted"

    # a repo that is not installed is skipped
    stranger = copy.deepcopy(payload)
    stranger["repository"] = {"id": 1, "full_name": "someone/else", "default_branch": "main"}
    r = post_webhook(client, "push", stranger)
    assert r.status_code == 200 and r.json()["skipped"] == "repo not installed"

    # the payload's default_branch corrects a stale row
    renamed = copy.deepcopy(payload)
    renamed["ref"] = "refs/heads/trunk"
    renamed["repository"]["default_branch"] = "trunk"
    r = post_webhook(client, "push", renamed)
    assert r.status_code == 200 and r.json()["reason"] == "push"
    session.expire_all()
    assert session.get(Repo, repo.id).default_branch == "trunk"


def test_renamed_repo_follows_its_new_name(client, session, factory):
    """api-correctness-8: GitHub renames reach the repo row (push payloads and the
    repository event), so ingest and the settings page work under the new name."""
    org = factory.org()
    repo = factory.repo(org)
    key = factory.sdk_key(org)
    payload = load_payload("push")
    payload["repository"]["full_name"] = "acme/support-bot"
    payload["repository"]["name"] = "support-bot"
    r = post_webhook(client, "push", payload)
    assert r.status_code == 200, r.text
    session.expire_all()
    assert session.get(Repo, repo.id).full_name == "acme/support-bot"
    r = client.post(
        "/v1/ingest", json={"repo": "acme/support-bot", "events": []}, headers=bearer(key)
    )
    assert r.status_code == 200, r.text

    event = {
        "action": "renamed",
        "changes": {"repository": {"name": {"from": "support-bot"}}},
        "repository": {"id": REPO_GITHUB_ID, "full_name": "acme/agent", "private": False},
        "installation": {"id": INSTALLATION_ID},
        "sender": SENDER,
    }
    r = post_webhook(client, "repository", event)
    assert r.status_code == 200 and r.json()["changed"] is True, r.text
    session.expire_all()
    row = session.get(Repo, repo.id)
    assert row.full_name == "acme/agent" and row.private is False
    stranger = {**event, "repository": {"id": 1, "full_name": "x/y"}}
    assert post_webhook(client, "repository", stranger).json()["skipped"] == "repo not installed"
    assert post_webhook(client, "repository", {**event, "action": "starred"}).status_code == 202


def test_pull_request_closed_enqueues_pr_outcome_only_for_kintsua_branches(client, factory):
    org = factory.org()
    repo = factory.repo(org)
    payload = load_payload("pull_request_closed")

    r = post_webhook(client, "pull_request", payload)
    assert r.status_code == 200, r.text
    assert r.json()["pr_number"] == 42 and r.json()["merged"] is True
    jobs = factory.jobs("pr_outcome")
    assert len(jobs) == 1
    assert jobs[0].payload == {
        "repo_id": str(repo.id),
        "pr_number": 42,
        "action": "closed",
        "merged": True,
        "merge_commit_sha": "9b1d6c2f4e8a0c3b5d7f9a1c3e5b7d9f1a3c5e7b",
        "head_sha": "4f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d",
    }
    # a replay collapses on the dedupe key
    post_webhook(client, "pull_request", payload)
    assert len(factory.jobs("pr_outcome")) == 1

    # a human's PR on another branch is not ours
    other = copy.deepcopy(payload)
    other["number"] = 43
    other["pull_request"]["number"] = 43
    other["pull_request"]["head"]["ref"] = "feature/new-thing"
    r = post_webhook(client, "pull_request", other)
    assert r.status_code == 200 and r.json()["skipped"] == "not a kintsua branch"
    assert len(factory.jobs("pr_outcome")) == 1

    # other actions are ignored
    opened = copy.deepcopy(payload)
    opened["action"] = "opened"
    assert post_webhook(client, "pull_request", opened).status_code == 202


def test_issues_closed_by_a_human_dismisses_the_impact(client, session, factory):
    org = factory.org()
    repo = factory.repo(org)
    impact, event, _ = factory.breaking_impact(repo, status="issued", github_issue_number=7)
    payload = load_payload("issues_closed")

    r = post_webhook(client, "issues", payload)
    assert r.status_code == 200, r.text
    assert r.json()["impact_id"] == str(impact.id) and r.json()["status"] == "dismissed"
    session.expire_all()
    impact = session.get(Impact, impact.id)
    assert impact.status == "dismissed" and impact.resolved_at is not None
    assert "octocat" in (impact.fix_reason or "")

    # reopening restores it
    reopened = {**payload, "action": "reopened"}
    r = post_webhook(client, "issues", reopened)
    assert r.status_code == 200 and r.json()["status"] == "issued"
    session.expire_all()
    impact = session.get(Impact, impact.id)
    assert impact.status == "issued" and impact.resolved_at is None

    # closed by a bot (Kintsua itself closing a dismissed issue) is not a human decision
    by_bot = copy.deepcopy(payload)
    by_bot["sender"] = {"login": "kintsua[bot]", "id": 2, "type": "Bot"}
    r = post_webhook(client, "issues", by_bot)
    assert r.status_code == 200 and r.json()["skipped"] == "closed by a bot"
    session.expire_all()
    assert session.get(Impact, impact.id).status == "issued"

    # an issue Kintsua did not open is skipped
    stray = copy.deepcopy(payload)
    stray["issue"]["number"] = 99
    stray["issue"]["body"] = "just a bug report"
    stray["issue"]["labels"] = []
    r = post_webhook(client, "issues", stray)
    assert r.status_code == 200 and r.json()["skipped"] == "not a kintsua issue"

    # the body marker maps an issue back to its change event when the number is unknown
    other, other_event, _ = factory.breaking_impact(
        repo, status="issued", source=factory.source(kind="pypi", locator="openai", vendor="openai")
    )
    marked = copy.deepcopy(payload)
    marked["issue"]["number"] = 55
    marked["issue"]["body"] = f"<!-- kintsua:change_event:{other_event.id} -->\nsomething"
    r = post_webhook(client, "issues", marked)
    assert r.status_code == 200 and r.json()["impact_id"] == str(other.id)
    session.expire_all()
    assert session.get(Impact, other.id).status == "dismissed"

    # labelled issues are ignored once the impact is already merged
    merged, _, _ = factory.breaking_impact(
        repo,
        status="merged",
        github_issue_number=8,
        source=factory.source(kind="npm", locator="@slack/web-api", vendor="slack"),
    )
    done = copy.deepcopy(payload)
    done["issue"]["number"] = 8
    post_webhook(client, "issues", done)
    session.expire_all()
    assert session.get(Impact, merged.id).status == "merged"


def test_issue_closed_by_merging_the_fix_pr_leaves_the_outcome_to_the_pr(client, session, factory):
    """api-correctness-7: merging Kintsua's PR ("Closes #N") closes the issue as completed;
    that must not dismiss the impact before the pull_request event records the merge."""
    org = factory.org()
    repo = factory.repo(org)
    impact, _, _ = factory.breaking_impact(
        repo, status="pr_open", github_issue_number=7, github_pr_number=12
    )
    payload = load_payload("issues_closed")
    payload["issue"]["state_reason"] = "completed"
    r = post_webhook(client, "issues", payload)
    assert r.status_code == 200 and r.json()["skipped"] == "fix PR is open", r.text
    session.expire_all()
    row = session.get(Impact, impact.id)
    assert row.status == "pr_open" and row.fix_reason is None and row.resolved_at is None

    # closing it as "not planned" is a human dismissing the change
    payload["issue"]["state_reason"] = "not_planned"
    r = post_webhook(client, "issues", payload)
    assert r.status_code == 200 and r.json()["status"] == "dismissed"


def test_check_run_completed_calls_record_ci_result(client, factory, monkeypatch):
    org = factory.org()
    repo = factory.repo(org)
    impact, _, _ = factory.breaking_impact(
        repo,
        status="pr_open",
        github_issue_number=7,
        github_pr_number=42,
        pr_branch="kintsua/stripe-1a2b3c4d",
    )
    calls: list[tuple] = []

    def fake_record(session_, repo_, pr_number, conclusion, details):
        calls.append((repo_.id, pr_number, conclusion, details))
        return impact

    monkeypatch.setattr("kintsua.api.webhooks.record_ci_result", fake_record)
    payload = load_payload("check_run_completed")
    r = post_webhook(client, "check_run", payload)
    assert r.status_code == 200, r.text
    assert r.json()["pr_number"] == 42 and r.json()["conclusion"] == "failure"
    assert len(calls) == 1
    repo_id, pr_number, conclusion, details = calls[0]
    assert (repo_id, pr_number, conclusion) == (repo.id, 42, "failure")
    assert details["name"] == "pytest"
    assert (
        details["details_url"] == "https://github.com/acme/support-agent/actions/runs/1001/job/2002"
    )
    assert "KeyError: 'currency'" in details["text"]
    assert details["summary"] == "2 failed, 41 passed in 38.2s"
    assert details["head_branch"] == "kintsua/stripe-1a2b3c4d"

    # a check on a human's branch is skipped
    other = copy.deepcopy(payload)
    other["check_run"]["check_suite"]["head_branch"] = "main"
    other["check_run"]["pull_requests"] = []
    r = post_webhook(client, "check_run", other)
    assert r.status_code == 200 and r.json()["skipped"] == "not a kintsua branch"
    assert len(calls) == 1
    # in-progress runs are ignored
    created = copy.deepcopy(payload)
    created["action"] = "created"
    assert post_webhook(client, "check_run", created).status_code == 202

    # check_suite carries the branch too; the PR number comes from the impact
    suite = {
        "action": "completed",
        "check_suite": {
            "id": 1,
            "head_branch": "kintsua/stripe-1a2b3c4d",
            "head_sha": "4f1c2d3e",
            "status": "completed",
            "conclusion": "success",
            "app": {"name": "GitHub Actions"},
            "pull_requests": [],
        },
        "repository": payload["repository"],
        "installation": payload["installation"],
        "sender": payload["sender"],
    }
    r = post_webhook(client, "check_suite", suite)
    assert r.status_code == 200 and r.json()["pr_number"] == 42
    assert calls[-1][2] == "success" and calls[-1][3]["name"] == "GitHub Actions"


def test_check_run_failure_moves_impact_to_failed_end_to_end(client, session, factory, fake_github):
    org = factory.org()
    repo = factory.repo(org)
    gh_repo = fake_github.repo(REPO_FULL_NAME)
    issue = gh_repo.create_issue(title="Stripe", body="x", labels=["kintsua"])
    impact, _, _ = factory.breaking_impact(
        repo,
        status="pr_open",
        github_issue_number=issue.number,
        github_pr_number=42,
        pr_branch="kintsua/stripe-1a2b3c4d",
    )
    r = post_webhook(client, "check_run", load_payload("check_run_completed"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"
    session.expire_all()
    impact = session.get(Impact, impact.id)
    assert impact.status == "failed" and impact.ci_result["conclusion"] == "failure"
    assert "pytest" in (impact.fix_reason or "")
    assert issue.comments, "a failing CI run comments on the issue"
    last = issue.comments[-1].body
    assert "KeyError" in last and "ghs_abcdefghijklmnopqrstuvwxyz0123456789" not in last


def test_unknown_repo_events_do_not_create_rows(client, session, factory):
    payload = load_payload("push")
    r = post_webhook(client, "push", payload)
    assert r.status_code == 200 and r.json()["skipped"] == "repo not installed"
    assert session.scalar(select(func.count(Repo.id))) == 0
    assert session.scalar(select(func.count(Dependency.id))) == 0
