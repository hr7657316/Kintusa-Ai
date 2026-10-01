"""``index_repo``: clone -> ``.kintsua.yml`` -> three extractors -> resolve -> write rows ->
dismiss impacts whose call sites vanished -> install issue -> enqueue polls.

Registered as the ``index_repo`` job handler (payload ``{"repo_id", "reason"}``). The whole
run is bounded by ``Settings.index_budget_s`` (10 minutes) with checks between phases and
inside the extractors; the clone lives in a temp directory that is always removed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from kintsua.core.config import get_settings
from kintsua.core.github import LABEL, get_github_app
from kintsua.core.models import CallSite, Dependency, Impact, Repo, Snapshot, Source
from kintsua.core.queue import (
    JOB_INDEX_REPO,
    JOB_POLL_SOURCE,
    NonRetryable,
    enqueue,
    job_handler,
    reschedule_queued,
)
from kintsua.core.storage import get_document
from kintsua.core.util import utcnow
from kintsua.indexer import mcp_config, python_extract, ts_extract
from kintsua.indexer.clone import CloneError, clone_repo, read_repo_file, temp_clone_dir
from kintsua.indexer.config import parse_kintsua_yml
from kintsua.indexer.depmap import DependencyMap, dependency_map, render_install_issue
from kintsua.indexer.resolve import resolve_dependencies
from kintsua.indexer.schema import (
    CallSiteInfo,
    DependencyInfo,
    ExtractResult,
    IndexBudgetExceeded,
)

log = logging.getLogger(__name__)

CONFIG_FILE_NAMES = (".kintsua.yml", ".kintsua.yaml", "kintsua.yml")
CONFIG_KEYS = ("sources", "mute", "fix", "test_command")
OPEN_IMPACT_STATUSES = ("detected", "issued", "fixing", "pr_open")
INSTALL_ISSUE_TITLE_PREFIX = "Kintsua is watching "
TOOLS_FETCH_TIMEOUT_S = 30.0
_PERMANENT_CLONE_ERRORS = (
    "repository not found",
    "could not read username",
    "authentication failed",
    "invalid username or password",
    "remote: repository not found",
    "does not appear to be a git repository",
)

CloneFn = Callable[..., str]


@dataclass
class IndexResult:
    repo_id: UUID
    reason: str = "manual"
    head_sha: str | None = None
    dependencies: int = 0
    call_sites: int = 0
    watched: int = 0
    unwatched: int = 0
    files_parsed: int = 0
    parse_errors: int = 0
    new_sources: int = 0
    dismissed_impacts: int = 0
    deleted_dependencies: int = 0
    install_issue_number: int | None = None
    install_issue_created: bool = False
    config_error: str | None = None
    error: str | None = None
    duration_s: float = 0.0
    phases: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_id": str(self.repo_id),
            "reason": self.reason,
            "head_sha": self.head_sha,
            "dependencies": self.dependencies,
            "call_sites": self.call_sites,
            "watched": self.watched,
            "unwatched": self.unwatched,
            "files_parsed": self.files_parsed,
            "parse_errors": self.parse_errors,
            "new_sources": self.new_sources,
            "dismissed_impacts": self.dismissed_impacts,
            "install_issue_number": self.install_issue_number,
            "config_error": self.config_error,
            "duration_s": round(self.duration_s, 2),
            "phases": {k: round(v, 2) for k, v in self.phases.items()},
        }


class Budget:
    """Wall-clock budget for one index run; ``check`` is called between phases."""

    def __init__(self, total_s: float):
        self.total_s = float(total_s)
        self.start = time.monotonic()
        self.deadline = self.start + self.total_s

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check(self, phase: str) -> None:
        if time.monotonic() > self.deadline:
            raise IndexBudgetExceeded(
                f"{phase}: {self.total_s:.0f}s budget exhausted after {self.elapsed():.0f}s"
            )


# --------------------------------------------------------------------------------------
# Known MCP tool names (for tool-literal call sites)
# --------------------------------------------------------------------------------------


def _tool_names_from_body(body: Any) -> list[str] | None:
    if not isinstance(body, dict):
        return None
    tools = body.get("tools")
    if isinstance(tools, dict):
        return sorted(str(k) for k in tools)
    if isinstance(tools, list):
        return sorted(str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name"))
    return None


def known_tools_lookup(
    session: Session, *, allow_network: bool, budget: Budget | None = None
) -> Callable[[str], list[str] | None]:
    """``tools_for_locator`` for the MCP extractor: the source's latest snapshot first, the
    live server (``kintsua.watchers.mcp.fetch_tools``) only when network is allowed."""
    cache: dict[str, list[str] | None] = {}

    def lookup(locator: str) -> list[str] | None:
        if locator in cache:
            return cache[locator]
        names: list[str] | None = None
        src = session.scalar(select(Source).where(Source.kind == "mcp", Source.locator == locator))
        if src is not None:
            snap = session.scalar(
                select(Snapshot)
                .where(Snapshot.source_id == src.id)
                .order_by(Snapshot.captured_at.desc())
                .limit(1)
            )
            if snap is not None:
                try:
                    names = _tool_names_from_body(get_document(snap.storage_ref))
                except Exception as e:  # noqa: BLE001
                    log.info("snapshot for %s unreadable: %s", locator, e)
        remaining = budget.remaining() if budget is not None else TOOLS_FETCH_TIMEOUT_S * 2
        if names is None and allow_network and remaining > 2 * TOOLS_FETCH_TIMEOUT_S:
            names = _fetch_tool_names(locator, dict(src.meta) if src is not None else None)
        cache[locator] = names
        return names

    return lookup


def _fetch_tool_names(locator: str, meta: dict[str, Any] | None) -> list[str] | None:
    try:
        from kintsua.watchers.mcp import fetch_tools
    except ImportError:
        return None
    meta = dict(meta or {})
    if not locator.startswith(("http://", "https://")):
        meta.setdefault("transport", "stdio")
        meta.setdefault("quiet", True)
    meta.setdefault("timeout_s", TOOLS_FETCH_TIMEOUT_S)

    async def go() -> list[dict[str, Any]]:
        return await asyncio.wait_for(fetch_tools(locator, meta), timeout=TOOLS_FETCH_TIMEOUT_S)

    try:
        tools = asyncio.run(go())
    except Exception as e:  # noqa: BLE001 - unreachable servers are expected
        log.info("fetch_tools(%s) failed: %s: %s", locator, type(e).__name__, e)
        return None
    return [str(t["name"]) for t in tools if isinstance(t, dict) and t.get("name")]


# --------------------------------------------------------------------------------------
# Writing rows
# --------------------------------------------------------------------------------------


@dataclass
class WriteResult:
    dependency_rows: dict[tuple[str, str], Dependency]
    old_sites: set[tuple[str, str]]
    new_sites: set[tuple[str, str]]
    inserted_call_sites: int = 0
    deleted_dependencies: int = 0


def _jsonable_meta(dep: DependencyInfo) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for k, v in dep.meta.items():
        if v is None:
            continue
        meta[k] = str(v) if isinstance(v, UUID) else v
    if dep.vendor:
        meta["vendor"] = dep.vendor
    if dep.display_name:
        meta["display_name"] = dep.display_name
    return meta


def write_index(
    session: Session,
    repo: Repo,
    head_sha: str,
    deps: list[DependencyInfo],
    call_sites: list[CallSiteInfo],
) -> WriteResult:
    """Upsert ``dependency`` by (repo, kind, name) keeping ``first_seen_sha``; replace the
    indexer's ``call_site`` rows (runtime-discovered rows are kept); delete dependencies no
    longer found unless they were discovered at runtime."""
    existing = {
        (d.kind, d.name): d
        for d in session.scalars(select(Dependency).where(Dependency.repo_id == repo.id))
    }
    old_sites = {
        (fp, op)
        for fp, op in session.execute(
            select(CallSite.file_path, CallSite.operation)
            .join(Dependency, Dependency.id == CallSite.dependency_id)
            .where(Dependency.repo_id == repo.id)
        ).all()
    }
    rows: dict[tuple[str, str], Dependency] = {}
    for dep in deps:
        key = dep.key
        meta = _jsonable_meta(dep)
        row = existing.get(key)
        if row is None:
            row = Dependency(
                repo_id=repo.id,
                source_id=dep.source_id,
                kind=dep.kind,
                name=dep.name,
                version_constraint=dep.version_constraint,
                first_seen_sha=head_sha,
                discovered_at_runtime=False,
                meta=meta,
            )
            session.add(row)
        else:
            row.source_id = dep.source_id
            row.version_constraint = dep.version_constraint
            row.first_seen_sha = row.first_seen_sha or head_sha
            row.discovered_at_runtime = False  # the indexer sees it now
            kept = {k: v for k, v in (row.meta or {}).items() if k.startswith("runtime_")}
            row.meta = {**kept, **meta}
        rows[key] = row
    session.flush()

    deleted = 0
    for key, row in existing.items():
        if key in rows or row.discovered_at_runtime:
            continue
        session.execute(delete(CallSite).where(CallSite.dependency_id == row.id))
        session.execute(delete(Dependency).where(Dependency.id == row.id))
        session.expunge(row)
        deleted += 1

    dep_ids = [row.id for row in rows.values()]
    if dep_ids:
        session.execute(
            delete(CallSite).where(
                CallSite.dependency_id.in_(dep_ids), CallSite.discovered_at_runtime.is_(False)
            )
        )
    inserted = 0
    seen: set[tuple[Any, str, int, str, str | None]] = set()
    for cs in call_sites:
        row = rows.get(cs.dependency_key)
        if row is None:
            continue
        dedupe = (row.id, cs.file_path, cs.line, cs.operation, cs.symbol)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        shape = dict(cs.args_shape)
        if cs.in_tests:
            shape["in_tests"] = True
        session.add(
            CallSite(
                dependency_id=row.id,
                file_path=cs.file_path,
                line=cs.line,
                symbol=cs.symbol,
                operation=cs.operation,
                args_shape=shape,
                discovered_at_runtime=False,
                last_seen_sha=head_sha,
            )
        )
        inserted += 1
    session.flush()
    new_sites = {
        (fp, op)
        for fp, op in session.execute(
            select(CallSite.file_path, CallSite.operation)
            .join(Dependency, Dependency.id == CallSite.dependency_id)
            .where(Dependency.repo_id == repo.id)
        ).all()
    }
    return WriteResult(
        dependency_rows=rows,
        old_sites=old_sites,
        new_sites=new_sites,
        inserted_call_sites=inserted,
        deleted_dependencies=deleted,
    )


# --------------------------------------------------------------------------------------
# Impacts whose call sites vanished
# --------------------------------------------------------------------------------------


def _impact_pairs(impact: Impact) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for m in impact.matched_call_sites or []:
        if not isinstance(m, dict):
            continue
        fp, op = m.get("file_path"), m.get("operation")
        if fp and op:
            pairs.append((str(fp), str(op)))
    return pairs


def dismiss_vanished_impacts(
    session: Session, repo: Repo, head_sha: str, new_sites: set[tuple[str, str]]
) -> list[Impact]:
    """Open impacts (detected / issued / fixing / pr_open) none of whose matched call sites
    (by file_path + operation) exist any more are dismissed; their GitHub issue gets a
    comment saying why and is closed."""
    impacts = list(
        session.scalars(
            select(Impact).where(Impact.repo_id == repo.id, Impact.status.in_(OPEN_IMPACT_STATUSES))
        )
    )
    dismissed: list[Impact] = []
    now = utcnow()
    for imp in impacts:
        pairs = _impact_pairs(imp)
        if not pairs or any(p in new_sites for p in pairs):
            continue
        imp.status = "dismissed"
        imp.resolved_at = now
        imp.fix_reason = f"call site removed in {head_sha}"
        dismissed.append(imp)
    session.flush()
    for imp in dismissed:
        if imp.github_issue_number:
            _comment_dismissed(repo, imp, head_sha)
    return dismissed


def _comment_dismissed(repo: Repo, impact: Impact, head_sha: str) -> None:
    pairs = _impact_pairs(impact)
    listed = "\n".join(f"- `{fp}` — `{op}`" for fp, op in pairs[:20])
    body = (
        "Kintsua dismissed this: the call site(s) it referred to no longer exist on "
        f"`{repo.default_branch}` as of `{head_sha[:12]}`:\n\n{listed}\n\n"
        "Closing the issue. Reopen it if the dependency is still in use somewhere the indexer "
        "could not see (dynamic tool names, URLs from config)."
    )
    app = get_github_app()
    inst = repo.org.github_installation_id
    try:
        with app.write_lock(inst):
            gh_repo = app.installation_client(inst).get_repo(repo.full_name)
            issue = gh_repo.get_issue(int(impact.github_issue_number or 0))
            issue.create_comment(body)
            issue.edit(state="closed")
    except Exception as e:  # noqa: BLE001 - GitHub trouble must not fail the index
        log.warning(
            "could not comment on issue #%s for impact %s: %s",
            impact.github_issue_number,
            impact.id,
            e,
        )


# --------------------------------------------------------------------------------------
# Install issue
# --------------------------------------------------------------------------------------


def _ensure_label(gh_repo: Any) -> None:
    try:
        gh_repo.get_label(LABEL)
    except Exception:  # noqa: BLE001 - UnknownObjectException / KeyError in the fake
        try:
            gh_repo.create_label(name=LABEL, color="5319e7", description="Opened by Kintsua")
        except Exception as e:  # noqa: BLE001
            log.info("could not create label %s: %s", LABEL, e)


def upsert_install_issue(session: Session, repo: Repo, depmap: DependencyMap) -> tuple[int, bool]:
    """Create the install issue on first index, edit it on later ones. Returns
    (issue_number, created)."""
    title, body = render_install_issue(repo, depmap)
    app = get_github_app()
    inst = repo.org.github_installation_id
    with app.write_lock(inst):
        gh_repo = app.installation_client(inst).get_repo(repo.full_name)
        if repo.install_issue_number:
            try:
                issue = gh_repo.get_issue(int(repo.install_issue_number))
                issue.edit(title=title, body=body)
                return int(repo.install_issue_number), False
            except Exception as e:  # noqa: BLE001 - deleted / transferred issue
                log.warning(
                    "install issue #%s on %s unavailable (%s); creating a new one",
                    repo.install_issue_number,
                    repo.full_name,
                    e,
                )
        # a retry after a crash must not open a second issue
        try:
            for issue in gh_repo.get_issues(state="open", labels=[LABEL]):
                if str(getattr(issue, "title", "")).startswith(INSTALL_ISSUE_TITLE_PREFIX):
                    issue.edit(title=title, body=body)
                    repo.install_issue_number = int(issue.number)
                    session.flush()
                    return int(issue.number), False
        except Exception as e:  # noqa: BLE001
            log.info("listing issues on %s failed: %s", repo.full_name, e)
        _ensure_label(gh_repo)
        issue = gh_repo.create_issue(title=title, body=body, labels=[LABEL])
        repo.install_issue_number = int(issue.number)
        session.flush()
        return int(issue.number), True


# --------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------


def _record_error(session: Session, repo_id: UUID, message: str) -> None:
    try:
        session.rollback()
        repo = session.get(Repo, repo_id)
        if repo is not None:
            repo.last_index_error = message[:4000]
            session.commit()
    except Exception:  # noqa: BLE001
        log.exception("could not record index error for repo %s", repo_id)


def _merge_config(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """The file defines the spec keys; anything else on Repo.config (set through the API)
    survives a re-index."""
    merged = {k: v for k, v in (old or {}).items() if k not in CONFIG_KEYS}
    merged.update(new)
    return merged


def extract_tree(
    root: Any,
    *,
    tools_for_locator: Callable[[str], list[str] | None] | None = None,
    deadline: float | None = None,
) -> ExtractResult:
    """Run the three extractors over a checked-out tree and merge their results."""
    py = python_extract.extract_python(root, deadline=deadline)
    ts = ts_extract.extract_ts(root, deadline=deadline)
    mcp = mcp_config.extract_mcp(root, tools_for_locator=tools_for_locator, deadline=deadline)
    return py.merge(ts).merge(mcp)


def index_repo(
    session: Session,
    repo: Repo,
    *,
    reason: str = "manual",
    clone: CloneFn | None = None,
    allow_network: bool | None = None,
    budget_s: float | None = None,
) -> IndexResult:
    """Index one repo end to end. Raises on failure after recording ``last_index_error``."""
    settings = get_settings()
    budget = Budget(budget_s or settings.index_budget_s)
    clone = clone or clone_repo  # resolved at call time so tests can monkeypatch the module
    if allow_network is None:
        allow_network = settings.env != "test"
    result = IndexResult(repo_id=repo.id, reason=reason)
    repo_id = repo.id
    org = repo.org
    if org is None:
        raise NonRetryable(f"repo {repo.full_name} has no org")
    log.info("index %s (%s) start", repo.full_name, reason)
    try:
        t = time.monotonic()
        with temp_clone_dir() as tmp:
            dest = tmp / "repo"
            clone_url = get_github_app().clone_url(org.github_installation_id, repo.full_name)
            head = clone(
                clone_url,
                repo.default_branch,
                dest,
                timeout_s=max(30.0, min(budget.remaining() - 60.0, budget.total_s * 0.6)),
            )
            result.head_sha = head
            result.phases["clone"] = time.monotonic() - t
            budget.check("config")

            t = time.monotonic()
            found = read_repo_file(dest, *CONFIG_FILE_NAMES)
            if found is not None:
                name, text = found
                try:
                    repo.config = _merge_config(repo.config, parse_kintsua_yml(text))
                except ValueError as e:
                    result.config_error = f"{name}: {e}"
                    log.info("%s: bad %s kept previous config: %s", repo.full_name, name, e)
            budget.check("extract")
            extracted = extract_tree(
                dest,
                tools_for_locator=known_tools_lookup(
                    session, allow_network=allow_network, budget=budget
                ),
                deadline=budget.deadline,
            )
            result.phases["extract"] = time.monotonic() - t
        budget.check("resolve")

        t = time.monotonic()
        resolved = resolve_dependencies(session, extracted.dependencies, repo_config=repo.config)
        result.phases["resolve"] = time.monotonic() - t
        budget.check("write")

        t = time.monotonic()
        written = write_index(session, repo, head, resolved.dependencies, extracted.call_sites)
        dismissed = dismiss_vanished_impacts(session, repo, head, written.new_sites)
        repo.last_indexed_sha = head
        repo.indexed_at = utcnow()
        repo.last_index_error = None
        repo.index_stats = {
            "files_parsed": extracted.stats.files_parsed,
            "parse_errors": extracted.stats.parse_errors,
            "head_sha": head,
            "reason": reason,
            "indexed_at": repo.indexed_at.isoformat(),
        }
        for sid in resolved.new_source_ids:
            enqueue(
                session, JOB_POLL_SOURCE, {"source_id": str(sid)}, dedupe_key=f"poll_source:{sid}"
            )
        session.commit()
        result.phases["write"] = time.monotonic() - t

        t = time.monotonic()
        depmap = dependency_map(
            session, repo, stats=extracted.stats, config_error=result.config_error
        )
        number, created = upsert_install_issue(session, repo, depmap)
        session.commit()
        result.phases["issue"] = time.monotonic() - t

        result.dependencies = depmap.totals.dependencies
        result.call_sites = depmap.totals.call_sites
        result.watched = depmap.totals.watched
        result.unwatched = depmap.totals.unwatched
        result.files_parsed = extracted.stats.files_parsed
        result.parse_errors = extracted.stats.parse_errors
        result.new_sources = len(resolved.new_source_ids)
        result.dismissed_impacts = len(dismissed)
        result.deleted_dependencies = written.deleted_dependencies
        result.install_issue_number = number
        result.install_issue_created = created
    except IndexBudgetExceeded as e:
        result.error = f"index budget exceeded: {e}"
        _record_error(session, repo_id, result.error)
        raise
    except CloneError as e:
        result.error = f"clone failed: {e}"
        _record_error(session, repo_id, result.error)
        if any(s in str(e).lower() for s in _PERMANENT_CLONE_ERRORS):
            raise NonRetryable(result.error) from e
        raise
    except NonRetryable as e:
        result.error = str(e)
        _record_error(session, repo_id, result.error)
        raise
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        _record_error(session, repo_id, result.error)
        raise
    finally:
        result.duration_s = budget.elapsed()
    log.info(
        "index %s done in %.1fs: %s deps (%s watched), %s call sites, issue #%s%s",
        repo.full_name,
        result.duration_s,
        result.dependencies,
        result.watched,
        result.call_sites,
        result.install_issue_number,
        " (created)" if result.install_issue_created else "",
    )
    return result


# --------------------------------------------------------------------------------------
# Job plumbing
# --------------------------------------------------------------------------------------


def index_dedupe_key(repo_id: UUID | str) -> str:
    return f"index_repo:{repo_id}"


def enqueue_index_repo(
    session: Session,
    repo: Repo | UUID | str,
    reason: str = "manual",
    *,
    debounce_s: float | None = None,
) -> None:
    """Queue an index run (deduped per repo). With ``debounce_s`` an already-queued run is
    pushed later instead of running now, so a burst of pushes indexes once."""
    repo_id = repo.id if isinstance(repo, Repo) else UUID(str(repo))
    key = index_dedupe_key(repo_id)
    if debounce_s:
        from datetime import timedelta

        when: datetime = utcnow() + timedelta(seconds=debounce_s)
        if reschedule_queued(session, key, when):
            return
        enqueue(
            session,
            JOB_INDEX_REPO,
            {"repo_id": str(repo_id), "reason": reason},
            run_after=when,
            dedupe_key=key,
        )
        return
    enqueue(session, JOB_INDEX_REPO, {"repo_id": str(repo_id), "reason": reason}, dedupe_key=key)


@job_handler(JOB_INDEX_REPO)
def handle_index_repo(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("repo_id")
    try:
        repo_id = UUID(str(raw))
    except (TypeError, ValueError):
        raise NonRetryable(f"index_repo: bad repo_id {raw!r}")
    repo = session.get(Repo, repo_id)
    if repo is None:
        raise NonRetryable(f"index_repo: repo {repo_id} does not exist")
    if not repo.active:
        raise NonRetryable(f"index_repo: repo {repo.full_name} is inactive")
    if repo.org is None or not repo.org.active:
        raise NonRetryable(f"index_repo: org of {repo.full_name} is inactive")
    reason = str(payload.get("reason") or "manual")
    return index_repo(session, repo, reason=reason).as_dict()


def main(argv: list[str] | None = None) -> int:
    """``python -m kintsua.indexer.run owner/name [--reason manual]`` for operators."""
    from kintsua.core.db import session_scope

    parser = argparse.ArgumentParser(prog="kintsua-index")
    parser.add_argument("full_name", help="owner/name of an installed repo")
    parser.add_argument("--reason", default="manual")
    parser.add_argument("--no-network", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    with session_scope() as session:
        repo = session.scalar(select(Repo).where(Repo.full_name == args.full_name))
        if repo is None:
            print(f"no repo {args.full_name!r}")  # noqa: T201 - CLI
            return 2
        res = index_repo(session, repo, reason=args.reason, allow_network=not args.no_network)
        print(res.as_dict())  # noqa: T201 - CLI
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "Budget",
    "IndexResult",
    "dismiss_vanished_impacts",
    "enqueue_index_repo",
    "extract_tree",
    "handle_index_repo",
    "index_dedupe_key",
    "index_repo",
    "known_tools_lookup",
    "upsert_install_issue",
    "write_index",
]
