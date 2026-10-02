"""The full ``index_repo`` flow on fixture repos: clone is replaced by a copy of the fixture
directory, GitHub by ``fake_github`` (needs Postgres)."""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select

from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Dependency,
    Impact,
    Job,
    Org,
    Repo,
    Snapshot,
    Source,
)
from kintsua.core.queue import HANDLERS, NonRetryable, drain
from kintsua.indexer import run as run_mod
from kintsua.indexer.clone import CloneError
from kintsua.indexer.run import (
    enqueue_index_repo,
    handle_index_repo,
    index_repo,
    known_tools_lookup,
)
from kintsua.indexer.schema import IndexBudgetExceeded

REPOS = Path(__file__).resolve().parents[1] / "fixtures" / "repos"
SHA1 = "a" * 40
SHA2 = "b" * 40


def make_org_repo(session, full_name="acme/billing-agent", installation_id=42, **repo_kw):
    org = Org(github_installation_id=installation_id, name=full_name.split("/")[0], settings={})
    session.add(org)
    session.flush()
    repo = Repo(
        org_id=org.id,
        github_repo_id=abs(hash(full_name)) % 1_000_000,
        full_name=full_name,
        default_branch="main",
        **repo_kw,
    )
    session.add(repo)
    session.commit()
    return org, repo


def fake_clone(fixture: str, sha: str, mutate: Callable[[Path], None] | None = None):
    def clone(url: str, branch: str, dest: Path, *, timeout_s: float = 0, depth: int = 1) -> str:
        assert (
            url.startswith("https://x-access-token:ghs_fake_")
            and url.endswith("/billing-agent.git")
            or "x-access-token" in url
        )
        assert branch == "main"
        shutil.copytree(REPOS / fixture, dest)
        if mutate:
            mutate(Path(dest))
        return sha

    return clone


def deps_of(session, repo) -> dict[tuple[str, str], Dependency]:
    session.expire_all()
    return {
        (d.kind, d.name): d
        for d in session.scalars(select(Dependency).where(Dependency.repo_id == repo.id))
    }


def sites_of(session, repo) -> list[CallSite]:
    return list(
        session.scalars(
            select(CallSite)
            .join(Dependency, Dependency.id == CallSite.dependency_id)
            .where(Dependency.repo_id == repo.id)
        )
    )


