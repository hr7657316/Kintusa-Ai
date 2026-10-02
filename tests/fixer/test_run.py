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


def _live_fixing(
    f: Factory, fake_github, started_s_ago: float = 5.0, job_status: str | None = "running"
):
    """An impact in ``fixing`` whose job started ``started_s_ago`` seconds ago; the job row
    its ``fix_job_id`` points at has ``job_status`` (None: no job recorded)."""
    other = f.seed_break(
        load_break("stripe_charge_source_renamed"), fake_github=fake_github, status="fixing"
    )
    started = utcnow() - timedelta(seconds=started_s_ago)
    other["impact"].ci_result = {"fix_started_at": started.isoformat()}
    if job_status is not None:
        job = Job(
            kind=JOB_FIX_IMPACT,
            payload={"impact_id": str(other["impact"].id)},
            status=job_status,
            attempts=1,
            locked_at=started if job_status == "running" else None,
        )
        f.session.add(job)
        f.session.flush()
        other["impact"].fix_job_id = job.id
        other["job"] = job
    f.session.commit()
    return other


def test_concurrency_defers_and_reenqueues(session, f: Factory, ready, monkeypatch):
    monkeypatch.setattr(ready["settings"], "fix_concurrency", 2)
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    for _ in range(2):
        other = _live_fixing(f, ready["github"])
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
        and job.payload == {"impact_id": str(impact.id), "deferrals": 1}
    )
    assert job.run_after >= before + timedelta(seconds=55)
    assert ready["llm"].calls == []
    # once a slot frees up the same job runs to completion
    for imp in session.scalars(select(Impact).where(Impact.status == "fixing")):
        imp.status = "failed"
    session.commit()
    _script_fix(ready["llm"], RENAME)
    assert handle_fix_impact(session, {"impact_id": str(impact.id)})["status"] == "pr_open"


def test_reaped_fix_job_frees_its_concurrency_slot(session, f: Factory, ready, monkeypatch):
    """Only impacts whose ``fix_job_id`` is a ``running`` job hold a slot: a fix job the
    queue reaped (lease expired: re-queued or failed) or never recorded frees its slot at
    once, even though the impact is still ``fixing`` and freshly stamped."""
    import kintsua.fixer.run as runmod

    monkeypatch.setattr(ready["settings"], "fix_concurrency", 1)
    reaped = _live_fixing(f, ready["github"], job_status="queued")  # reaped, re-queued
    failed = _live_fixing(f, ready["github"], job_status="failed")  # reaped, out of attempts
    done = _live_fixing(f, ready["github"], job_status="done")
    unrecorded = _live_fixing(f, ready["github"], job_status=None)
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    assert runmod._fixing_count(session, world["impact"]) == 0
    _script_fix(ready["llm"], RENAME)
    result = handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    assert result["status"] == "pr_open", result  # not deferred behind reaped jobs
    for w in (reaped, failed, done, unrecorded):  # still fixing; the sweep is time-based
        assert session.get(Impact, w["impact"].id).status == "fixing"

    # a running job holds the slot
    live = _live_fixing(f, ready["github"])
    other = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    assert runmod._fixing_count(session, other["impact"]) == 1
    assert handle_fix_impact(session, {"impact_id": str(other["impact"].id)})["deferred"] is True
    # the queue reaps it: the slot is free on the next attempt
    live["job"].status = "queued"
    live["job"].locked_at = None
    session.commit()
    assert runmod._fixing_count(session, other["impact"]) == 0


def test_fix_impact_records_its_job_id(session, f: Factory, ready):
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    job = Job(kind=JOB_FIX_IMPACT, payload={}, status="running", attempts=1, locked_at=utcnow())
    session.add(job)
    session.commit()
    _script_fix(ready["llm"], RENAME)
    payload = {"impact_id": str(world["impact"].id), "_job_id": str(job.id)}
    assert handle_fix_impact(session, payload)["status"] == "pr_open"
    assert session.get(Impact, world["impact"].id).fix_job_id == job.id


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


