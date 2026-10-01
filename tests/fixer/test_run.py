from __future__ import annotations

import re
import shutil
import types
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from kintsua.core.github import LABEL, LABEL_BREAKING
from kintsua.core.llm import FakeLLM
from kintsua.core.models import Impact, Job
from kintsua.core.queue import HANDLERS, JOB_FIX_IMPACT
from kintsua.core.util import utcnow
from kintsua.fixer.run import (
    handle_fix_impact,
    handle_pr_outcome,
    merged_outcome,
    record_ci_result,
    record_pr_outcome,
)
from tests.fixer.conftest import AGENT_REPO, Factory, install_fixture_clone, load_break

RENAME = {
    "path": "agent/billing.py",
    "old": "        source=source,\n",
    "new": "        payment_method=source,\n",
}
UNITS = {
    "path": "agent/weather.py",
    "old": '    result = client.call_tool("get_weather", {"city": city})\n',
    "new": '    result = client.call_tool("get_weather", {"city": city, "units": "metric"})\n',
}


def _script_fix(
    fake_llm: FakeLLM, patch: dict, reason: str = "Applied the vendor migration."
) -> None:
    fake_llm.queue_turn(
        tool_calls=[("read_file", {"path": patch["path"], "start_line": None, "end_line": None})]
    )
    fake_llm.queue_turn(tool_calls=[("apply_patch", patch)])
    fake_llm.queue_turn(tool_calls=[("run_tests", {})])
    fake_llm.queue_turn(tool_calls=[("done", {"reason": reason, "needs_human": False})])


def _issue(fake_github, world):
    return fake_github.repo(world["repo"].full_name).get_issue(world["issue_number"])


@pytest.fixture
def ready(fixture_clone, no_push, no_install, data_dir, fake_github, fake_llm, settings):
    """Everything the handler needs without network: fixture clone, no push, no install."""
    return {"pushes": no_push, "github": fake_github, "llm": fake_llm, "settings": settings}


def test_handler_is_registered():
    assert HANDLERS[JOB_FIX_IMPACT] is handle_fix_impact
    assert "pr_outcome" in HANDLERS


def test_fix_impact_opens_a_pr(session, f: Factory, ready):
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    _script_fix(ready["llm"], RENAME)
    result = handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    assert result["status"] == "pr_open", result

    impact = session.get(Impact, world["impact"].id)
    session.refresh(impact)
    assert impact.status == "pr_open"
    assert impact.github_pr_number == result["pr_number"]
    assert impact.pr_branch and re.fullmatch(r"kintsua/stripe-[0-9a-f]{8}", impact.pr_branch)
    assert impact.tokens_used == 40 + 20
    assert impact.fix_reason is None and impact.resolved_at is None
    assert ready["pushes"] and ready["pushes"][0]["branch"] == impact.pr_branch
    assert impact.ci_result["pushed_sha"] == ready["pushes"][0]["sha"]

    gh_repo = ready["github"].repo(world["repo"].full_name)
    pr = gh_repo.get_pull(impact.github_pr_number)
    assert pr.head == impact.pr_branch and pr.base == "main"
    assert pr.title.startswith("[Kintsua] fix: ")
    assert {lb.name for lb in pr.labels} == {LABEL, LABEL_BREAKING}
    body = pr.body
    assert body.startswith(
        "## What changed upstream\nstripe openapi 2024-06-20 → 2024-09-30, detected "
    )
    assert (
        "## What this PR changes\n1 files, 1 call sites.\n- `agent/billing.py:12` — updated call site `stripe.Charge.create` (+1/-1)"
        in body
    )
    assert "- Test command: `python -m pytest -q` → exit 0 (3 passed)" in body
    assert "- Baseline on unpatched tree: exit 0, 3 passed; `ruff check .` exit 0" in body
    assert "- Static checks: `ruff check .` → pass" in body
    assert "Agent: Applied the vendor migration." in body
    assert body.rstrip().endswith(f"Closes #{world['issue_number']}. Opened by Kintsua.")

    comments = _issue(ready["github"], world).comments
    assert len(comments) == 1 and f"PR #{impact.github_pr_number}" in comments[0].body

    # the agent saw the call site with context, as data
    first_user = ready["llm"].calls[0]["messages"][0].content
    assert "<data>" in first_user and "agent/billing.py:12" in first_user
    # a retry of the same job is a no-op
    assert handle_fix_impact(session, {"impact_id": str(impact.id)})["skipped"] is True