def test_first_index_writes_rows_sources_jobs_and_the_install_issue(session, fake_github):
    org, repo = make_org_repo(session)
    res = index_repo(
        session,
        repo,
        reason="install",
        clone=fake_clone("openai-agents-stripe", SHA1),
        allow_network=False,
    )
    assert res.head_sha == SHA1 and res.error is None
    # the stdio server ``uvx mcp-server-fetch`` is never run by the hosted worker: unwatched
    # (its package is watched through PyPI instead)
    assert (res.dependencies, res.call_sites, res.watched, res.unwatched) == (5, 6, 4, 1)
    assert res.files_parsed == 5 and res.parse_errors == 0
    assert res.install_issue_created and res.install_issue_number == 1
    assert (
        set(res.phases) == {"clone", "extract", "resolve", "write", "issue"} and res.duration_s > 0
    )

    deps = deps_of(session, repo)
    assert set(deps) == {
        ("pypi", "openai-agents"),
        ("pypi", "stripe"),
        ("pypi", "mcp-server-fetch"),
        ("mcp", "uvx mcp-server-fetch"),
        ("mcp", "https://mcp.stripe.com"),
    }
    stripe = deps[("pypi", "stripe")]
    assert stripe.first_seen_sha == SHA1 and stripe.version_constraint == ">=11,<13"
    assert stripe.meta["vendor"] == "stripe" and stripe.meta["config_file"] == "pyproject.toml"
    assert session.get(Source, stripe.source_id).kind == "pypi"
    assert (
        session.get(Source, __import__("uuid").UUID(stripe.meta["openapi_source_id"])).kind
        == "openapi"
    )
    mcp_src = session.get(Source, deps[("mcp", "https://mcp.stripe.com")].source_id)
    assert (
        mcp_src.kind == "mcp"
        and mcp_src.meta["transport"] == "http"
        and mcp_src.meta["header_keys"] == ["Authorization"]
    )
    sites = sites_of(session, repo)
    assert len(sites) == 6 and all(s.last_seen_sha == SHA1 for s in sites)
    test_site = next(s for s in sites if s.file_path == "tests/test_tools.py")
    assert test_site.args_shape == {"keys": ["limit"], "in_tests": True}

    assert (
        repo.last_indexed_sha == SHA1
        and repo.indexed_at is not None
        and repo.last_index_error is None
    )
    assert repo.install_issue_number == 1
    # trust rule 1: sdk_map-derived sources are seed, everything from the repo is customer
    origins = {(s.kind, s.locator): s.origin for s in session.scalars(select(Source))}
    assert origins == {
        ("pypi", "openai-agents"): "seed",
        ("changelog", "https://github.com/openai/openai-agents-python/releases"): "seed",
        ("pypi", "stripe"): "seed",
        ("openapi", "https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json"): (
            "seed"
        ),
        ("pypi", "mcp-server-fetch"): "customer",
        ("mcp", "uvx mcp-server-fetch"): "customer",
        ("mcp", "https://mcp.stripe.com"): "customer",
    }
    stdio = session.scalar(select(Source).where(Source.locator == "uvx mcp-server-fetch"))
    assert not stdio.active and "never runs a command line" in stdio.last_error
    # one poll_source job per new pollable source (never the stdio command), deduped like
    # the tick does it
    jobs = list(session.scalars(select(Job).where(Job.kind == "poll_source")))
    assert len(jobs) == res.new_sources == 6
    assert {j.dedupe_key for j in jobs} == {
        f"poll_source:{s.id}" for s in session.scalars(select(Source)) if s.id != stdio.id
    }

    issue = fake_github.repo("acme/billing-agent").issues[1]
    assert issue.title == "Kintsua is watching 4 of your 5 external dependencies"
    assert [lb.name for lb in issue.labels] == ["kintsua"]
    assert "| `stripe` (Stripe) | Python package | 🟢 watched (pypi + openapi) | 5 |" in issue.body
    assert (
        "| **stripe** `https://mcp.stripe.com` | MCP server | 🟢 watched (mcp + http) | 0 |"
        in issue.body
    )
    assert (
        "| `openai-agents` (OpenAI Agents SDK) | Python package | 🟢 watched (pypi + changelog) | 1 |"
        in issue.body
    )
    assert (
        "| **fetch** `uvx mcp-server-fetch` | MCP server | ⚪ unwatched — Kintsua does not run "
        "commands from a repo" in issue.body
    )
    assert f"at `{SHA1[:7]}`" in issue.body and "across 5 files" in issue.body
    assert "`DELETE /v1/repos/{id}/data`" in issue.body
    assert fake_github.tokens_issued  # the clone URL used an installation token


