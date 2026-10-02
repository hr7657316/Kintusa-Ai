"""``index_repo``: clone -> ``.kintsua.yml`` -> three extractors -> resolve -> write rows ->
dismiss impacts whose call sites vanished -> install issue -> enqueue polls.

Registered as the ``index_repo`` job handler (payload ``{"repo_id", "reason"}``). The whole
run is bounded by ``Settings.index_budget_s`` (10 minutes) with checks between phases and
inside the extractors; the clone lives in a temp directory that is always removed.

Nothing from the repo is ever executed: MCP command lines are only read (tool names come
from an operator-seeded server or an existing snapshot) and http(s) lookups of customer
URLs go through ``kintsua.core.netguard`` (docs/CONTRACTS.md, "Source trust"). The write
phase holds a row lock on the repo, so two runs of one repo (an install and a debounced
push on different workers) write one after the other, and a run whose clone is older than
the last write steps aside (``IndexResult.superseded``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from kintsua.core.config import get_settings
from kintsua.core.github import LABEL, get_github_app
from kintsua.core.models import CallSite, Dependency, Impact, Repo, Snapshot, Source
from kintsua.core.netguard import BlockedURL, check_url, is_http_locator
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
from kintsua.indexer.config import merge_api_settings, parse_kintsua_yml
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
# Source.meta keys a live tools fetch of a customer URL may use (never ``headers`` /
# ``auth_env``: those expand worker environment variables into the request)
_URL_FETCH_META_KEYS = ("transport", "timeout_s", "read_timeout_s")
RUNTIME_FILE_PATH = "<runtime>"  # matcher.runtime's call-site path for runtime evidence
# dependency meta kept when a row survives only through runtime call sites
_RUNTIME_KEPT_META = (
    "vendor",
    "display_name",
    "transport",
    "host",
    "server_name",
    "openapi_source_id",
    "changelog_source_id",
)
MAX_DEPENDENCY_NAME = 512  # Dependency.name is String(512)
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
    superseded: bool = False  # a run with a newer clone wrote first; nothing was written
    duration_s: float = 0.0
    phases: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_id": str(self.repo_id),
            "reason": self.reason,
            "head_sha": self.head_sha,
            "superseded": self.superseded,
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
    live server (``kintsua.watchers.mcp.fetch_tools``) only when network is allowed.

    Trust rules (docs/CONTRACTS.md): a locator that is not an http(s) URL is a command line
    and is fetched (spawned) only when its ``Source`` row is operator-seeded
    (``Source.trusted``); otherwise only its snapshot is read. An http(s) URL of a source
    that is not trusted passes ``kintsua.core.netguard.check_url`` first (and every request
    is guarded) when ``Settings.net_guard`` is on."""
    cache: dict[str, list[str] | None] = {}
    net_guard = bool(get_settings().net_guard)

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
        remaining = budget.remaining() if budget is not None else float("inf")
        if names is None and allow_network and remaining > 2 * TOOLS_FETCH_TIMEOUT_S:
            meta = _live_fetch_meta(locator, src, net_guard=net_guard)
            if meta is not None:
                names = _fetch_tool_names(locator, meta)
        cache[locator] = names
        return names

    return lookup


def _live_fetch_meta(locator: str, src: Source | None, *, net_guard: bool) -> dict[str, Any] | None:
    """The meta for a live ``fetch_tools`` of ``locator``, or None when it must not be
    fetched: a command line whose source is not operator-seeded is never spawned, and a
    guarded URL that points at a non-public address is never requested."""
    trusted = src is not None and src.trusted
    if not is_http_locator(locator):
        if not trusted:
            log.info("not running untrusted MCP command %r (snapshot only)", locator[:200])
            return None
        return dict(src.meta or {}) if src is not None else {}
    if trusted:
        meta = dict(src.meta or {}) if src is not None else {}
    else:
        meta = {
            k: v
            for k, v in ((src.meta if src else None) or {}).items()
            if k in _URL_FETCH_META_KEYS
        }
    if net_guard and not trusted:
        try:
            check_url(locator)
        except BlockedURL as e:
            log.info("not fetching MCP tools from %s: %s", locator[:200], e)
            return None
        meta["_net_guard"] = True
    return meta