def test_merge_after_close_clears_the_dismissal_reason(session, f: Factory, fake_github):
    """Closed (dismissed, "PR closed without merge"), reopened, then merged: the merged
    impact must not keep the dismissal's ``fix_reason``."""
    w = _pr_open_world(f, fake_github)
    impact = record_pr_outcome(session, w["repo"], w["pr"].number, False, "closed")
    assert impact.status == "dismissed" and impact.fix_reason == "PR closed without merge"
    impact = record_pr_outcome(session, w["repo"], w["pr"].number, False, "reopened")
    assert impact.status == "pr_open" and impact.fix_reason is None
    # the close reason is set again by a second close, and a merge clears it
    impact = record_pr_outcome(session, w["repo"], w["pr"].number, False, "closed")
    assert impact.fix_reason == "PR closed without merge"
    impact.status = "pr_open"  # e.g. the reopen webhook was lost
    session.commit()
    impact = record_pr_outcome(
        session, w["repo"], w["pr"].number, True, "closed", head_sha="a" * 40
    )
    assert impact.status == "merged" and impact.pr_outcome == "merged_as_is"
    assert impact.fix_reason is None
    session.expire_all()
    assert session.get(Impact, impact.id).fix_reason is None

    # a merge after a CI failure drops the CI reason too
    w2 = _pr_open_world(f, fake_github)
    record_ci_result(session, w2["repo"], w2["pr"].number, "failure", {"name": "tests"})
    impact = session.get(Impact, w2["impact"].id)
    assert impact.status == "failed" and impact.fix_reason
    impact = record_pr_outcome(
        session, w2["repo"], w2["pr"].number, True, "closed", head_sha="a" * 40
    )
    assert impact.status == "merged" and impact.fix_reason is None


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
        "\n".join(f"line {i}" for i in range(100)) + "\nERROR: token=ghp_abcdefghijklm"
        "nopqrstuvwxyz1234 leaked"
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
    assert ("ghp_abcdefghijklm" + "nopqrstuvwxyz1234") not in comment and "[REDACTED]" in comment


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


# --------------------------------------------------------------------------------------
# review regressions
# --------------------------------------------------------------------------------------


def test_fix_impact_on_the_docker_backend_opens_a_pr(
    session, f: Factory, ready, monkeypatch, tmp_path: Path, fixture_git_repo: Path
):
    """The production backend: the clone lives at ``/work/repo`` inside the container, so
    the test plan must be read through the sandbox (it used to be read on the worker's
    filesystem, find nothing and refuse every job with "no test command")."""
    import socket

    import kintsua.fixer.run as runmod
    from kintsua.fixer.sandbox import DockerSandbox
    from tests.fixer.conftest import clone_url_for
    from tests.fixer.test_sandbox_docker import FakeDockerClient

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443))],
    )
    monkeypatch.delenv("SANDBOX_HTTPS_PROXY", raising=False)

    class FixtureDockerSandbox(DockerSandbox):
        def _git_clone_args(self, clone_url, branch, dest, sha):  # the fixture, not GitHub
            return super()._git_clone_args(clone_url_for(fixture_git_repo), branch, dest, sha)

    made: list[DockerSandbox] = []

    def docker_backend(settings, **kw):
        sb = FixtureDockerSandbox(settings=settings, client=FakeDockerClient(tmp_path / "d"), **kw)
        made.append(sb)
        return sb

    monkeypatch.setattr(runmod, "make_sandbox", docker_backend)
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    _script_fix(ready["llm"], RENAME)
    result = handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    impact = session.get(Impact, world["impact"].id)
    assert result["status"] == "pr_open", impact.fix_reason
    assert made and made[0].workdir == "/work/repo" and made[0].backend == "docker"
    pr = ready["github"].repo(world["repo"].full_name).get_pull(impact.github_pr_number)
    assert "- Test command: `python -m pytest -q` → exit 0 (3 passed)" in pr.body
    assert "`ruff check .` → pass" in pr.body


