"""The spec's end-to-end test, taken all the way.

A local MCP server (``tests/fixtures/watchers/mcp_server.py``) changes a tool schema and
the real job handlers carry it through the whole pipeline on the Postgres queue:

    poll_source (1.0.0) -> snapshot
    poll_source (1.1.0) -> change_event -> classify_event -> match_event -> open_issue
    fix_impact (sandbox + scripted agent + validator) -> PR
    PR merged as-is -> learning loop -> weekly report numbers

Only the model (scripted ``FakeLLM``), GitHub (in-memory fake) and the git push are
replaced; the MCP server, the watcher, the diff, the rules, the matcher, the issue body,
the sandbox, the test run and the validator are the real ones.
"""

from __future__ import annotations

import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from kintsua.core.db import new_session
from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Classification,
    Dependency,
    Impact,
    Job,
    Org,
    Repo,
    Snapshot,
    Source,
)
from kintsua.core.queue import JOB_POLL_SOURCE, drain, enqueue
from kintsua.core.util import utcnow
from kintsua.worker import register_all_handlers

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "tests" / "fixtures" / "watchers" / "mcp_server.py"
LOCATOR = f"{sys.executable} {SERVER}"

pytestmark = [pytest.mark.slow, pytest.mark.timeout(600)]


def _meta(variant: str) -> dict:
    return {
        "env": {"KINTSUA_MCP_VARIANT": variant, "PYTHONPATH": str(REPO_ROOT)},
        "quiet": True,
        "timeout_s": 60,
        "transport": "stdio",
    }


def _set_variant(source_id, variant: str) -> None:
    s = new_session()
    try:
        src = s.get(Source, source_id)
        src.meta = {**src.meta, "env": {**src.meta["env"], "KINTSUA_MCP_VARIANT": variant}}
        s.commit()
    finally:
        s.close()


def _poll(source_id) -> int:
    s = new_session()
    try:
        enqueue(s, JOB_POLL_SOURCE, {"source_id": str(source_id)})
        s.commit()
    finally:
        s.close()
    return drain(new_session, [JOB_POLL_SOURCE])


@pytest.fixture
def world(session, fake_github, fake_llm, settings, monkeypatch, tmp_path):
    """An installed repo whose agent calls ``get_weather`` on the fixture server, with the
    fixer wired to a local sandbox built from the fixture agent repo (no network)."""
    import kintsua.fixer.pr as prmod
    import kintsua.fixer.run as runmod
    from kintsua.watchers.run import find_or_create_source
    from tests.fixer.conftest import install_fixture_clone

    register_all_handlers()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "auto_issue", False)  # per-org opt-in below
    install_fixture_clone(monkeypatch)
    pushes: list[dict] = []
    monkeypatch.setattr(prmod, "remote_branch_exists", lambda *a, **k: False)
    monkeypatch.setattr(
        prmod,
        "push_branch",
        lambda sandbox, url, branch: pushes.append({"branch": branch, "sha": sandbox.head_sha()}),
    )
    monkeypatch.setattr(runmod, "run_install", lambda sandbox, plan: None)

    org = Org(
        github_installation_id=424242,
        name="acme",
        settings={"fix_mode": "pr", "auto_issue": True},
    )
    session.add(org)
    session.flush()
    repo = Repo(
        org_id=org.id,
        github_repo_id=990001,
        full_name="acme/weather-billing-agent",
        default_branch="main",
        config={"fix": {"mode": "pr", "base": "main"}},
    )
    session.add(repo)
    session.flush()
    source = find_or_create_source(
        session, "mcp", LOCATOR, display_name="Weather MCP", vendor="weather", meta=_meta("v1")
    )
    dep = Dependency(
        repo_id=repo.id,
        source_id=source.id,
        kind="mcp",
        name=LOCATOR,
        meta={"vendor": "weather", "config_file": ".mcp.json", "transport": "stdio"},
    )
    session.add(dep)
    session.flush()
    session.add(
        CallSite(
            dependency_id=dep.id,
            file_path="agent/weather.py",
            line=14,
            symbol="client.call_tool",
            operation="get_weather",
            args_shape={"keys": ["city"]},
        )
    )
    session.commit()
    return {
        "org_id": org.id,
        "repo_id": repo.id,
        "repo_full_name": repo.full_name,
        "source_id": source.id,
        "pushes": pushes,
        "llm": fake_llm,
        "github": fake_github,
    }


