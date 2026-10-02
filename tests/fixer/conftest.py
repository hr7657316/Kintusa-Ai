"""Fixtures for the fixer tests: a git repo built from the fixture agent repo, a prepared
``LocalSandbox``, the recorded breaks, and row factories for the handler tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Classification,
    Dependency,
    Impact,
    Org,
    Repo,
    Snapshot,
    Source,
)
from kintsua.core.util import utcnow
from kintsua.fixer.agent import CallSiteContext, FixContext, read_context
from kintsua.fixer.sandbox import LocalSandbox, Sandbox
from kintsua.fixer.testcmd import TestPlan, detect_test_command

FIXER_FIXTURES = Path(__file__).parent.parent / "fixtures" / "fixer"
AGENT_REPO = FIXER_FIXTURES / "agent-repo"
BREAKS_DIR = FIXER_FIXTURES / "breaks"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C.UTF-8",
}


def git(*args: str, cwd: str | Path) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_ENV)
    env.setdefault("HOME", str(cwd))
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout


def make_git_repo(src: Path, dest: Path, *, branch: str = "main") -> str:
    """Copy ``src`` into ``dest``, ``git init`` it on ``branch`` and commit; returns the sha."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    git("init", "-q", "-b", branch, cwd=dest)
    git("add", "-A", cwd=dest)
    git("commit", "-q", "-m", "fixture: initial import", cwd=dest)
    return git("rev-parse", "HEAD", cwd=dest).strip()


@pytest.fixture(scope="session")
def fixture_git_repo(tmp_path_factory) -> Path:
    """The fixture agent repo as a real git repository (cloned with ``file://``)."""
    dest = tmp_path_factory.mktemp("fixer-origin") / "agent-repo"
    make_git_repo(AGENT_REPO, dest)
    return dest


def clone_url_for(path: Path) -> str:
    return f"file://{path}"


@pytest.fixture
def sandbox(fixture_git_repo: Path, settings, tmp_path: Path):
    """A ``LocalSandbox`` prepared from the fixture git repo through a real ``git clone``."""
    sb = LocalSandbox(settings=settings, root=tmp_path / "sandbox", wall_clock_s=600)
    sb.prepare(clone_url_for(fixture_git_repo), "main")
    try:
        yield sb
    finally:
        sb.cleanup()