def test_push_uses_a_freshly_minted_token(session, f: Factory, ready, monkeypatch):
    """The clone URL minted at job start may hold a token that expires mid-job: the push
    gets a new one, and both are scrubbed from everything the job logs."""
    import kintsua.fixer.pr as prmod
    import kintsua.fixer.run as runmod

    minted: list[str] = []

    def installation_token(installation_id: int) -> str:
        minted.append(f"ghs_tok{len(minted) + 1}_abcdefghijklmnop")
        return minted[-1]

    monkeypatch.setattr(ready["github"], "installation_token", installation_token)
    pushed_urls: list[str] = []

    def push_branch(sandbox, url, branch):
        pushed_urls.append(url)
        sandbox.record(f"git push {url} {branch}")  # what a failing push would log

    monkeypatch.setattr(prmod, "push_branch", push_branch)
    sandboxes = []
    real = runmod.make_sandbox
    monkeypatch.setattr(
        runmod,
        "make_sandbox",
        lambda settings, **kw: sandboxes.append(real(settings, **kw)) or sandboxes[-1],
    )
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    _script_fix(ready["llm"], RENAME)
    assert handle_fix_impact(session, {"impact_id": str(world["impact"].id)})["status"] == "pr_open"
    clone_token = minted[0]
    assert len(pushed_urls) == 1 and clone_token not in pushed_urls[0]
    push_token = next(t for t in minted if t in pushed_urls[0])
    assert minted.index(push_token) > 0  # minted after the clone, right before the push
    log_text = "\n".join(sandboxes[0].log_lines)
    assert clone_token not in log_text and push_token not in log_text and "git push" in log_text


def test_lost_fixing_impacts_free_their_slots(session, f: Factory, ready, monkeypatch):
    """Workers killed mid-job (deploy, OOM) leave impacts in ``fixing``: they must not hold
    the concurrency slots forever. Each is re-queued once, then failed honestly."""
    settings = ready["settings"]
    monkeypatch.setattr(settings, "fix_concurrency", 3)
    stale = [
        _live_fixing(f, ready["github"], started_s_ago=3 * settings.sandbox_wall_clock_s)
        for _ in range(2)
    ]
    legacy = f.seed_break(
        load_break("stripe_charge_source_renamed"), fake_github=ready["github"], status="fixing"
    )  # never stamped (started before stamping existed)
    live = _live_fixing(f, ready["github"])
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    _script_fix(ready["llm"], RENAME)
    result = handle_fix_impact(session, {"impact_id": str(world["impact"].id)})
    assert result["status"] == "pr_open", result  # not deferred behind dead jobs
    for w in [*stale, legacy]:
        imp = session.get(Impact, w["impact"].id)
        session.refresh(imp)
        assert imp.status == "issued" and imp.ci_result["fix_lost"] == 1
        job = session.scalar(
            select(Job).where(Job.dedupe_key == f"fix_impact:{imp.id}", Job.status == "queued")
        )
        assert job is not None and job.kind == JOB_FIX_IMPACT
    assert session.get(Impact, live["impact"].id).status == "fixing"  # still running

    # lost a second time: failed with a reason, not re-queued again
    again = stale[0]["impact"]
    again.status = "fixing"
    again.ci_result = {**again.ci_result, "fix_started_at": "2020-01-01T00:00:00+00:00"}
    session.commit()
    other = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    _script_fix(ready["llm"], RENAME)
    handle_fix_impact(session, {"impact_id": str(other["impact"].id)})
    session.refresh(again)
    assert again.status == "failed" and "lost twice" in again.fix_reason
    assert again.resolved_at is not None