def test_reindex_updates_issue_prunes_rows_keeps_runtime_rows_and_dismisses_impacts(
    session, fake_github
):
    org, repo = make_org_repo(session)
    index_repo(
        session,
        repo,
        reason="install",
        clone=fake_clone("openai-agents-stripe", SHA1),
        allow_network=False,
    )
    deps = deps_of(session, repo)
    stripe_dep = deps[("pypi", "stripe")]

    # rows the runtime SDK added: must survive a re-index
    rt_dep = Dependency(
        repo_id=repo.id,
        kind="mcp",
        name="https://runtime.example/mcp",
        discovered_at_runtime=True,
        meta={"transport": "http"},
    )
    session.add(rt_dep)
    session.flush()
    session.add(
        CallSite(
            dependency_id=rt_dep.id,
            file_path="?",
            line=0,
            operation="get_forecast",
            discovered_at_runtime=True,
        )
    )
    session.add(
        CallSite(
            dependency_id=stripe_dep.id,
            file_path="?",
            line=0,
            operation="POST /v1/refunds",
            discovered_at_runtime=True,
        )
    )
    # two open impacts: one on a call site that is about to disappear, one that stays
    src = session.scalar(select(Source).where(Source.kind == "openapi"))
    ev1 = ChangeEventRow(source_id=src.id, diff={"ops": []})
    ev2 = ChangeEventRow(source_id=src.id, diff={"ops": []})
    session.add_all([ev1, ev2])
    session.flush()
    gh_repo = fake_github.repo("acme/billing-agent")
    issue_gone = gh_repo.create_issue(title="Refund.create renamed", body="...", labels=["kintsua"])
    issue_stays = gh_repo.create_issue(
        title="Customer.retrieve changed", body="...", labels=["kintsua"]
    )
    imp_gone = Impact(
        repo_id=repo.id,
        change_event_id=ev1.id,
        status="issued",
        github_issue_number=issue_gone.number,
        matched_call_sites=[
            {"file_path": "billing/tools.py", "line": 27, "operation": "stripe.Refund.create"}
        ],
    )
    imp_stays = Impact(
        repo_id=repo.id,
        change_event_id=ev2.id,
        status="pr_open",
        github_issue_number=issue_stays.number,
        matched_call_sites=[
            {"file_path": "billing/tools.py", "line": 32, "operation": "stripe.Customer.retrieve"},
            {"file_path": "billing/tools.py", "line": 27, "operation": "stripe.Refund.create"},
        ],
    )
    session.add_all([imp_gone, imp_stays])
    session.commit()

    def mutate(root: Path) -> None:
        tools = root / "billing" / "tools.py"
        text = tools.read_text()
        start = text.index("@function_tool\ndef refund")
        end = text.index("def customer_email")
        tools.write_text(text[:start] + text[end:])
        (root / "billing" / "agent.py").unlink()  # openai-agents import + MCP registrations gone
        (root / "pyproject.toml").write_text(
            '[project]\nname = "billing-agent"\ndependencies = ["stripe>=12"]\n'
        )

    res = index_repo(
        session,
        repo,
        reason="push",
        clone=fake_clone("openai-agents-stripe", SHA2, mutate),
        allow_network=False,
    )
    assert res.head_sha == SHA2 and not res.install_issue_created and res.install_issue_number == 1
    # tools.py still imports ``agents`` (function_tool): openai-agents stays, the MCP
    # registrations and the fetch server package go
    assert res.dismissed_impacts == 1 and res.deleted_dependencies == 3

    deps = deps_of(session, repo)
    assert set(deps) == {
        ("pypi", "stripe"),
        ("pypi", "openai-agents"),
        ("mcp", "https://runtime.example/mcp"),
    }
    assert deps[("pypi", "stripe")].id == stripe_dep.id  # upsert, not replace
    assert deps[("pypi", "stripe")].first_seen_sha == SHA1
    assert deps[("pypi", "stripe")].version_constraint == ">=12"
    sites = sites_of(session, repo)
    ops = sorted((s.operation, s.discovered_at_runtime, s.last_seen_sha) for s in sites)
    assert ops == [
        ("POST /v1/refunds", True, None),
        ("get_forecast", True, None),
        ("stripe.Charge.list", False, SHA2),
        ("stripe.Charge.list", False, SHA2),
        ("stripe.Customer.retrieve", False, SHA2),
        ("stripe.StripeClient.v1.payment_intents.create", False, SHA2),
    ]

    session.refresh(imp_gone)
    session.refresh(imp_stays)
    assert imp_gone.status == "dismissed" and imp_gone.resolved_at is not None
    assert imp_gone.fix_reason == f"call site removed in {SHA2}"
    assert imp_stays.status == "pr_open" and imp_stays.resolved_at is None
    gone = gh_repo.issues[issue_gone.number]
    assert gone.state == "closed" and len(gone.comments) == 1
    assert (
        "no longer exist on `main`" in gone.comments[0].body and SHA2[:12] in gone.comments[0].body
    )
    assert "`billing/tools.py` — `stripe.Refund.create`" in gone.comments[0].body
    assert (
        gh_repo.issues[issue_stays.number].state == "open"
        and not gh_repo.issues[issue_stays.number].comments
    )

    install = gh_repo.issues[1]
    assert (
        install.title == "Kintsua is watching 2 of your 3 external dependencies"
    )  # runtime one unwatched
    assert f"at `{SHA2[:7]}`" in install.body
    assert "uvx mcp-server-fetch" not in install.body and "mcp.stripe.com" not in install.body
    assert "seen at runtime" in install.body
    assert repo.last_indexed_sha == SHA2