@pytest.fixture
def data_dir(settings, monkeypatch, tmp_path: Path) -> Path:
    """Point ``settings.data_dir`` (baseline cache) at a per-test directory."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d


def install_fixture_clone(monkeypatch, src: Path = AGENT_REPO) -> None:
    """Make ``LocalSandbox._clone`` copy ``src`` (as a fresh git repo) instead of cloning
    the (fake) GitHub URL the handler builds."""

    def fake_clone(self: LocalSandbox, clone_url: str, branch: str, dest: str, sha: str | None):
        make_git_repo(src, Path(dest), branch=branch or "main")
        git("remote", "add", "origin", clone_url, cwd=dest)

    monkeypatch.setattr(LocalSandbox, "_clone", fake_clone)


@pytest.fixture
def fixture_clone(monkeypatch):
    install_fixture_clone(monkeypatch)


@pytest.fixture
def no_push(monkeypatch) -> list[dict[str, Any]]:
    """Replace the networked git steps of ``kintsua.fixer.pr``; returns the recorded pushes."""
    import kintsua.fixer.pr as prmod

    pushes: list[dict[str, Any]] = []
    monkeypatch.setattr(prmod, "remote_branch_exists", lambda sandbox, url, branch: False)
    monkeypatch.setattr(
        prmod,
        "push_branch",
        lambda sandbox, url, branch: pushes.append(
            {"branch": branch, "sha": sandbox.head_sha(), "url": url}
        ),
    )
    return pushes


@pytest.fixture
def no_install(monkeypatch):
    import kintsua.fixer.run as runmod

    monkeypatch.setattr(runmod, "run_install", lambda sandbox, plan: None)


# --------------------------------------------------------------------------------------
# Recorded breaks
# --------------------------------------------------------------------------------------


def load_break(name: str) -> dict[str, Any]:
    path = BREAKS_DIR / (name if name.endswith(".json") else f"{name}.json")
    return json.loads(path.read_text("utf-8"))


def all_breaks() -> list[dict[str, Any]]:
    return [json.loads(p.read_text("utf-8")) for p in sorted(BREAKS_DIR.glob("*.json"))]


def context_from_break(
    brk: dict[str, Any], sandbox: Sandbox, plan: TestPlan | None = None
) -> FixContext:
    plan = plan or detect_test_command(sandbox.workdir or ".", {})
    sites = [
        CallSiteContext(
            file_path=s["file_path"],
            line=int(s["line"]),
            operation=s["operation"],
            symbol=s.get("symbol"),
            args_shape=dict(s.get("args_shape") or {}),
            context=read_context(sandbox, s["file_path"], int(s["line"])),
        )
        for s in brk["call_sites"]
    ]
    cls = brk["classification"]
    ev = brk["event"]
    return FixContext(
        repo_full_name="acme/weather-billing-agent",
        default_branch="main",
        vendor=brk["source"].get("vendor") or brk["source"]["display_name"],
        source_kind=ev["source_kind"],
        severity=cls["severity"],
        category=cls["category"],
        summary=cls["summary"],
        migration_hint=cls.get("migration_hint"),
        affected_paths=list(cls.get("affected_paths") or []),
        ops=list(ev.get("ops") or []),
        vendor_text=ev.get("text"),
        call_sites=sites,
        test_plan=plan,
        source_display=brk["source"]["display_name"],
        from_version=ev.get("from_version"),
        to_version=ev.get("to_version"),
        detected_at=utcnow(),
        new_input_schema=brk.get("new_input_schema"),
        tool_name=brk.get("tool_name"),
        issue_number=7,
        change_event_id=str(uuid.uuid4()),
        base_sha=sandbox.base_sha,
    )


# --------------------------------------------------------------------------------------
# Rows for the handler tests
# --------------------------------------------------------------------------------------


class Factory:
    def __init__(self, session: Session):
        self.session = session

    def org(self, **settings: Any) -> Org:
        org = Org(
            github_installation_id=uuid.uuid4().int % 10**9,
            name="acme",
            settings=dict(settings),
        )
        self.session.add(org)
        self.session.flush()
        return org

    def repo(
        self,
        org: Org,
        full_name: str = "acme/weather-billing-agent",
        *,
        config: dict[str, Any] | None = None,
    ) -> Repo:
        repo = Repo(
            org_id=org.id,
            github_repo_id=uuid.uuid4().int % 10**9,
            full_name=full_name,
            default_branch="main",
            config=dict(config or {}),
        )
        self.session.add(repo)
        self.session.flush()
        return repo

    def seed_break(
        self,
        brk: dict[str, Any],
        *,
        fake_github,
        org_settings: dict[str, Any] | None = None,
        repo_config: dict[str, Any] | None = None,
        status: str = "issued",
        with_issue: bool = True,
        classification_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """org + repo + source + dependency + call sites + change event + classification +
        impact (+ a fake GitHub issue) for a recorded break."""
        org = self.org(**(org_settings if org_settings is not None else {"fix_mode": "pr"}))
        repo = self.repo(org, config=repo_config)
        src_spec = brk["source"]
        source = self.session.scalar(
            select(Source).where(
                Source.kind == src_spec["kind"], Source.locator == src_spec["locator"]
            )
        )
        if source is None:
            source = Source(
                kind=src_spec["kind"],
                locator=src_spec["locator"],
                display_name=src_spec["display_name"],
                vendor=src_spec.get("vendor"),
            )
            self.session.add(source)
            self.session.flush()
        dep_spec = brk.get("dependency") or {"kind": "pypi", "name": "stripe", "meta": {}}
        dep = Dependency(
            repo_id=repo.id,
            source_id=source.id,
            kind=dep_spec["kind"],
            name=dep_spec["name"],
            meta=dict(dep_spec.get("meta") or {}),
        )
        self.session.add(dep)
        self.session.flush()
        sites: list[CallSite] = []
        for s in brk["call_sites"]:
            cs = CallSite(
                dependency_id=dep.id,
                file_path=s["file_path"],
                line=int(s["line"]),
                symbol=s.get("symbol"),
                operation=s["operation"],
                args_shape=dict(s.get("args_shape") or {}),
            )
            self.session.add(cs)
            sites.append(cs)
        self.session.flush()
        to_snapshot = None
        if brk.get("to_snapshot_body"):
            to_snapshot = Snapshot(
                source_id=source.id,
                content_hash="f" * 64,
                version=brk["event"].get("to_version"),
                storage_ref={"kind": "inline", "body": brk["to_snapshot_body"]},
            )
            self.session.add(to_snapshot)
            self.session.flush()
        ev = brk["event"]
        row = ChangeEventRow(
            source_id=source.id,
            to_snapshot_id=to_snapshot.id if to_snapshot is not None else None,
            diff={
                "source_kind": ev["source_kind"],
                "from_version": ev.get("from_version"),
                "to_version": ev.get("to_version"),
                "ops": list(ev.get("ops") or []),
                "raw_diff_ref": "inline",
                "text": ev.get("text"),
            },
            detected_at=utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        cls_spec = {**brk["classification"], **(classification_overrides or {})}
        cls = Classification(
            change_event_id=row.id,
            severity=cls_spec["severity"],
            category=cls_spec["category"],
            summary=cls_spec["summary"],
            affected_paths=list(cls_spec.get("affected_paths") or []),
            migration_hint=cls_spec.get("migration_hint"),
            rule_hits=list(cls_spec.get("rule_hits") or []),
        )
        self.session.add(cls)
        self.session.flush()
        issue_number = None
        if with_issue:
            gh_repo = fake_github.repo(repo.full_name)
            issue = gh_repo.create_issue(
                title=f"[Kintsua] {cls.summary}", body="issue", labels=["kintsua"]
            )
            issue_number = issue.number
        impact = Impact(
            repo_id=repo.id,
            change_event_id=row.id,
            classification_id=cls.id,
            status=status,
            github_issue_number=issue_number,
            matched_call_sites=[
                {
                    "call_site_id": str(cs.id),
                    "file_path": cs.file_path,
                    "line": cs.line,
                    "symbol": cs.symbol,
                    "operation": cs.operation,
                    "dependency_id": str(dep.id),
                    "dependency": dep.name,
                    "vendor": source.vendor,
                }
                for cs in sites
            ],
        )
        self.session.add(impact)
        self.session.commit()
        return {
            "org": org,
            "repo": repo,
            "source": source,
            "dep": dep,
            "sites": sites,
            "event": row,
            "cls": cls,
            "impact": impact,
            "issue_number": issue_number,
        }


@pytest.fixture
def f(session: Session) -> Factory:
    return Factory(session)