def test_deferral_is_bounded(session, f: Factory, ready, monkeypatch):
    import kintsua.fixer.run as runmod

    monkeypatch.setattr(ready["settings"], "fix_concurrency", 1)
    _live_fixing(f, ready["github"])
    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    impact_id = str(world["impact"].id)
    result = handle_fix_impact(
        session, {"impact_id": impact_id, "deferrals": runmod.MAX_DEFERRALS - 1}
    )
    assert result["deferred"] is True
    job = session.scalar(
        select(Job).where(Job.dedupe_key == f"fix_impact:{impact_id}", Job.status == "queued")
    )
    assert job.payload["deferrals"] == runmod.MAX_DEFERRALS
    job.status = "done"
    session.commit()
    result = handle_fix_impact(
        session, {"impact_id": impact_id, "deferrals": job.payload["deferrals"]}
    )
    impact = session.get(Impact, world["impact"].id)
    assert result["status"] == "failed" and impact.status == "failed"
    assert "concurrency limit" in impact.fix_reason
    assert (
        session.scalar(
            select(Job).where(Job.dedupe_key == f"fix_impact:{impact_id}", Job.status == "queued")
        )
        is None
    )


def test_record_ci_result_ignores_cancelled_and_action_required(session, f: Factory, fake_github):
    w = _pr_open_world(f, fake_github)
    for conclusion in ("cancelled", "action_required", "neutral", "skipped"):
        impact = record_ci_result(session, w["repo"], w["pr"].number, conclusion, {"name": "ci"})
        assert impact.status == "pr_open" and impact.fix_reason is None, conclusion
        assert impact.ci_result["conclusion"] == conclusion
    assert fake_github.repo(w["repo"].full_name).get_issue(w["issue_number"]).comments == []


def test_record_ci_result_recovers_when_the_failed_checks_pass(session, f: Factory, fake_github):
    w = _pr_open_world(f, fake_github)
    n = w["pr"].number
    impact = record_ci_result(session, w["repo"], n, "failure", {"name": "test"})
    assert impact.status == "failed" and impact.resolved_at is not None
    # a cancelled re-run does not clear the failure; another check's success does not either
    impact = record_ci_result(session, w["repo"], n, "cancelled", {"name": "test"})
    assert impact.status == "failed"
    impact = record_ci_result(session, w["repo"], n, "timed_out", {"name": "lint"})
    impact = record_ci_result(session, w["repo"], n, "success", {"name": "test"})
    assert (
        impact.status == "failed" and impact.ci_result["checks"]["test"]["conclusion"] == "success"
    )
    impact = record_ci_result(session, w["repo"], n, "success", {"name": "lint"})
    assert impact.status == "pr_open"
    assert impact.fix_reason is None and impact.resolved_at is None
    assert impact.ci_result["pushed_sha"] == "a" * 40
    comments = fake_github.repo(w["repo"].full_name).get_issue(w["issue_number"]).comments
    assert "could not fix this automatically" in comments[0].body
    assert "now passes" in comments[-1].body
    # a PR the customer closed stays closed whatever CI says afterwards
    record_ci_result(session, w["repo"], n, "failure", {"name": "test"})
    record_pr_outcome(session, w["repo"], n, False, "closed")
    impact = record_ci_result(session, w["repo"], n, "success", {"name": "test"})
    assert impact.status == "failed" and impact.pr_outcome == "closed"


def test_push_url_token_outlives_the_push(session, f: Factory, ready):
    from kintsua.fixer.run import PUSH_TOKEN_MIN_TTL_S

    world = f.seed_break(load_break("stripe_charge_source_renamed"), fake_github=ready["github"])
    _script_fix(ready["llm"], RENAME)
    assert handle_fix_impact(session, {"impact_id": str(world["impact"].id)})["status"] == "pr_open"
    ttls = [
        ttl for (_, name, ttl) in ready["github"].clone_requests if name == world["repo"].full_name
    ]
    assert PUSH_TOKEN_MIN_TTL_S in ttls