def test_kintsua_yml_is_stored_and_errors_are_reported_in_the_issue(session, fake_github):
    org, repo = make_org_repo(session, config={"api_added": True})

    def good(root: Path) -> None:
        (root / ".kintsua.yml").write_text(
            "sources:\n  - kind: openapi\n    url: https://specs.example/weather.json\n    hosts: [api.weather.gov]\n"
            "mute:\n  - stripe:/v1/legacy/*\nfix:\n  mode: pr\n"
        )

    res = index_repo(
        session, repo, clone=fake_clone("raw-httpx-agent", SHA1, good), allow_network=False
    )
    assert res.config_error is None
    assert repo.config["fix"] == {"mode": "pr"} and repo.config["mute"] == ["stripe:/v1/legacy/*"]
    assert repo.config["api_added"] is True  # keys set through the API survive
    deps = deps_of(session, repo)
    spec_dep = deps[("openapi", "https://specs.example/weather.json")]
    assert deps[("http", "api.weather.gov")].source_id == spec_dep.source_id
    issue = fake_github.repo("acme/billing-agent").issues[1]
    assert "Fix mode for this repo is **`pr`**" in issue.body
    assert "| `api.weather.gov` | HTTP API | 🟢 watched (openapi) | 1 |" in issue.body
    assert (
        "| <https://specs.example/weather.json> | OpenAPI spec | 🟢 watched (openapi) | 0 |"
        in issue.body
    )

    def bad(root: Path) -> None:
        (root / ".kintsua.yml").write_text("fix:\n  mode: sometimes\n")

    res2 = index_repo(
        session, repo, clone=fake_clone("raw-httpx-agent", SHA2, bad), allow_network=False
    )
    assert res2.config_error and "fix.mode" in res2.config_error
    assert repo.config["fix"] == {"mode": "pr"}  # previous config kept
    issue = fake_github.repo("acme/billing-agent").issues[1]
    assert "## Configuration error" in issue.body and "fix.mode" in issue.body
    assert repo.last_index_error is None


def test_install_issue_is_recovered_when_the_number_is_lost(session, fake_github):
    org, repo = make_org_repo(session)
    index_repo(session, repo, clone=fake_clone("crewai-notion", SHA1), allow_network=False)
    assert repo.install_issue_number == 1
    repo.install_issue_number = None  # e.g. a crash after creating the issue
    session.commit()
    res = index_repo(session, repo, clone=fake_clone("crewai-notion", SHA2), allow_network=False)
    assert res.install_issue_number == 1 and not res.install_issue_created
    assert len(fake_github.repo("acme/billing-agent").issues) == 1


def test_known_tools_lookup_uses_the_latest_snapshot_without_network(session):
    src = Source(kind="mcp", locator="https://mcp.deepwiki.com/mcp", display_name="deepwiki")
    session.add(src)
    session.flush()
    session.add(
        Snapshot(
            source_id=src.id,
            content_hash="1",
            storage_ref={"kind": "inline", "body": {"tools": {"old_tool": {}}}},
        )
    )
    session.flush()
    time.sleep(0.01)
    session.add(
        Snapshot(
            source_id=src.id,
            content_hash="2",
            storage_ref={
                "kind": "inline",
                "body": {"tools": {"ask_question": {}, "read_wiki_structure": {}}},
            },
        )
    )
    session.flush()
    lookup = known_tools_lookup(session, allow_network=False)
    assert lookup("https://mcp.deepwiki.com/mcp") == ["ask_question", "read_wiki_structure"]
    assert lookup("https://nobody.example/mcp") is None


