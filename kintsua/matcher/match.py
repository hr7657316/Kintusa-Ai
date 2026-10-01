"""The matcher: a classified change event -> one ``impact`` per affected repo.

``match_change_event(session, change_event_row)``:

1. Load the classification (none yet -> enqueue ``classify_event`` and return ``[]``).
   Only ``breaking`` / ``degraded`` proceed; ``safe`` / ``unknown`` create nothing.
2. Find the dependencies that point at the event's source: ``dependency.source_id`` or,
   for SDK packages that also watch the vendor's spec / changelog,
   ``meta.openapi_source_id`` / ``meta.changelog_source_id``; joined to active repos of
   active orgs.
3. Per repo, collect the call sites whose operation falls under an affected path
   (``kintsua.matcher.operations``), minus the repo's ``.kintsua.yml`` mutes
   (``kintsua.matcher.mute``). Package events (``package:<name>``, ``*`` or no affected
   paths from a registry source) affect every call site of the package dependency; a
   major bump creates an impact even when no call site is recorded.
4. Create one ``impact`` per ``(repo, change_event)`` (unique; an existing one is
   returned as is) in status ``detected`` with ``matched_call_sites``.
5. If issues may open (``Settings.auto_issue`` or ``Org.settings.auto_issue``) enqueue
   ``open_issue``; otherwise the impact stays a draft on the settings page.

Job handlers: ``match_event`` (``{"change_event_id"}``) and ``open_issue``
(``{"impact_id"}``: create the issue, then enqueue ``fix_impact`` when the effective fix
mode is ``pr`` and the category is fixable).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kintsua.core.config import get_settings
from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Classification,
    Dependency,
    Impact,
    Org,
    Repo,
    Source,
)
from kintsua.core.queue import (
    JOB_CLASSIFY_EVENT,
    JOB_FIX_IMPACT,
    JOB_MATCH_EVENT,
    JOB_OPEN_ISSUE,
    NonRetryable,
    enqueue,
    job_handler,
)
from kintsua.matcher.issues import (
    ACTIONABLE_SEVERITIES,
    create_issue_for_impact,
    effective_fix_mode,
    is_fixable,
)
from kintsua.matcher.mute import clean_patterns, is_muted
from kintsua.matcher.operations import matches_any

log = logging.getLogger(__name__)

PACKAGE_KINDS: tuple[str, ...] = ("pypi", "npm")
RULE_MAJOR_BUMP = "major_bump"
OPEN_STATUSES: tuple[str, ...] = ("detected", "issued", "fixing", "pr_open")


# --------------------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------------------


def issues_may_open(org: Org | None) -> bool:
    """Issues open automatically when ``Settings.auto_issue`` or the org's
    ``settings.auto_issue`` is true; otherwise impacts stay drafts."""
    if get_settings().auto_issue:
        return True
    return bool(((org.settings if org is not None else None) or {}).get("auto_issue"))


def open_issue_dedupe_key(impact_id: Any) -> str:
    return f"open_issue:{impact_id}"


def fix_impact_dedupe_key(impact_id: Any) -> str:
    return f"fix_impact:{impact_id}"


def enqueue_open_issue(session: Session, impact: Impact) -> bool:
    enqueue(
        session,
        JOB_OPEN_ISSUE,
        {"impact_id": str(impact.id)},
        dedupe_key=open_issue_dedupe_key(impact.id),
    )
    return True


def maybe_enqueue_fix(
    session: Session, impact: Impact, *, classification: Classification | None = None
) -> bool:
    """After an issue exists: queue ``fix_impact`` when the effective fix mode is ``pr``,
    the category is one the fixer handles and the impact is freshly issued."""
    if impact.status != "issued":
        return False
    cls = classification
    if cls is None and impact.classification_id is not None:
        cls = session.get(Classification, impact.classification_id)
    if cls is None:
        cls = session.scalar(
            select(Classification).where(Classification.change_event_id == impact.change_event_id)
        )
    if not is_fixable(cls):
        return False
    repo = session.get(Repo, impact.repo_id)
    if repo is None:
        return False
    org = session.get(Org, repo.org_id)
    if effective_fix_mode(repo.config, org.settings if org is not None else None) != "pr":
        return False
    enqueue(
        session,
        JOB_FIX_IMPACT,
        {"impact_id": str(impact.id)},
        dedupe_key=fix_impact_dedupe_key(impact.id),
    )
    return True


# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------


def candidate_dependencies(session: Session, source_id: UUID) -> list[Dependency]:
    """Dependencies of active repos that watch ``source_id`` directly or through the
    vendor spec / changelog an SDK package is mapped to."""
    sid = str(source_id)
    stmt = (
        select(Dependency)
        .join(Repo, Repo.id == Dependency.repo_id)
        .join(Org, Org.id == Repo.org_id)
        .where(
            Repo.active.is_(True),
            Org.active.is_(True),
            or_(
                Dependency.source_id == source_id,
                Dependency.meta["openapi_source_id"].astext == sid,
                Dependency.meta["changelog_source_id"].astext == sid,
            ),
        )
        .order_by(Dependency.created_at)
    )
    return list(session.scalars(stmt))


def dependency_vendor(dep: Dependency, source: Source | None) -> str | None:
    meta = dep.meta or {}
    vendor = meta.get("vendor")
    if vendor:
        return str(vendor)
    dep_source = getattr(dep, "source", None)
    if dep_source is not None and dep_source.vendor:
        return str(dep_source.vendor)
    if source is not None and source.vendor:
        return str(source.vendor)
    return dep.name or None


def is_package_event(source: Source | None, change_event_row: ChangeEventRow) -> bool:
    kind = (change_event_row.diff or {}).get("source_kind") or (source.kind if source else None)
    return kind in PACKAGE_KINDS


def is_major_bump(classification: Classification) -> bool:
    return RULE_MAJOR_BUMP in (classification.rule_hits or [])


def site_record(site: CallSite, dep: Dependency, vendor: str | None) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "call_site_id": str(site.id),
        "file_path": site.file_path,
        "line": int(site.line or 0),
        "symbol": site.symbol,
        "operation": site.operation,
        "dependency_id": str(dep.id),
        "dependency": dep.name,
    }
    if vendor:
        rec["vendor"] = vendor
    if (site.args_shape or {}).get("in_tests"):
        rec["in_tests"] = True
    if site.discovered_at_runtime:
        rec["discovered_at_runtime"] = True
    return rec


def match_call_sites(
    session: Session,
    deps: list[Dependency],
    *,
    source: Source | None,
    source_id: UUID,
    affected_paths: list[str],
    package_event: bool,
    mute_patterns: list[str],
) -> list[dict[str, Any]]:
    """The call sites of ``deps`` (one repo) that fall under the affected paths, minus
    mutes, as ``matched_call_sites`` records."""
    if not deps:
        return []
    sites = list(
        session.scalars(
            select(CallSite)
            .where(CallSite.dependency_id.in_([d.id for d in deps]))
            .order_by(CallSite.file_path, CallSite.line)
        )
    )
    by_dep = {d.id: d for d in deps}
    affected = [str(p) for p in affected_paths if isinstance(p, str) and p.strip()]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for site in sites:
        dep = by_dep.get(site.dependency_id)
        if dep is None or str(site.id) in seen:
            continue
        direct_package = package_event and dep.source_id == source_id and dep.kind in PACKAGE_KINDS
        if direct_package:
            hit = True  # a package release affects every call of that package
        else:
            hit = matches_any(site.operation, affected)
        if not hit:
            continue
        vendor = dependency_vendor(dep, source)
        if is_muted(mute_patterns, vendor, site.operation):
            log.info("call site %s %s muted for %s", site.file_path, site.operation, dep.repo_id)
            continue
        seen.add(str(site.id))
        out.append(site_record(site, dep, vendor))
    # production code first, tests after
    out.sort(key=lambda r: (bool(r.get("in_tests")), r["file_path"], r["line"]))
    return out


def ensure_impact(
    session: Session,
    repo: Repo,
    change_event_row: ChangeEventRow,
    classification: Classification,
    matched: list[dict[str, Any]],
) -> tuple[Impact, bool]:
    """``(impact, created)``: unique on ``(repo, change_event)``; on conflict the existing
    row is returned untouched."""
    existing = session.scalar(
        select(Impact).where(
            Impact.repo_id == repo.id, Impact.change_event_id == change_event_row.id
        )
    )
    if existing is not None:
        return existing, False
    impact = Impact(
        repo_id=repo.id,
        change_event_id=change_event_row.id,
        classification_id=classification.id,
        status="detected",
        matched_call_sites=matched,
    )
    try:
        with session.begin_nested():
            session.add(impact)
            session.flush()
    except IntegrityError:
        winner = session.scalar(
            select(Impact).where(
                Impact.repo_id == repo.id, Impact.change_event_id == change_event_row.id
            )
        )
        if winner is None:  # pragma: no cover - cannot happen
            raise
        return winner, False
    return impact, True


def after_match(session: Session, impact: Impact, org: Org | None) -> bool:
    """Queue the issue when policy allows; returns whether it was queued."""
    if impact.status != "detected" or impact.github_issue_number:
        return False
    if not issues_may_open(org):
        return False
    return enqueue_open_issue(session, impact)


def match_change_event(session: Session, change_event_row: ChangeEventRow) -> list[Impact]:
    """See the module docstring. Commits."""
    row = change_event_row
    classification = session.scalar(
        select(Classification).where(Classification.change_event_id == row.id)
    )
    if classification is None:
        enqueue(
            session,
            JOB_CLASSIFY_EVENT,
            {"change_event_id": str(row.id)},
            dedupe_key=f"classify_event:{row.id}",
        )
        session.commit()
        log.info("change_event %s has no classification yet; classify_event queued", row.id)
        return []
    if classification.severity not in ACTIONABLE_SEVERITIES:
        log.info("change_event %s is %s; nothing to match", row.id, classification.severity)
        return []

    source = session.get(Source, row.source_id)
    package_event = is_package_event(source, row)
    affected = [str(p) for p in (classification.affected_paths or []) if isinstance(p, str)]
    if package_event and not affected:
        affected = ["*"]
    major_bump = package_event and is_major_bump(classification)

    deps = candidate_dependencies(session, row.source_id)
    by_repo: dict[UUID, list[Dependency]] = defaultdict(list)
    for dep in deps:
        by_repo[dep.repo_id].append(dep)

    impacts: list[Impact] = []
    for repo_id, repo_deps in by_repo.items():
        repo = session.get(Repo, repo_id)
        if repo is None:
            continue
        org = session.get(Org, repo.org_id)
        mutes = clean_patterns((repo.config or {}).get("mute"))
        matched = match_call_sites(
            session,
            repo_deps,
            source=source,
            source_id=row.source_id,
            affected_paths=affected,
            package_event=package_event,
            mute_patterns=mutes,
        )
        package_dep_hit = major_bump and any(
            d.source_id == row.source_id and d.kind in PACKAGE_KINDS for d in repo_deps
        )
        if not matched and not package_dep_hit:
            continue
        impact, created = ensure_impact(session, repo, row, classification, matched)
        queued = after_match(session, impact, org)
        log.info(
            "impact %s for %s: %s call sites (%s) issue_queued=%s",
            impact.id,
            repo.full_name,
            len(matched),
            "created" if created else "existing",
            queued,
        )
        impacts.append(impact)
    session.commit()
    return impacts


# --------------------------------------------------------------------------------------
# Drafts -> issues (settings page / operator)
# --------------------------------------------------------------------------------------


def pending_impacts(
    session: Session, repo_id: UUID | str | None = None, *, limit: int = 200
) -> list[Impact]:
    stmt = (
        select(Impact)
        .join(Classification, Classification.id == Impact.classification_id)
        .where(
            Impact.status == "detected",
            Impact.github_issue_number.is_(None),
            Classification.severity.in_(list(ACTIONABLE_SEVERITIES)),
        )
        .order_by(Impact.created_at)
        .limit(limit)
    )
    if repo_id is not None:
        stmt = stmt.where(Impact.repo_id == UUID(str(repo_id)))
    return list(session.scalars(stmt))


def open_issues_for_pending(
    session: Session, repo_id: UUID | str | None = None, *, enqueue_only: bool = False
) -> list[Impact]:
    """Turn draft impacts (status ``detected``) into real GitHub issues, for one repo or
    all. Used by the settings page / an operator once classifications are trusted.
    Returns the impacts that got an issue (or were queued with ``enqueue_only``)."""
    out: list[Impact] = []
    for impact in pending_impacts(session, repo_id):
        if enqueue_only:
            enqueue_open_issue(session, impact)
            out.append(impact)
            continue
        try:
            create_issue_for_impact(session, impact)
            maybe_enqueue_fix(session, impact)
            session.commit()
            out.append(impact)
        except Exception:  # noqa: BLE001 - one repo's GitHub trouble must not stop the rest
            session.rollback()
            log.exception("could not open issue for impact %s", impact.id)
    if enqueue_only:
        session.commit()
    return out


# --------------------------------------------------------------------------------------
# Job handlers
# --------------------------------------------------------------------------------------


def _uuid_from(payload: dict[str, Any], key: str, job: str) -> UUID:
    raw = payload.get(key)
    if not raw:
        raise NonRetryable(f"{job}: payload has no {key}")
    try:
        return UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        raise NonRetryable(f"{job}: bad {key} {raw!r}")


@job_handler(JOB_MATCH_EVENT)
def handle_match_event(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    change_event_id = _uuid_from(payload, "change_event_id", "match_event")
    row = session.get(ChangeEventRow, change_event_id)
    if row is None:
        raise NonRetryable(f"match_event: change_event {change_event_id} does not exist")
    impacts = match_change_event(session, row)
    return {
        "change_event_id": str(change_event_id),
        "impacts": [str(i.id) for i in impacts],
        "repos": [str(i.repo_id) for i in impacts],
    }


@job_handler(JOB_OPEN_ISSUE)
def handle_open_issue(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    impact_id = _uuid_from(payload, "impact_id", "open_issue")
    impact = session.get(Impact, impact_id)
    if impact is None:
        raise NonRetryable(f"open_issue: impact {impact_id} does not exist")
    if impact.status not in OPEN_STATUSES and not impact.github_issue_number:
        log.info("open_issue: impact %s is %s; skipping", impact.id, impact.status)
        return {"impact_id": str(impact_id), "skipped": impact.status}
    try:
        number = create_issue_for_impact(session, impact)
    except ValueError as e:  # missing repo / org / classification: nothing to retry
        raise NonRetryable(str(e)) from e
    fix_queued = maybe_enqueue_fix(session, impact)
    session.commit()
    return {
        "impact_id": str(impact_id),
        "issue_number": number,
        "status": impact.status,
        "fix_queued": fix_queued,
    }


__all__ = [
    "OPEN_STATUSES",
    "after_match",
    "candidate_dependencies",
    "dependency_vendor",
    "ensure_impact",
    "enqueue_open_issue",
    "fix_impact_dedupe_key",
    "handle_match_event",
    "handle_open_issue",
    "is_package_event",
    "issues_may_open",
    "match_call_sites",
    "match_change_event",
    "maybe_enqueue_fix",
    "open_issue_dedupe_key",
    "open_issues_for_pending",
    "pending_impacts",
    "site_record",
]
