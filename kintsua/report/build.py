"""Fill a ``WeeklyReport`` from Postgres: every change event in the window by severity, the
"needs a human look" residue, sources failing for 7+ days and the headline numbers.

``build_weekly_report(session, since, until) -> WeeklyReport`` is the seam used by the
``weekly_report`` job, the public ``/report/weekly`` page and ``python -m kintsua.watchers.run
--report``.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kintsua.core.models import (
    ChangeEventRow,
    Classification,
    Impact,
    Repo,
    RuntimeEvent,
    Source,
)
from kintsua.core.util import utcnow
from kintsua.report.markdown import ReportItem, ReportStats, WeeklyReport
from kintsua.watchers.run import failing_sources

log = logging.getLogger(__name__)

UNCLASSIFIED_CATEGORY = "unclassified"


def _count(session: Session, stmt: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


def _ops_summary(diff: dict[str, Any]) -> str:
    ops = diff.get("ops") or []
    total = int(diff.get("ops_total") or len(ops))
    if not ops:
        if diff.get("text"):
            return "text changed (awaiting classification)"
        return "no structural changes recorded"
    counts = Counter(str(op.get("op")) for op in ops if isinstance(op, dict))
    parts = ", ".join(f"{n} {kind}" for kind, n in counts.most_common(4))
    return f"{total} change{'s' if total != 1 else ''} ({parts}); awaiting classification"


def _affected_paths(diff: dict[str, Any], limit: int = 12) -> list[str]:
    out: list[str] = []
    for op in diff.get("ops") or []:
        if isinstance(op, dict) and op.get("path") and op["path"] not in out:
            out.append(str(op["path"]))
        if len(out) >= limit:
            break
    return out


def _item_for(
    row: ChangeEventRow,
    source: Source | None,
    cls: Classification | None,
    impact: Impact | None,
    repo_name: str | None,
) -> ReportItem:
    diff = row.diff or {}
    url: str | None = None
    if impact is not None and repo_name:
        if impact.github_pr_number:
            url = f"https://github.com/{repo_name}/pull/{impact.github_pr_number}"
        elif impact.github_issue_number:
            url = f"https://github.com/{repo_name}/issues/{impact.github_issue_number}"
    ops = diff.get("ops") or []
    op_count = int(diff.get("ops_total") or len(ops))
    if cls is None:
        return ReportItem(
            source_name=source.display_name if source else str(row.source_id),
            source_kind=str(diff.get("source_kind") or (source.kind if source else "unknown")),
            vendor=source.vendor if source else None,
            severity="unknown",
            category=UNCLASSIFIED_CATEGORY,
            summary=_ops_summary(diff),
            detected_at=row.detected_at,
            affected_paths=_affected_paths(diff),
            from_version=diff.get("from_version"),
            to_version=diff.get("to_version"),
            op_count=op_count,
            url=url,
            change_event_id=str(row.id),
        )
    return ReportItem(
        source_name=source.display_name if source else str(row.source_id),
        source_kind=str(diff.get("source_kind") or (source.kind if source else "unknown")),
        vendor=source.vendor if source else None,
        severity=cls.severity,
        category=cls.category,
        summary=cls.summary,
        detected_at=row.detected_at,
        affected_paths=[str(p) for p in (cls.affected_paths or [])],
        from_version=diff.get("from_version"),
        to_version=diff.get("to_version"),
        migration_hint=cls.migration_hint,
        needs_human=bool(cls.needs_human),
        rule_hits=[str(r) for r in (cls.rule_hits or [])],
        llm_confidence=cls.llm_confidence,
        op_count=op_count,
        url=url,
        change_event_id=str(row.id),
    )


def build_weekly_report(session: Session, since: datetime, until: datetime) -> WeeklyReport:
    """One ``ReportItem`` per change event detected in ``[since, until)`` (unclassified ones
    as severity ``unknown``), stats from the tables, and the sources failing for 7+ days."""
    stats = ReportStats()

    # --- sources ----------------------------------------------------------------------
    stats.sources_watched = _count(session, select(Source.id).where(Source.active.is_(True)))
    stats.sources_polled = _count(
        session,
        select(Source.id).where(Source.last_polled_at >= since, Source.last_polled_at < until),
    )

    # --- change events + classifications ------------------------------------------------
    rows = session.execute(
        select(ChangeEventRow, Source, Classification)
        .join(Source, Source.id == ChangeEventRow.source_id, isouter=True)
        .join(
            Classification,
            Classification.change_event_id == ChangeEventRow.id,
            isouter=True,
        )
        .where(ChangeEventRow.detected_at >= since, ChangeEventRow.detected_at < until)
        .order_by(ChangeEventRow.detected_at.desc())
    ).all()
    event_ids = [row[0].id for row in rows]
    impacts_by_event: dict[Any, tuple[Impact, str]] = {}
    if event_ids:
        for impact, repo_name in session.execute(
            select(Impact, Repo.full_name)
            .join(Repo, Repo.id == Impact.repo_id)
            .where(Impact.change_event_id.in_(event_ids))
            .order_by(Impact.created_at)
        ).all():
            impacts_by_event.setdefault(impact.change_event_id, (impact, repo_name))

    items: list[ReportItem] = []
    unclassified = 0
    for row, source, cls in rows:
        impact, repo_name = impacts_by_event.get(row.id, (None, None))
        item = _item_for(row, source, cls, impact, repo_name)
        items.append(item)
        stats.change_events += 1
        if cls is None:
            unclassified += 1
            stats.unknown += 1
            continue
        if cls.severity == "breaking":
            stats.breaking += 1
        elif cls.severity == "degraded":
            stats.degraded += 1
        elif cls.severity == "safe":
            stats.safe += 1
        else:
            stats.unknown += 1
        if cls.needs_human:
            stats.needs_human += 1

    # --- repos ------------------------------------------------------------------------
    stats.repos_installed = _count(session, select(Repo.id).where(Repo.active.is_(True)))
    active_repo_ids: set[Any] = set()
    active_repo_ids.update(
        session.scalars(
            select(Impact.repo_id).where(Impact.created_at >= since, Impact.created_at < until)
        )
    )
    active_repo_ids.update(
        session.scalars(
            select(RuntimeEvent.repo_id).where(
                RuntimeEvent.observed_at >= since, RuntimeEvent.observed_at < until
            )
        )
    )
    active_repo_ids.update(
        session.scalars(
            select(Repo.id).where(
                Repo.active.is_(True), Repo.indexed_at >= since, Repo.indexed_at < until
            )
        )
    )
    stats.repos_active = len(active_repo_ids)

    # --- issues / PRs ---------------------------------------------------------------------
    stats.issues_opened = _count(
        session,
        select(Impact.id).where(
            Impact.github_issue_number.is_not(None),
            Impact.created_at >= since,
            Impact.created_at < until,
        ),
    )
    stats.prs_opened = _count(
        session,
        select(Impact.id).where(
            Impact.github_pr_number.is_not(None),
            Impact.created_at >= since,
            Impact.created_at < until,
        ),
    )
    merged_window = select(Impact.id).where(
        Impact.pr_outcome.in_(["merged_as_is", "merged_edited"]),
        Impact.resolved_at >= since,
        Impact.resolved_at < until,
    )
    stats.prs_merged = _count(session, merged_window)
    stats.prs_merged_as_is = _count(
        session,
        select(Impact.id).where(
            Impact.pr_outcome == "merged_as_is",
            Impact.resolved_at >= since,
            Impact.resolved_at < until,
        ),
    )
    stats.merged_as_is_rate = (
        stats.prs_merged_as_is / stats.prs_merged if stats.prs_merged else None
    )

    # --- runtime -----------------------------------------------------------------------
    stats.runtime_events = _count(
        session,
        select(RuntimeEvent.id).where(
            RuntimeEvent.observed_at >= since, RuntimeEvent.observed_at < until
        ),
    )
    stats.runtime_mismatches = _count(
        session,
        select(RuntimeEvent.id).where(
            RuntimeEvent.outcome == "schema_mismatch",
            RuntimeEvent.observed_at >= since,
            RuntimeEvent.observed_at < until,
        ),
    )

    notes: list[str] = []
    if unclassified:
        notes.append(
            f"{unclassified} change event{'s' if unclassified != 1 else ''} not yet classified "
            "(listed under ⚪ Unknown)."
        )
    failing = failing_sources(session)
    if failing:
        notes.append(
            f"{len(failing)} source{'s' if len(failing) != 1 else ''} failing for 7+ days."
        )

    return WeeklyReport(
        title=f"Kintsua weekly drift report · week of {since.strftime('%Y-%m-%d')}",
        since=since,
        until=until,
        generated_at=utcnow(),
        items=items,
        failing_sources=failing,
        stats=stats,
        notes=notes,
    )
