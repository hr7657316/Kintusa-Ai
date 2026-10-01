"""Per-repo settings API (session auth; the HTMX settings page calls the same endpoints).

* ``GET  /v1/repos/{id}/dependencies`` - the dependency map (``kintsua.indexer.depmap``).
* ``GET  /v1/repos/{id}/changes``      - impacts with their change event / classification.
* ``POST /v1/repos/{id}/sources``      - add a spec / changelog URL or an MCP server.
* ``POST /v1/repos/{id}/mute``, ``DELETE ...?pattern=`` - mute patterns (open impacts that
  are now fully muted are dismissed with a comment on their issue).
* ``POST /v1/repos/{id}/fix-mode``     - ``pr`` | ``issue_only``.
* ``POST /v1/repos/{id}/issues/open``  - turn a draft impact (or all drafts) into a GitHub
  issue; this is how a human reviews drafts while ``auto_issue`` is off.
* ``DELETE /v1/repos/{id}/data``       - delete every runtime event, call site, dependency
  and impact of the repo (the repo row stays).

Every endpoint answers JSON, or an HTML fragment when called by HTMX (``HX-Request``).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from kintsua.api.deps import get_db, read_body, render, repo_access, wants_html
from kintsua.api.schemas import AddSourceBody, FixModeBody, MuteBody, OpenIssueBody
from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Classification,
    Dependency,
    Impact,
    Org,
    Repo,
    RuntimeEvent,
    Source,
)
from kintsua.core.queue import JOB_POLL_SOURCE, enqueue
from kintsua.core.util import utcnow
from kintsua.indexer.config import effective_fix_mode
from kintsua.indexer.depmap import dependency_map
from kintsua.matcher.issues import close_impact_issue, create_issue_for_impact
from kintsua.matcher.match import maybe_enqueue_fix, open_issues_for_pending
from kintsua.matcher.mute import is_muted
from kintsua.watchers.run import find_or_create_source

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/repos", tags=["repos"])

ACTIVE_IMPACT_STATUSES: tuple[str, ...] = ("detected", "issued", "fixing", "pr_open")
DEFAULT_CHANGES_LIMIT = 50


# --------------------------------------------------------------------------------------
# Config helpers (Repo.config is JSONB: always assign a new dict so SQLAlchemy sees it)
# --------------------------------------------------------------------------------------


def repo_config(repo: Repo) -> dict[str, Any]:
    return dict(repo.config or {})


def mute_list(repo: Repo) -> list[str]:
    out: list[str] = []
    for m in (repo.config or {}).get("mute") or []:
        if isinstance(m, str) and m.strip() and m.strip() not in out:
            out.append(m.strip())
    return out


def config_sources(repo: Repo) -> list[dict[str, Any]]:
    return [s for s in (repo.config or {}).get("sources") or [] if isinstance(s, dict)]


def _entry_locator(entry: dict[str, Any]) -> str | None:
    v = entry.get("url") or entry.get("command") or entry.get("locator")
    return str(v).strip() if v else None


def repo_fix_mode(repo: Repo) -> str | None:
    fix = (repo.config or {}).get("fix")
    mode = fix.get("mode") if isinstance(fix, dict) else None
    return str(mode) if mode in ("pr", "issue_only") else None


# --------------------------------------------------------------------------------------
# Change list
# --------------------------------------------------------------------------------------


def _ops_summary(diff: dict[str, Any]) -> str:
    ops = diff.get("ops") or []
    total = int(diff.get("ops_total") or len(ops))
    if not ops:
        return "text changed (awaiting classification)" if diff.get("text") else "change detected"
    counts = Counter(str(op.get("op")) for op in ops if isinstance(op, dict))
    parts = ", ".join(f"{n} {kind}" for kind, n in counts.most_common(3))
    return f"{total} change{'s' if total != 1 else ''} ({parts}); awaiting classification"


def impact_entry(
    impact: Impact,
    row: ChangeEventRow | None,
    cls: Classification | None,
    source: Source | None,
    repo: Repo,
) -> dict[str, Any]:
    diff = dict(row.diff or {}) if row is not None else {}
    issue = impact.github_issue_number
    pr = impact.github_pr_number
    html = f"https://github.com/{repo.full_name}"
    vendor = source.vendor if source is not None else None
    if not vendor:
        for site in impact.matched_call_sites or []:
            if isinstance(site, dict) and site.get("vendor"):
                vendor = str(site["vendor"])
                break
    return {
        "impact_id": str(impact.id),
        "change_event_id": str(impact.change_event_id),
        "status": impact.status,
        "draft": impact.status == "detected" and issue is None,
        "severity": cls.severity if cls is not None else "unknown",
        "category": cls.category if cls is not None else "unclassified",
        "summary": cls.summary if cls is not None else _ops_summary(diff),
        "affected_paths": list(cls.affected_paths or []) if cls is not None else [],
        "migration_hint": cls.migration_hint if cls is not None else None,
        "rule_hits": list(cls.rule_hits or []) if cls is not None else [],
        "needs_human": bool(cls.needs_human) if cls is not None else False,
        "detected_at": (row.detected_at if row is not None else impact.created_at).isoformat(),
        "created_at": impact.created_at.isoformat(),
        "resolved_at": impact.resolved_at.isoformat() if impact.resolved_at else None,
        "issue_number": issue,
        "issue_url": f"{html}/issues/{issue}" if issue else None,
        "pr_number": pr,
        "pr_url": f"{html}/pull/{pr}" if pr else None,
        "pr_branch": impact.pr_branch,
        "pr_outcome": impact.pr_outcome,
        "fix_reason": impact.fix_reason,
        "vendor": vendor,
        "source_kind": diff.get("source_kind") or (source.kind if source is not None else None),
        "source_name": source.display_name if source is not None else None,
        "from_version": diff.get("from_version"),
        "to_version": diff.get("to_version"),
        "op_count": int(diff.get("ops_total") or len(diff.get("ops") or [])),
        "runtime_error_count": int(impact.runtime_error_count or 0),
        "matched_call_sites": list(impact.matched_call_sites or []),
        "call_site_count": len(impact.matched_call_sites or []),
    }


def change_entries(
    session: Session, repo: Repo, limit: int = DEFAULT_CHANGES_LIMIT
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Impact, ChangeEventRow, Classification, Source)
        .join(ChangeEventRow, ChangeEventRow.id == Impact.change_event_id, isouter=True)
        .join(Classification, Classification.id == Impact.classification_id, isouter=True)
        .join(Source, Source.id == ChangeEventRow.source_id, isouter=True)
        .where(Impact.repo_id == repo.id)
        .order_by(Impact.created_at.desc())
        .limit(limit)
    ).all()
    return [impact_entry(impact, row, cls, source, repo) for impact, row, cls, source in rows]


# --------------------------------------------------------------------------------------
# GET dependencies / changes
# --------------------------------------------------------------------------------------


@router.get("/{repo_id}/dependencies")
def get_dependencies(
    repo: Repo = Depends(repo_access), session: Session = Depends(get_db)
) -> dict[str, Any]:
    org = session.get(Org, repo.org_id)
    return dependency_map(
        session, repo, org_settings=dict(org.settings or {}) if org else {}
    ).model_dump(mode="json")


@router.get("/{repo_id}/changes")
def get_changes(
    limit: int = Query(DEFAULT_CHANGES_LIMIT, ge=1, le=500),
    repo: Repo = Depends(repo_access),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    changes = change_entries(session, repo, limit)
    return {
        "repo_id": str(repo.id),
        "repo": repo.full_name,
        "count": len(changes),
        "changes": changes,
    }


# --------------------------------------------------------------------------------------
# POST sources
# --------------------------------------------------------------------------------------


def add_source(session: Session, repo: Repo, body: AddSourceBody) -> dict[str, Any]:
    meta: dict[str, Any] = {"configured_by": "settings"}
    if body.transport:
        meta["transport"] = body.transport
        if body.transport == "stdio":
            meta["quiet"] = True
    assert body.locator is not None
    source = find_or_create_source(
        session, body.kind, body.locator, display_name=body.display_name, meta=meta
    )
    if not source.active:
        source.active = True
    dep = session.scalar(
        select(Dependency).where(
            Dependency.repo_id == repo.id,
            Dependency.kind == body.kind,
            Dependency.name == source.locator,
        )
    )
    dep_meta: dict[str, Any] = {"configured": True}
    if body.hosts:
        dep_meta["hosts"] = list(body.hosts)
    if body.display_name:
        dep_meta["display_name"] = body.display_name
    if body.transport:
        dep_meta["transport"] = body.transport
    if dep is None:
        dep = Dependency(
            repo_id=repo.id,
            source_id=source.id,
            kind=body.kind,
            name=source.locator,
            meta=dep_meta,
        )
        session.add(dep)
    else:
        dep.source_id = source.id
        dep.meta = {**(dep.meta or {}), **dep_meta}
    session.flush()
    job = enqueue(
        session,
        JOB_POLL_SOURCE,
        {"source_id": str(source.id)},
        dedupe_key=f"poll_source:{source.id}",
    )
    cfg = repo_config(repo)
    sources = config_sources(repo)
    if not any(_entry_locator(s) == source.locator for s in sources):
        sources.append(body.config_entry())
    cfg["sources"] = sources
    repo.config = cfg
    session.flush()
    log.info("source %s %s added to %s", body.kind, source.locator, repo.full_name)
    return {
        "source_id": str(source.id),
        "dependency_id": str(dep.id),
        "kind": body.kind,
        "locator": source.locator,
        "display_name": source.display_name,
        "poll_job_id": str(job.id) if job is not None else None,
        "sources": sources,
    }


@router.post("/{repo_id}/sources")
async def post_source(
    request: Request, repo: Repo = Depends(repo_access), session: Session = Depends(get_db)
) -> Response:
    body = await read_body(request, AddSourceBody)
    result = add_source(session, repo, body)
    session.commit()
    if wants_html(request):
        resp = render(
            request, "partials/notice.html", kind="ok", message=f"Watching {result['locator']}."
        )
        resp.headers["HX-Refresh"] = "true"
        return resp
    return JSONResponse(result, status_code=201)


# --------------------------------------------------------------------------------------
# Mutes
# --------------------------------------------------------------------------------------


def _impact_vendor(session: Session, impact: Impact) -> str | None:
    for site in impact.matched_call_sites or []:
        if isinstance(site, dict) and site.get("vendor"):
            return str(site["vendor"])
    row = session.get(ChangeEventRow, impact.change_event_id)
    if row is not None:
        src = session.get(Source, row.source_id)
        if src is not None and src.vendor:
            return src.vendor
    return None


def muting_pattern(session: Session, impact: Impact, patterns: list[str]) -> str | None:
    """The pattern that mutes every matched call site of ``impact`` (or its affected paths
    when it has none), else None."""
    if not patterns:
        return None
    vendor = _impact_vendor(session, impact)
    candidates: list[tuple[str | None, str]] = []
    for site in impact.matched_call_sites or []:
        if isinstance(site, dict) and site.get("operation"):
            candidates.append((str(site.get("vendor") or vendor or ""), str(site["operation"])))
    if not candidates:
        cls = (
            session.get(Classification, impact.classification_id)
            if impact.classification_id
            else None
        )
        paths = [str(p) for p in (cls.affected_paths or [])] if cls is not None else []
        candidates = [(vendor, p) for p in paths] or [(vendor, "*")]
    if not all(is_muted(patterns, v or None, op) for v, op in candidates):
        return None
    # name the single pattern that covers everything when there is one, else the first
    for pattern in patterns:
        if all(is_muted([pattern], v or None, op) for v, op in candidates):
            return pattern
    return patterns[0]


def dismiss_muted_impacts(session: Session, repo: Repo, patterns: list[str]) -> list[str]:
    """Dismiss (and close on GitHub) open impacts that the mute list now covers."""
    impacts = list(
        session.scalars(
            select(Impact).where(
                Impact.repo_id == repo.id, Impact.status.in_(list(ACTIVE_IMPACT_STATUSES))
            )
        )
    )
    dismissed: list[str] = []
    for impact in impacts:
        pattern = muting_pattern(session, impact, patterns)
        if pattern is None:
            continue
        reason = f"muted by .kintsua.yml pattern `{pattern}`"
        try:
            close_impact_issue(session, impact, reason)
        except Exception as e:  # noqa: BLE001 - GitHub trouble must not keep the impact open
            log.warning("could not close issue for impact %s: %s", impact.id, e)
            session.rollback()
            impact = session.get(Impact, impact.id) or impact
            impact.status = "dismissed"
            impact.fix_reason = (
                impact.fix_reason or f"{reason} (issue comment failed: {type(e).__name__})"
            )
            impact.resolved_at = impact.resolved_at or utcnow()
            session.commit()
        dismissed.append(str(impact.id))
    return dismissed


def _mute_response(
    request: Request, repo: Repo, mutes: list[str], dismissed: list[str]
) -> Response:
    if wants_html(request):
        return render(request, "partials/mutes.html", repo=repo, mutes=mutes, dismissed=dismissed)
    return JSONResponse({"repo_id": str(repo.id), "mute": mutes, "dismissed": dismissed})


@router.post("/{repo_id}/mute")
async def post_mute(
    request: Request, repo: Repo = Depends(repo_access), session: Session = Depends(get_db)
) -> Response:
    body = await read_body(request, MuteBody)
    mutes = mute_list(repo)
    if body.pattern not in mutes:
        mutes.append(body.pattern)
    cfg = repo_config(repo)
    cfg["mute"] = mutes
    repo.config = cfg
    session.commit()
    dismissed = dismiss_muted_impacts(session, repo, mutes)
    session.commit()
    log.info(
        "mute %r added to %s (%d impact(s) dismissed)", body.pattern, repo.full_name, len(dismissed)
    )
    return _mute_response(request, repo, mutes, dismissed)


@router.delete("/{repo_id}/mute")
def delete_mute(
    request: Request,
    pattern: str = Query(..., min_length=1),
    repo: Repo = Depends(repo_access),
    session: Session = Depends(get_db),
) -> Response:
    pattern = pattern.strip()
    mutes = [m for m in mute_list(repo) if m != pattern]
    cfg = repo_config(repo)
    cfg["mute"] = mutes
    repo.config = cfg
    session.commit()
    return _mute_response(request, repo, mutes, [])


# --------------------------------------------------------------------------------------
# Fix mode
# --------------------------------------------------------------------------------------


@router.post("/{repo_id}/fix-mode")
async def post_fix_mode(
    request: Request, repo: Repo = Depends(repo_access), session: Session = Depends(get_db)
) -> Response:
    body = await read_body(request, FixModeBody)
    cfg = repo_config(repo)
    fix = dict(cfg.get("fix") or {}) if isinstance(cfg.get("fix"), dict) else {}
    fix["mode"] = body.mode
    cfg["fix"] = fix
    repo.config = cfg
    session.commit()
    org = session.get(Org, repo.org_id)
    effective = effective_fix_mode(repo.config, org.settings if org else None)
    if wants_html(request):
        return render(
            request,
            "partials/fix_mode.html",
            repo=repo,
            fix_mode=effective,
            repo_fix_mode=body.mode,
        )
    return JSONResponse({"repo_id": str(repo.id), "fix_mode": body.mode, "effective": effective})


# --------------------------------------------------------------------------------------
# Drafts -> issues
# --------------------------------------------------------------------------------------


def open_draft(session: Session, repo: Repo, impact_id: str) -> Impact:
    try:
        iid = UUID(str(impact_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="impact not found")
    impact = session.get(Impact, iid)
    if impact is None or impact.repo_id != repo.id:
        raise HTTPException(status_code=404, detail="impact not found")
    if impact.github_issue_number:
        return impact
    if impact.status != "detected":
        raise HTTPException(status_code=409, detail=f"impact is {impact.status}, not a draft")
    try:
        create_issue_for_impact(session, impact)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - surface GitHub trouble as a 502, never a 500
        log.exception("could not open issue for impact %s", impact.id)
        session.rollback()
        raise HTTPException(
            status_code=502, detail=f"GitHub refused the issue: {type(e).__name__}: {e}"
        )
    maybe_enqueue_fix(session, impact)
    session.commit()
    return impact


@router.post("/{repo_id}/issues/open")
async def post_open_issue(
    request: Request, repo: Repo = Depends(repo_access), session: Session = Depends(get_db)
) -> Response:
    body = await read_body(request, OpenIssueBody)
    if body.impact_id:
        opened = [open_draft(session, repo, body.impact_id)]
    else:
        opened = open_issues_for_pending(session, repo.id)
    session.commit()
    result = {
        "repo_id": str(repo.id),
        "count": len(opened),
        "opened": [
            {"impact_id": str(i.id), "issue_number": i.github_issue_number, "status": i.status}
            for i in opened
        ],
    }
    if wants_html(request):
        return render(
            request,
            "partials/changes.html",
            repo=repo,
            changes=change_entries(session, repo, 100),
            notice=f"Opened {len(opened)} issue(s).",
        )
    return JSONResponse(result)


# --------------------------------------------------------------------------------------
# DELETE data
# --------------------------------------------------------------------------------------


def delete_repo_data(session: Session, repo: Repo) -> dict[str, int]:
    dep_ids = select(Dependency.id).where(Dependency.repo_id == repo.id)
    runtime = session.execute(
        delete(RuntimeEvent)
        .where(RuntimeEvent.repo_id == repo.id)
        .execution_options(synchronize_session=False)
    ).rowcount
    impacts = session.execute(
        delete(Impact).where(Impact.repo_id == repo.id).execution_options(synchronize_session=False)
    ).rowcount
    call_sites = session.execute(
        delete(CallSite)
        .where(CallSite.dependency_id.in_(dep_ids))
        .execution_options(synchronize_session=False)
    ).rowcount
    deps = session.execute(
        delete(Dependency)
        .where(Dependency.repo_id == repo.id)
        .execution_options(synchronize_session=False)
    ).rowcount
    repo.last_indexed_sha = None
    repo.indexed_at = None
    repo.last_index_error = None
    session.flush()
    log.info(
        "data deleted for %s: %d runtime events, %d impacts, %d call sites, %d dependencies",
        repo.full_name,
        runtime,
        impacts,
        call_sites,
        deps,
    )
    return {
        "runtime_events": int(runtime or 0),
        "impacts": int(impacts or 0),
        "call_sites": int(call_sites or 0),
        "dependencies": int(deps or 0),
    }


@router.delete("/{repo_id}/data")
def delete_data(
    request: Request, repo: Repo = Depends(repo_access), session: Session = Depends(get_db)
) -> Response:
    counts = delete_repo_data(session, repo)
    session.commit()
    if wants_html(request):
        return render(request, "partials/deleted.html", repo=repo, counts=counts)
    return JSONResponse({"repo_id": str(repo.id), "deleted": counts})


__all__ = [
    "ACTIVE_IMPACT_STATUSES",
    "add_source",
    "change_entries",
    "config_sources",
    "delete_repo_data",
    "dismiss_muted_impacts",
    "impact_entry",
    "mute_list",
    "muting_pattern",
    "open_draft",
    "repo_config",
    "repo_fix_mode",
    "router",
]