def test_fix_impact_mcp_reads_new_schema_from_snapshot(session, f: Factory, ready):
    world = f.seed_break(load_break("mcp_get_weather_units_required"), fake_github=ready["github"])
    _script_fix(ready["llm"], UNITS)
    result = handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    assert result["status"] == "pr_open", result
    first_user = ready["llm"].calls[0]["messages"][0].content
    assert "New MCP tool inputSchema for get_weather" in first_user and '"units"' in first_user
    pr = ready["github"].repo(world["repo"].full_name).get_pull(result["pr_number"])
    assert "MCP schema check: 1 call site(s) validated against the new inputSchema" in pr.body
    assert "weather mcp 1.4.0 → 1.5.0" in pr.body


def test_refuses_unfixable_category(session, f: Factory, ready):
    world = f.seed_break(
        load_break("stripe_charge_source_renamed"),
        fake_github=ready["github"],
        classification_overrides={"category": "removal"},
    )
    result = handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    impact = session.get(Impact, world["impact"].id)
    assert result["status"] == "failed" and impact.status == "failed"
    assert "not auto-fixed" in impact.fix_reason and impact.resolved_at is not None
    assert "not auto-fixed" in _issue(ready["github"], world).comments[0].body
    assert ready["llm"].calls == [] and ready["pushes"] == []


def test_refuses_auth_and_issue_only_mode(session, f: Factory, ready):
    world = f.seed_break(
        load_break("stripe_charge_source_renamed"),
        fake_github=ready["github"],
        classification_overrides={"category": "auth"},
    )
    handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    assert "authentication" in session.get(Impact, world["impact"].id).fix_reason

    world2 = f.seed_break(
        load_break("stripe_charge_source_renamed"),
        fake_github=ready["github"],
        org_settings={"fix_mode": "issue_only"},
    )
    handle_fix_impact(session, {"impact_id": str(world2["impact"].id)})
    assert "fix mode is 'issue_only'" in session.get(Impact, world2["impact"].id).fix_reason

    # repo config overrides the org default
    world3 = f.seed_break(
        load_break("stripe_charge_source_renamed"),
        fake_github=ready["github"],
        org_settings={"fix_mode": "pr"},
        repo_config={"fix": {"mode": "issue_only"}},
    )
    handle_fix_impact(session, {"impact_id": str(world3["impact"].id)})
    assert "issue_only" in session.get(Impact, world3["impact"].id).fix_reason


