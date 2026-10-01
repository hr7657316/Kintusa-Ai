from __future__ import annotations

import re
import threading
from datetime import UTC, datetime

import pytest

from kintsua.core.github import BRANCH_PREFIX, LABEL, LABEL_BREAKING
from kintsua.fixer.pr import (
    PR_BODY_TEMPLATE,
    PushRefused,
    branch_name,
    commit_and_push,
    commit_message,
    open_pull_request,
    pr_title,
    render_pr_body,
    vendor_slug,
)
from kintsua.fixer.sandbox import LocalSandbox
from kintsua.fixer.testcmd import detect_test_command
from kintsua.fixer.validate import Verdict
from tests.fakes.github import FakeBranch, FakeRepo
from tests.fixer.conftest import context_from_break, load_break


def test_branch_name_and_slug():
    assert vendor_slug("Stripe API") == "stripe-api"
    assert vendor_slug("  Weather / MCP!! ") == "weather-mcp"
    assert vendor_slug("") == "vendor"
    b = branch_name("stripe", "11111111-2222-3333-4444-555555555555")
    assert re.fullmatch(rf"{re.escape(BRANCH_PREFIX)}stripe-[0-9a-f]{{8}}", b)
    assert b == branch_name("stripe", "11111111-2222-3333-4444-555555555555")  # deterministic
    assert b != branch_name("stripe", "another-id")


def _verdict() -> Verdict:
    return Verdict(
        ok=True,
        tests_passed=3,
        test_exit=0,
        baseline_result="exit 0, 3 passed",
        static_result="`ruff check .` → pass",
        files_changed=1,
        call_sites_changed=1,
        lines_changed=2,
        changed_files=["agent/billing.py"],
        file_notes=["- `agent/billing.py:12` — updated call site `stripe.Charge.create` (+1/-1)"],
        test_command="python -m pytest -q",
        mcp_check=None,
    )


def test_render_pr_body_matches_the_template(sandbox: LocalSandbox):
    brk = load_break("stripe_charge_source_renamed")
    ctx = context_from_break(brk, sandbox, detect_test_command(sandbox.workdir, {}))
    ctx.detected_at = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    body = render_pr_body(ctx, _verdict(), 7, agent_notes="Renamed the argument.")
    expected = PR_BODY_TEMPLATE.format(
        vendor="stripe",
        source_kind="openapi",
        from_version="2024-06-20",
        to_version="2024-09-30",
        detected_at="2026-10-01 12:30 UTC",
        summary="POST /v1/charges: request parameter `source` was renamed to `payment_method`",
        files_changed=1,
        call_sites_changed=1,
        changes="- `agent/billing.py:12` — updated call site `stripe.Charge.create` (+1/-1)",
        test_cmd="python -m pytest -q",
        test_exit=0,
        tests_passed=3,
        baseline_result="exit 0, 3 passed",
        static_result="`ruff check .` → pass",
        review_notes=(
            "Migration hint: Rename the `source` argument of Charge.create to `payment_method`; the value is unchanged.\n"
            "Agent: Renamed the argument."
        ),
        issue_number=7,
    )
    assert body == expected
    for heading in (
        "## What changed upstream",
        "## What this PR changes",
        "## How it was verified",
        "## Review notes",
    ):
        assert heading in body
    assert body.rstrip().endswith("Closes #7. Opened by Kintsua.")
    assert "- Test command: `python -m pytest -q` → exit 0 (3 passed)" in body


def test_render_pr_body_escapes_vendor_text(sandbox: LocalSandbox):
    brk = load_break("stripe_charge_source_renamed")
    ctx = context_from_break(brk, sandbox)
    ctx.summary = "# not a heading <script>alert(1)</script>"
    ctx.migration_hint = None
    v = _verdict()
    v.preexisting_failures = ["tests/test_flaky.py::test_always_fails"]
    body = render_pr_body(ctx, v, None)
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "\\# not a heading" in body
    assert "Pre-existing failing tests" in body and "test_always_fails" in body
    assert "Closes #" not in body and body.rstrip().endswith("Opened by Kintsua.")


def test_pr_title_and_commit_message(sandbox: LocalSandbox):
    ctx = context_from_break(load_break("stripe_charge_source_renamed"), sandbox)
    assert pr_title(ctx).startswith("[Kintsua] fix: POST /v1/charges")
    msg = commit_message(ctx)
    assert msg.splitlines()[0].startswith("fix(stripe): ")
    assert len(msg.splitlines()[0]) <= 72
    assert "Closes #7" in msg and "Opened by Kintsua." in msg