def _fetch_tool_names(locator: str, meta: dict[str, Any] | None) -> list[str] | None:
    """Live ``tools/list``. Callers decide whether the locator may be fetched at all
    (``_live_fetch_meta``)."""
    try:
        from kintsua.watchers.mcp import fetch_tools
    except ImportError:
        return None
    meta = dict(meta or {})
    if not is_http_locator(locator):
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
    longer found unless they were discovered at runtime. A dependency the code no longer
    shows but the runtime SDK reported calls on keeps its runtime call sites and becomes a
    runtime-discovered row instead of being deleted."""
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
        session.execute(
            delete(CallSite).where(
                CallSite.dependency_id == row.id, CallSite.discovered_at_runtime.is_(False)
            )
        )
        runtime_sites = session.scalar(
            select(func.count()).select_from(CallSite).where(CallSite.dependency_id == row.id)
        )
        if runtime_sites:
            # the code no longer shows it (URL moved to config, ...) but the runtime SDK saw
            # calls: keep the row and that evidence, as a runtime-discovered dependency
            row.discovered_at_runtime = True
            row.meta = {
                k: v
                for k, v in (row.meta or {}).items()
                if k.startswith("runtime_") or k in _RUNTIME_KEPT_META
            }
            continue
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


@dataclass(frozen=True)
class _LiveSite:
    call_site_id: str
    dependency_id: str
    dependency: str
    file_path: str
    line: int
    symbol: str | None
    operation: str
    in_tests: bool
    runtime: bool


def _live_sites(session: Session, repo: Repo) -> list[_LiveSite]:
    rows = session.execute(
        select(
            CallSite.id,
            CallSite.dependency_id,
            Dependency.name,
            CallSite.file_path,
            CallSite.line,
            CallSite.symbol,
            CallSite.operation,
            CallSite.args_shape,
            CallSite.discovered_at_runtime,
        )
        .join(Dependency, Dependency.id == CallSite.dependency_id)
        .where(Dependency.repo_id == repo.id)
        .order_by(CallSite.file_path, CallSite.line)
    ).all()
    return [
        _LiveSite(
            call_site_id=str(r[0]),
            dependency_id=str(r[1]),
            dependency=str(r[2]),
            file_path=str(r[3]),
            line=int(r[4] or 0),
            symbol=r[5],
            operation=str(r[6]),
            in_tests=bool((r[7] or {}).get("in_tests")),
            runtime=bool(r[8]),
        )
        for r in rows
    ]


def _moved_record(old: dict[str, Any], site: _LiveSite) -> dict[str, Any]:
    rec = {
        k: v for k, v in old.items() if k not in ("in_tests", "discovered_at_runtime", "moved_from")
    }
    rec.update(
        call_site_id=site.call_site_id,
        file_path=site.file_path,
        line=site.line,
        symbol=site.symbol,
        operation=site.operation,
        dependency_id=site.dependency_id,
        dependency=site.dependency,
        moved_from=f"{old.get('file_path')}:{old.get('line')}",
    )
    if site.in_tests:
        rec["in_tests"] = True
    if site.runtime:
        rec["discovered_at_runtime"] = True
    return rec


def dismiss_vanished_impacts(
    session: Session,
    repo: Repo,
    head_sha: str,
    new_sites: set[tuple[str, str]] | None = None,
    *,
    partial_files: Iterable[str] = (),
) -> list[Impact]:
    """Dismiss open impacts (detected / issued / fixing / pr_open) whose operations the repo
    no longer calls; their GitHub issue gets a comment saying why and is closed.

    "No longer calls" is decided per operation and dependency, not per file: a call that
    moved to another file (rename, refactor) keeps the impact alive and its
    ``matched_call_sites`` entry is rewritten to the new place. An impact is never dismissed
    on the strength of a file that still exists but was not fully parsed this run
    (``partial_files``: over the size limit, binary, syntax errors, extractor error), nor
    for runtime evidence (``<runtime>`` sites, which a re-index never removes)."""
    impacts = list(
        session.scalars(
            select(Impact).where(Impact.repo_id == repo.id, Impact.status.in_(OPEN_IMPACT_STATUSES))
        )
    )
    if not impacts:
        return []
    live = _live_sites(session, repo)
    at = {(s.file_path, s.operation) for s in live} | set(new_sites or ())
    by_dep_id: dict[tuple[str, str], list[_LiveSite]] = {}
    by_dep_name: dict[tuple[str, str], list[_LiveSite]] = {}
    by_op: dict[str, list[_LiveSite]] = {}
    for s in live:
        by_dep_id.setdefault((s.dependency_id, s.operation), []).append(s)
        by_dep_name.setdefault((s.dependency, s.operation), []).append(s)
        by_op.setdefault(s.operation, []).append(s)
    partial = set(partial_files)

    def elsewhere(m: dict[str, Any], op: str) -> list[_LiveSite]:
        dep_id, dep_name = m.get("dependency_id"), m.get("dependency")
        if dep_id or dep_name:
            return by_dep_id.get((str(dep_id), op)) or by_dep_name.get((str(dep_name), op)) or []
        return by_op.get(op, [])

    def still_there(m: dict[str, Any]) -> bool:
        fp, op = str(m.get("file_path")), str(m.get("operation"))
        return bool(
            (fp, op) in at
            or fp == RUNTIME_FILE_PATH
            or m.get("discovered_at_runtime")
            or fp in partial
        )

    dismissed: list[Impact] = []
    now = utcnow()
    for imp in impacts:
        records = [
            (m, isinstance(m, dict) and bool(m.get("file_path") and m.get("operation")))
            for m in imp.matched_call_sites or []
        ]
        kept = [m for m, ok in records if ok and still_there(m)]
        alive, moved = bool(kept), False
        placed = {(str(m["file_path"]), int(m.get("line") or 0), str(m["operation"])) for m in kept}
        rewritten: list[Any] = []
        for m, ok in records:
            if not ok or still_there(m):
                rewritten.append(m)
                continue
            sites = elsewhere(m, str(m["operation"]))
            if not sites:
                rewritten.append(m)  # this one is gone; the impact may live through others
                continue
            alive = moved = True
            for s in sites:
                if (s.file_path, s.line, s.operation) not in placed:
                    placed.add((s.file_path, s.line, s.operation))
                    rewritten.append(_moved_record(m, s))
        if alive:
            if moved:
                imp.matched_call_sites = rewritten
            continue
        if not _impact_pairs(imp):
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
    """The file defines the spec keys; anything else on Repo.config survives a re-index, and
    the values the settings API set (recorded under ``config["settings"]``) are merged back
    into the spec keys (``kintsua.indexer.config.merge_api_settings``)."""
    merged = {k: v for k, v in (old or {}).items() if k not in CONFIG_KEYS}
    merged.update(new)
    return merge_api_settings(merged)


def _drop_oversized(extracted: ExtractResult) -> None:
    """Dependencies whose name (an MCP command line or URL) does not fit ``dependency.name``
    are left out with an extract error instead of failing the whole run."""
    keep: list[DependencyInfo] = []
    dropped: set[tuple[str, str]] = set()
    for dep in extracted.dependencies:
        if len(dep.name) <= MAX_DEPENDENCY_NAME:
            keep.append(dep)
            continue
        dropped.add(dep.key)
        where = str(dep.meta.get("config_file") or "?")
        extracted.stats.record_error(
            where, f"{dep.kind} locator longer than {MAX_DEPENDENCY_NAME} characters skipped"
        )
    if dropped:
        extracted.dependencies = keep
        extracted.call_sites = [c for c in extracted.call_sites if c.dependency_key not in dropped]


def _lock_repo_for_write(session: Session, repo: Repo, cloned_at: datetime) -> bool:
    """Serialise the write phase per repo (``SELECT ... FOR UPDATE`` on the repo row, held
    until the write commits). Returns False when a run whose clone is newer than ours has
    already written: this run's tree is stale and must not overwrite it."""
    stats = session.execute(
        select(Repo.index_stats).where(Repo.id == repo.id).with_for_update()
    ).scalar_one_or_none()
    written = (stats or {}).get("cloned_at") if isinstance(stats, dict) else None
    if not written:
        return True
    try:
        return datetime.fromisoformat(str(written)) <= cloned_at
    except (TypeError, ValueError):  # unparsable or naive: treat as older
        return True


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
        file_config: dict[str, Any] | None = None
        with temp_clone_dir() as tmp:
            dest = tmp / "repo"
            clone_url = get_github_app().clone_url(org.github_installation_id, repo.full_name)
            cloned_at = utcnow()  # the tree is at least this new
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
                    file_config = parse_kintsua_yml(text)
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
            _drop_oversized(extracted)
            result.phases["extract"] = time.monotonic() - t
        budget.check("resolve")

        t = time.monotonic()
        # one writer per repo from here to the commit; a run with an older tree than the
        # last write steps aside (two workers can hold an install and a push run at once)
        if not _lock_repo_for_write(session, repo, cloned_at):
            session.rollback()
            result.superseded = True
            log.info("index %s (%s): a newer index already wrote; skipping", repo.full_name, reason)
            return result
        session.refresh(repo)  # config / stats as committed by whoever wrote last
        if file_config is not None:
            repo.config = _merge_config(repo.config, file_config)
        resolved = resolve_dependencies(session, extracted.dependencies, repo_config=repo.config)
        result.phases["resolve"] = time.monotonic() - t
        budget.check("write")

        t = time.monotonic()
        written = write_index(session, repo, head, resolved.dependencies, extracted.call_sites)
        dismissed = dismiss_vanished_impacts(
            session, repo, head, written.new_sites, partial_files=extracted.stats.partial_files
        )
        repo.last_indexed_sha = head
        repo.indexed_at = utcnow()
        repo.last_index_error = None
        repo.index_stats = {
            "files_parsed": extracted.stats.files_parsed,
            "parse_errors": extracted.stats.parse_errors,
            "head_sha": head,
            "reason": reason,
            "indexed_at": repo.indexed_at.isoformat(),
            "cloned_at": cloned_at.isoformat(),
        }
        for sid in resolved.new_source_ids:
            enqueue(
                session, JOB_POLL_SOURCE, {"source_id": str(sid)}, dedupe_key=f"poll_source:{sid}"
            )
        session.commit()
        result.phases["write"] = time.monotonic() - t

        t = time.monotonic()
        # the install issue too has one writer per repo (a concurrent run may have just
        # created it: refresh to see its number)
        session.execute(select(Repo.id).where(Repo.id == repo.id).with_for_update())
        session.refresh(repo)
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