def test_refuses_repo_without_tests_or_static_checks(
    session, f: Factory, ready, monkeypatch, tmp_path: Path
):
    bare = tmp_path / "bare-repo"
    shutil.copytree(AGENT_REPO, bare, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    (bare / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
    install_fixture_clone(monkeypatch, bare)
    import kintsua.fixer.run as runmod

    monkeypatch.setattr(runmod, "detect_test_command", _no_tools)
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    result = handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    impact = session.get(Impact, world["impact"].id)
    assert result["status"] == "failed"
    assert "no test command and no static checks" in impact.fix_reason
    assert "unlocks PRs" in _issue(ready["github"], world).comments[0].body
    assert ready["llm"].calls == []


def _no_tools(wd, cfg):
    from kintsua.fixer.testcmd import detect_test_command

    return detect_test_command(wd, cfg, tool_available=lambda n: False)


def test_concurrency_defers_and_reenqueues(session, f: Factory, ready, monkeypatch):
    monkeypatch.setattr(ready["settings"], "fix_concurrency", 2)
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    for _ in range(2):
        other = f.seed_break(
            load_break("stripe_charge_source_renamed"), fake_github=ready["github"], status="fixing"
        )
        assert other["impact"].status == "fixing"
    before = utcnow()
    result = handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    assert result["deferred"] is True
    impact = session.get(Impact, world["impact"].id)
    assert impact.status == "issued"
    job = session.scalar(
        select(Job).where(Job.dedupe_key == f"fix_impact:{impact.id}", Job.status == "queued")
    )
    assert (
        job is not None
        and job.kind == JOB_FIX_IMPACT
        and job.payload == {"impact_id": str(impact.id)}
    )
    assert job.run_after >= before + timedelta(seconds=55)
    assert ready["llm"].calls == []
    # once a slot frees up the same job runs to completion
    for imp in session.scalars(select(Impact).where(Impact.status == "fixing")):
        imp.status = "failed"
    session.commit()
    _script_fix(ready["llm"], RENAME)
    assert handle_fix_impact(session, {"impact_id": str(impact.id)})["status"] == "pr_open"


def test_needs_human_fails_with_comment(session, f: Factory, ready):
    world = f.seed_break(load_break("mcp_get_weather_units_required"), fake_github=ready["github"])
    ready["llm"].queue_turn(
        tool_calls=[
            (
                "done",
                {"reason": "units cannot be inferred for this deployment", "needs_human": True},
            )
        ]
    )
    result = handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    impact = session.get(Impact, world["impact"].id)
    assert result["status"] == "failed" and impact.status == "failed"
    assert "human decision" in impact.fix_reason and "cannot be inferred" in impact.fix_reason
    assert impact.tokens_used == 15
    comment = _issue(ready["github"], world).comments[0].body
    assert "could not open a fix PR" in comment and "cannot be inferred" in comment
    assert "Log excerpt" in comment
    assert ready["pushes"] == []


def test_validation_failure_is_recorded(session, f: Factory, ready):
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    bad = {
        "path": "agent/unrelated.py",
        "old": '    return "\\n".join(',
        "new": '    return "\\n".join( ',
    }
    ready["llm"].queue_turn(tool_calls=[("apply_patch", RENAME), ("apply_patch", bad)])
    ready["llm"].queue_turn(tool_calls=[("done", {"reason": "done", "needs_human": False})])
    result = handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    impact = session.get(Impact, world["impact"].id)
    assert result["status"] == "failed"
    assert (
        "validation failed" in impact.fix_reason
        and "outside the affected call sites" in impact.fix_reason
    )
    assert "agent/unrelated.py" in _issue(ready["github"], world).comments[0].body
    assert ready["pushes"] == []


def test_agent_tokens_logged_and_sandbox_cleaned(session, f: Factory, ready, caplog, monkeypatch):
    import kintsua.fixer.run as runmod

    roots: list[Path] = []
    real = runmod.make_sandbox

    def spy(settings, **kw):
        sb = real(settings, **kw)
        roots.append(sb.root)
        return sb

    monkeypatch.setattr(runmod, "make_sandbox", spy)
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    _script_fix(ready["llm"], RENAME)
    with caplog.at_level("INFO", logger="kintsua.fixer.run"):
        handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    assert any(
        "fix_impact tokens_in=40 tokens_out=20 model=fake" in r.getMessage() for r in caplog.records
    )
    assert roots and not roots[0].exists()


# --------------------------------------------------------------------------------------
# PR / CI outcomes
# --------------------------------------------------------------------------------------


def _pr_open_world(f: Factory, fake_github, pushed_sha: str = "a" * 40):
    world = f.seed_break(
        load_break("stripe_charge_source_renamed"), fake_github=fake_github, status="pr_open"
    )
    gh_repo = fake_github.repo(world["repo"].full_name)
    pr = gh_repo.create_pull(title="t", body="b", head="kintsua/stripe-deadbeef", base="main")
    impact = world["impact"]
    impact.github_pr_number = pr.number
    impact.pr_branch = "kintsua/stripe-deadbeef"
    impact.ci_result = {"pushed_sha": pushed_sha}
    f.session.commit()
    world["pr"] = pr
    return world


def test_merged_outcome_helper():
    assert merged_outcome("abc", "ABC") == "merged_as_is"
    assert merged_outcome("abc", "def") == "merged_edited"
    assert merged_outcome(None, "def") == "merged_unverified"


def test_record_pr_outcome_merged_as_is_vs_edited_vs_closed(session, f: Factory, fake_github):
    w = _pr_open_world(f, fake_github)
    impact = record_pr_outcome(
        session, w["repo"], w["pr"].number, True, "closed", head_sha="a" * 40
    )
    assert impact is not None and impact.status == "merged" and impact.pr_outcome == "merged_as_is"
    assert impact.resolved_at is not None and impact.ci_result["merged_head_sha"] == "a" * 40

    w2 = _pr_open_world(f, fake_github)
    w2["pr"].head = types.SimpleNamespace(
        sha="b" * 40, ref="kintsua/stripe-deadbeef"
    )  # looked up via PyGithub
    impact = record_pr_outcome(session, w2["repo"], w2["pr"].number, True, "closed")
    assert impact.status == "merged" and impact.pr_outcome == "merged_edited"

    w3 = _pr_open_world(f, fake_github)
    impact = record_pr_outcome(session, w3["repo"], w3["pr"].number, False, "closed")
    assert (
        impact.status == "dismissed"
        and impact.pr_outcome == "closed"
        and impact.resolved_at is not None
    )
    impact = record_pr_outcome(session, w3["repo"], w3["pr"].number, False, "reopened")
    assert impact.status == "pr_open" and impact.pr_outcome is None

    assert record_pr_outcome(session, w3["repo"], 9999, True, "closed") is None


def test_pr_outcome_job_handler(session, f: Factory, fake_github):
    w = _pr_open_world(f, fake_github)
    out = handle_pr_outcome(
        session,
        {
            "repo_id": str(w["repo"].id),
            "pr_number": w["pr"].number,
            "action": "closed",
            "merged": True,
            "head_sha": "a" * 40,
        },
    )
    assert out["status"] == "merged" and out["pr_outcome"] == "merged_as_is"
    out = handle_pr_outcome(
        session,
        {"repo_id": str(w["repo"].id), "pr_number": 4242, "action": "closed", "merged": False},
    )
    assert out["impact"] is None
    with pytest.raises(Exception, match="bad payload"):
        handle_pr_outcome(session, {"repo_id": "nope"})


def test_record_ci_result_failure_moves_to_failed_with_comment(session, f: Factory, fake_github):
    w = _pr_open_world(f, fake_github)
    log = (
        "\n".join(f"line {i}" for i in range(100))
        + "\nERROR: token=ghp_abcdefghijklmnopqrstuvwxyz1234 leaked"
    )
    impact = record_ci_result(
        session,
        w["repo"],
        w["pr"].number,
        "failure",
        {"name": "ci / test", "log": log, "details_url": "https://ci.example/run/1"},
    )
    assert (
        impact.status == "failed"
        and "reported failure" in impact.fix_reason
        and impact.resolved_at is not None
    )
    assert (
        impact.ci_result["conclusion"] == "failure" and impact.ci_result["pushed_sha"] == "a" * 40
    )
    assert "log" not in impact.ci_result["details"]
    comment = fake_github.repo(w["repo"].full_name).get_issue(w["issue_number"]).comments[-1].body
    assert "could not fix this automatically" in comment and "https://ci.example/run/1" in comment
    assert "line 99" in comment and "line 10\n" not in comment  # last 40 lines only
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234" not in comment and "[REDACTED]" in comment


def test_record_ci_result_success_keeps_pr_open(session, f: Factory, fake_github):
    w = _pr_open_world(f, fake_github)
    impact = record_ci_result(session, w["repo"], w["pr"].number, "success", {"name": "ci"})
    assert impact.status == "pr_open" and impact.ci_result["conclusion"] == "success"
    assert fake_github.repo(w["repo"].full_name).get_issue(w["issue_number"]).comments == []
    assert record_ci_result(session, w["repo"], 777, "failure", {}) is None


def test_fix_impact_bad_payloads(session):
    from kintsua.core.queue import NonRetryable

    with pytest.raises(NonRetryable):
        handle_fix_impact(session, {"impact_id": "not-a-uuid"})
    with pytest.raises(NonRetryable):
        handle_fix_impact(session, {"impact_id": str(uuid.uuid4())})