def test_required_param_change_opens_issue_then_tested_pr(world):
    source_id, repo_id = world["source_id"], world["repo_id"]

    # 1. first sight: a snapshot and nothing else
    assert _poll(source_id) == 1
    s = new_session()
    assert s.scalar(select(func.count()).select_from(Snapshot)) == 1
    assert s.scalar(select(func.count()).select_from(ChangeEventRow)) == 0
    src = s.get(Source, source_id)
    assert src.last_error is None and src.consecutive_failures == 0 and src.last_polled_at
    s.close()

    # 2. the vendor ships 1.1.0: `units` is now required
    _set_variant(source_id, "units-required")
    assert _poll(source_id) == 1
    s = new_session()
    row = s.scalar(select(ChangeEventRow))
    assert row is not None
    assert row.diff["from_version"] == "1.0.0" and row.diff["to_version"] == "1.1.0"
    ops = {(o["op"], o["path"]) for o in row.diff["ops"]}
    assert ("required_added", "/tools/get_weather/inputSchema/properties/units") in ops
    assert s.scalar(select(func.count()).select_from(Snapshot)) == 2
    s.close()

    # 3. rules classify it (the model is never consulted) -> matcher -> GitHub issue
    assert drain(new_session, ["classify_event", "match_event", "open_issue"]) >= 3
    assert world["llm"].calls == []
    s = new_session()
    cls = s.scalar(select(Classification))
    assert cls.severity == "breaking" and cls.category == "required_param", cls.rule_hits
    assert "tool:get_weather" in cls.affected_paths
    assert cls.rule_hits and cls.rule_hits[0] == "required_added"
    assert not cls.needs_human
    impact = s.scalar(select(Impact))
    assert impact is not None and impact.status == "issued", impact.status
    assert impact.matched_call_sites[0]["operation"] == "get_weather"
    gh_repo = world["github"].repo(world["repo_full_name"])
    issue = gh_repo.get_issue(impact.github_issue_number)
    assert issue.title.startswith("[Kintsua]") and "units" in issue.title
    assert {lb.name for lb in issue.labels} >= {"kintsua", "severity:breaking"}
    body = issue.body
    assert f"<!-- kintsua:change_event:{row.id} -->" in body
    for section in (
        "What changed upstream",
        "1.0.0",
        "1.1.0",
        "Where this repo calls it",
        "agent/weather.py",
        "Suggested migration",
        "Kintsua will open a PR",
    ):
        assert section in body, section
    fix_jobs = s.scalars(select(Job).where(Job.kind == "fix_impact")).all()
    assert len(fix_jobs) == 1 and fix_jobs[0].status == "queued"
    fix_job_id = fix_jobs[0].id
    impact_id = impact.id
    s.close()

    # 4. the fixer: real sandbox + validator, scripted agent -> PR with the template body
    from tests.fixer.test_run import UNITS, _script_fix

    _script_fix(
        world["llm"], UNITS, reason="Added units='metric', the previous implicit behaviour."
    )
    assert drain(new_session, ["fix_impact"]) == 1
    s = new_session()
    impact = s.get(Impact, impact_id)
    assert impact.status == "pr_open", impact.fix_reason
    assert impact.fix_job_id == fix_job_id
    assert re.fullmatch(r"kintsua/weather-[0-9a-f]{8}", impact.pr_branch)
    assert impact.tokens_used > 0
    assert world["pushes"] and world["pushes"][0]["branch"] == impact.pr_branch
    pr = gh_repo.get_pull(impact.github_pr_number)
    assert pr.base == "main" and str(pr.head) == impact.pr_branch
    assert pr.body.startswith("## What changed upstream\n")
    assert "1.0.0 → 1.1.0" in pr.body
    assert "## What this PR changes\n1 files, 1 call sites." in pr.body
    assert "agent/weather.py" in pr.body
    assert "## How it was verified" in pr.body and "exit 0" in pr.body
    assert pr.body.rstrip().endswith(f"Closes #{impact.github_issue_number}. Opened by Kintsua.")
    # the agent saw the diff and the call site as data, never as instructions
    first_user = world["llm"].calls[0]["messages"][0].content
    assert "<data>" in first_user and "agent/weather.py" in first_user
    assert "units" not in world["llm"].calls[0]["system"]
    # the issue got a comment linking the PR
    issue = gh_repo.get_issue(impact.github_issue_number)
    assert any(f"PR #{impact.github_pr_number}" in c.body for c in issue.comments)
    s.close()

    # 5. the customer merges it as-is: the learning loop records the north-star metric
    from kintsua.fixer.run import record_pr_outcome

    s = new_session()
    repo = s.get(Repo, repo_id)
    pr.merged = True
    out = record_pr_outcome(
        s,
        repo,
        impact.github_pr_number,
        True,
        "closed",
        head_sha=world["pushes"][0]["sha"],
        merge_commit_sha="deadbeef",
    )
    assert out.status == "merged" and out.pr_outcome == "merged_as_is" and out.resolved_at
    s.commit()

    # 6. the weekly report is written from these rows
    from kintsua.report.build import build_weekly_report
    from kintsua.report.markdown import render_markdown

    rep = build_weekly_report(s, utcnow() - timedelta(days=7), utcnow() + timedelta(minutes=1))
    st = rep.stats
    assert st.change_events == 1 and st.breaking == 1
    assert st.issues_opened == 1 and st.prs_opened == 1 and st.prs_merged == 1
    assert st.merged_as_is_rate == 1.0
    md = render_markdown(rep)
    assert "get_weather" in md and "Breaking (1)" in md
    s.close()


def test_tool_removal_opens_issue_but_is_never_auto_fixed(world):
    """v2 removes a tool: breaking/removal opens an issue, and because removals are not a
    fixable category in v0 no fix job is queued; the issue says so."""
    source_id = world["source_id"]
    assert _poll(source_id) == 1
    _set_variant(source_id, "v2")
    assert _poll(source_id) == 1
    drain(new_session, ["classify_event", "match_event", "open_issue", "fix_impact"])
    s = new_session()
    cls = s.scalar(select(Classification))
    assert cls.severity == "breaking" and cls.category == "removal"
    assert "tool:search" in cls.affected_paths
    impact = s.scalar(select(Impact))
    assert impact.status == "issued" and impact.fix_reason is None
    assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "fix_impact")) == 0
    gh_repo = world["github"].repo(world["repo_full_name"])
    issue = gh_repo.get_issue(impact.github_issue_number)
    assert issue.state == "open" and "not auto-fixed" in issue.body
    assert world["pushes"] == [] and world["llm"].calls == []
    s.close()