def test_tool_literals_become_call_sites_when_tools_are_known(session, fake_github):
    org, repo = make_org_repo(session)
    src = Source(
        kind="mcp",
        locator="https://mcp.deepwiki.com/mcp",
        display_name="deepwiki",
        meta={"transport": "http"},
    )
    session.add(src)
    session.flush()
    session.add(
        Snapshot(
            source_id=src.id,
            content_hash="2",
            storage_ref={
                "kind": "inline",
                "body": {"tools": {"ask_question": {}, "read_wiki_structure": {}}},
            },
        )
    )
    session.flush()

    def add_literal(root: Path) -> None:
        (root / "src" / "mcp.ts").write_text(
            (root / "src" / "mcp.ts").read_text()
            + '\nexport const TOOLS = ["read_wiki_structure"];\n'
        )

    res = index_repo(
        session, repo, clone=fake_clone("ts-agent", SHA1, add_literal), allow_network=False
    )
    deps = deps_of(session, repo)
    dw = deps[("mcp", "https://mcp.deepwiki.com/mcp")]
    assert dw.source_id == src.id and dw.meta["tool_names"] == [
        "ask_question",
        "read_wiki_structure",
    ]
    tool_sites = sorted(
        (s.operation, s.symbol) for s in sites_of(session, repo) if s.dependency_id == dw.id
    )
    assert tool_sites == [("ask_question", "call_tool"), ("read_wiki_structure", "literal")]
    assert res.call_sites == 13


def test_budget_exhaustion_records_the_error_and_raises(session, fake_github):
    org, repo = make_org_repo(session)

    def slow_clone(url, branch, dest, *, timeout_s=0, depth=1):
        shutil.copytree(REPOS / "crewai-notion", dest)
        time.sleep(0.05)
        return SHA1

    with pytest.raises(IndexBudgetExceeded):
        index_repo(session, repo, clone=slow_clone, allow_network=False, budget_s=0.01)
    session.refresh(repo)
    assert repo.last_index_error.startswith("index budget exceeded")
    assert repo.last_indexed_sha is None
    assert not fake_github.repo("acme/billing-agent").issues


def test_permanent_clone_failure_is_non_retryable(session, fake_github):
    org, repo = make_org_repo(session)

    def dead_clone(url, branch, dest, *, timeout_s=0, depth=1):
        raise CloneError("git clone failed (exit 128): remote: Repository not found.")

    with pytest.raises(NonRetryable):
        index_repo(session, repo, clone=dead_clone, allow_network=False)
    session.refresh(repo)
    assert "Repository not found" in repo.last_index_error

    def flaky_clone(url, branch, dest, *, timeout_s=0, depth=1):
        raise CloneError("git clone timed out after 30s")

    with pytest.raises(CloneError):
        index_repo(session, repo, clone=flaky_clone, allow_network=False)


def test_job_handler_and_queue_round_trip(session, fake_github, monkeypatch):
    assert HANDLERS["index_repo"] is handle_index_repo
    with pytest.raises(NonRetryable):
        handle_index_repo(session, {"repo_id": "not-a-uuid"})
    with pytest.raises(NonRetryable):
        handle_index_repo(
            session, {"repo_id": "00000000-0000-0000-0000-000000000000", "reason": "push"}
        )
    org, repo = make_org_repo(session)
    repo.active = False
    session.flush()
    with pytest.raises(NonRetryable):
        handle_index_repo(session, {"repo_id": str(repo.id)})
    repo.active = True
    session.commit()

    monkeypatch.setattr(run_mod, "clone_repo", fake_clone("crewai-notion", SHA1))
    enqueue_index_repo(session, repo, "push", debounce_s=300)
    enqueue_index_repo(session, repo, "push", debounce_s=300)  # burst: still one job
    jobs = list(session.scalars(select(Job).where(Job.kind == "index_repo")))
    assert len(jobs) == 1 and jobs[0].dedupe_key == f"index_repo:{repo.id}"
    assert jobs[0].payload == {"repo_id": str(repo.id), "reason": "push"}
    jobs[0].run_after = jobs[0].created_at  # make it due now
    session.commit()

    from kintsua.core.db import new_session

    assert drain(new_session, ["index_repo"]) == 1
    session.expire_all()
    job = session.get(Job, jobs[0].id)
    assert job.status == "done", job.last_error
    assert repo.last_indexed_sha == SHA1 and repo.install_issue_number == 1
    assert len(deps_of(session, repo)) == 4
    title = fake_github.repo("acme/billing-agent").issues[1].title
    assert re.fullmatch(r"Kintsua is watching \d+ of your 4 external dependencies", title)