def test_commit_and_push_creates_commit_and_pushes_once(sandbox: LocalSandbox, no_push):
    sandbox.apply_patch(
        {
            "path": "agent/billing.py",
            "old": "        source=source,\n",
            "new": "        payment_method=source,\n",
        }
    )
    repo = FakeRepo("acme/agent")
    branch = branch_name("stripe", "abc")
    result = commit_and_push(
        sandbox,
        branch,
        "fix(stripe): rename",
        clone_url="https://x-access-token:tok@github.com/acme/agent.git",
        gh_repo=repo,
        base_branch="main",
    )
    assert result.branch == branch and re.fullmatch(r"[0-9a-f]{40}", result.sha)
    assert result.sha == sandbox.head_sha() and result.sha != sandbox.base_sha
    assert not result.replaced_existing
    assert no_push == [
        {
            "branch": branch,
            "sha": result.sha,
            "url": "https://x-access-token:tok@github.com/acme/agent.git",
        }
    ]
    assert sandbox.git_ok("rev-parse", "--abbrev-ref", "HEAD").strip() == branch
    assert sandbox.changed_files() == ["agent/billing.py"]  # diff vs base sha still shows the fix
    assert sandbox.git_ok("status", "--porcelain").strip() == ""
    # the token never lands in the clone's config
    assert "tok@" not in sandbox.git_ok("remote", "-v")


def test_commit_and_push_replaces_our_stale_branch(sandbox: LocalSandbox, monkeypatch):
    import kintsua.fixer.pr as prmod

    calls: list[str] = []
    monkeypatch.setattr(prmod, "remote_branch_exists", lambda sb, url, b: True)
    monkeypatch.setattr(
        prmod, "delete_remote_branch", lambda sb, url, b: calls.append(f"delete {b}")
    )
    monkeypatch.setattr(prmod, "push_branch", lambda sb, url, b: calls.append(f"push {b}"))
    sandbox.write_file("agent/helpers.py", sandbox.read_file("agent/helpers.py") + "\n")
    branch = branch_name("stripe", "abc")
    result = commit_and_push(sandbox, branch, "msg", clone_url="file:///x")
    assert result.replaced_existing and calls == [f"delete {branch}", f"push {branch}"]


def test_commit_and_push_refusals(sandbox: LocalSandbox, no_push):
    repo = FakeRepo("acme/agent")
    with pytest.raises(PushRefused, match="nothing to commit"):
        commit_and_push(
            sandbox, branch_name("stripe", "x"), "m", clone_url="file:///x", gh_repo=repo
        )
    sandbox.write_file("agent/helpers.py", sandbox.read_file("agent/helpers.py") + "\n")
    with pytest.raises(PushRefused, match="not a kintsua/ branch"):
        commit_and_push(sandbox, "main", "m", clone_url="file:///x", gh_repo=repo)
    with pytest.raises(PushRefused, match="base branch"):
        commit_and_push(
            sandbox,
            "kintsua/main",
            "m",
            clone_url="file:///x",
            gh_repo=repo,
            base_branch="kintsua/main",
        )
    protected = branch_name("stripe", "p")
    repo.branches[protected] = FakeBranch(protected, protected=True)
    with pytest.raises(PushRefused, match="protected"):
        commit_and_push(
            sandbox, protected, "m", clone_url="file:///x", gh_repo=repo, base_branch="main"
        )
    assert no_push == []
    assert sandbox.git_ok("status", "--porcelain").strip() != ""  # nothing was committed


def test_open_pull_request_labels_and_lock():
    repo = FakeRepo("acme/agent")
    lock = threading.BoundedSemaphore(2)
    number = open_pull_request(
        repo,
        "t",
        "b",
        head="kintsua/stripe-abc",
        base="main",
        labels=[LABEL, LABEL_BREAKING],
        write_lock=lock,
    )
    pr = repo.get_pull(number)
    assert (
        pr.title == "t" and pr.body == "b" and pr.head == "kintsua/stripe-abc" and pr.base == "main"
    )
    assert {lb.name for lb in pr.labels} == {LABEL, LABEL_BREAKING}
    assert set(repo.labels) == {LABEL, LABEL_BREAKING}
    assert lock._value == 2  # released
