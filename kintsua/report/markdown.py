"""The weekly drift report: a Pydantic model plus Markdown and RSS renderers.

Dependency-light so the CLI (``kintsua watch report``), the hosted job
(``python -m kintsua.report.run``) and the public page (``GET /report/weekly``) all use the
same renderer. ``kintsua.report.build`` fills the model from Postgres.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

SEVERITY_ORDER = ["breaking", "degraded", "unknown", "safe"]
SEVERITY_EMOJI = {"breaking": "🔴", "degraded": "🟠", "unknown": "⚪", "safe": "🟢"}


class ReportItem(BaseModel):
    source_name: str
    source_kind: str
    vendor: str | None = None
    severity: str
    category: str
    summary: str
    detected_at: datetime
    affected_paths: list[str] = Field(default_factory=list)
    from_version: str | None = None
    to_version: str | None = None
    migration_hint: str | None = None
    needs_human: bool = False
    rule_hits: list[str] = Field(default_factory=list)
    llm_confidence: float | None = None
    op_count: int = 0
    url: str | None = None  # link to an issue / PR / change detail
    change_event_id: str | None = None


class FailingSource(BaseModel):
    display_name: str
    kind: str
    locator: str
    last_error: str | None = None
    failing_since: datetime | None = None
    consecutive_failures: int = 0


class ReportStats(BaseModel):
    sources_watched: int = 0
    sources_polled: int = 0
    change_events: int = 0
    breaking: int = 0
    degraded: int = 0
    safe: int = 0
    unknown: int = 0
    needs_human: int = 0
    repos_installed: int = 0
    repos_active: int = 0
    issues_opened: int = 0
    prs_opened: int = 0
    prs_merged: int = 0
    prs_merged_as_is: int = 0
    merged_as_is_rate: float | None = None
    runtime_events: int = 0
    runtime_mismatches: int = 0


class WeeklyReport(BaseModel):
    title: str = "Kintsua weekly drift report"
    since: datetime
    until: datetime
    generated_at: datetime
    items: list[ReportItem] = Field(default_factory=list)
    failing_sources: list[FailingSource] = Field(default_factory=list)
    stats: ReportStats = Field(default_factory=ReportStats)
    notes: list[str] = Field(default_factory=list)

    def by_severity(self) -> dict[str, list[ReportItem]]:
        out: dict[str, list[ReportItem]] = {s: [] for s in SEVERITY_ORDER}
        for it in self.items:
            out.setdefault(it.severity, []).append(it)
        for v in out.values():
            v.sort(key=lambda i: (i.vendor or i.source_name, i.detected_at))
        return out


def _fmt_dt(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d") if d else "-"


def _version_span(it: ReportItem) -> str:
    if it.from_version or it.to_version:
        return f"{it.from_version or '?'} → {it.to_version or '?'}"
    return ""


def render_markdown(report: WeeklyReport, *, include_safe: bool = True, max_paths: int = 6) -> str:
    """Every change event by severity, a 'needs a human look' section, failing sources, and
    the headline numbers the application is written from."""
    st = report.stats
    lines: list[str] = []
    lines.append(f"# {report.title}")
    lines.append("")
    lines.append(
        f"_{_fmt_dt(report.since)} to {_fmt_dt(report.until)} · generated {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}_"
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(f"| Sources watched | {st.sources_watched} |")
    lines.append(f"| Change events | {st.change_events} |")
    lines.append(f"| 🔴 Breaking | {st.breaking} |")
    lines.append(f"| 🟠 Degraded | {st.degraded} |")
    lines.append(f"| 🟢 Safe | {st.safe} |")
    lines.append(f"| ⚪ Needs a human look | {st.needs_human} |")
    if st.repos_installed:
        lines.append(
            f"| Repos installed | {st.repos_installed} ({st.repos_active} active this week) |"
        )
    if st.issues_opened or st.prs_opened:
        lines.append(f"| Issues opened | {st.issues_opened} |")
        lines.append(f"| PRs opened / merged | {st.prs_opened} / {st.prs_merged} |")
        rate = f"{st.merged_as_is_rate:.0%}" if st.merged_as_is_rate is not None else "n/a"
        lines.append(f"| Merged as-is rate | {rate} |")
    if st.runtime_events:
        lines.append(
            f"| Runtime events / schema mismatches | {st.runtime_events} / {st.runtime_mismatches} |"
        )
    lines.append("")

    groups = report.by_severity()
    for sev in SEVERITY_ORDER:
        items = [i for i in groups.get(sev, []) if not (sev == "unknown" and i.needs_human)]
        if sev == "safe" and not include_safe:
            continue
        if not items:
            continue
        lines.append(f"## {SEVERITY_EMOJI.get(sev, '')} {sev.capitalize()} ({len(items)})")
        lines.append("")
        for it in items:
            lines.extend(_render_item(it, max_paths))
        lines.append("")

    human = [i for i in report.items if i.needs_human]
    if human:
        lines.append(f"## ⚪ Needs a human look ({len(human)})")
        lines.append("")
        lines.append(
            "_Rules did not cover these and the model was not confident; no issues were opened._"
        )
        lines.append("")
        for it in human:
            lines.extend(_render_item(it, max_paths))
        lines.append("")

    if report.failing_sources:
        lines.append(f"## Sources failing for 7+ days ({len(report.failing_sources)})")
        lines.append("")
        lines.append("_A vanished spec is itself a signal._")
        lines.append("")
        lines.append("| Source | Kind | Failing since | Last error |")
        lines.append("|---|---|---|---|")
        for f in report.failing_sources:
            err = (f.last_error or "").splitlines()[0][:120] if f.last_error else ""
            lines.append(f"| {f.display_name} | {f.kind} | {_fmt_dt(f.failing_since)} | {err} |")
        lines.append("")

    if report.notes:
        lines.append("## Notes")
        lines.append("")
        lines.extend(f"- {n}" for n in report.notes)
        lines.append("")

    lines.append("---")
    lines.append(
        "_Generated by [Kintsua](https://github.com/hr7657316/Kintusa-Ai). Install the GitHub App to get an issue and a tested fix PR when one of these touches your code._"
    )
    return "\n".join(lines).rstrip() + "\n"


def _render_item(it: ReportItem, max_paths: int) -> list[str]:
    head = f"**{it.vendor or it.source_name}** · {it.source_kind}"
    span = _version_span(it)
    if span:
        head += f" · {span}"
    head += f" · {it.detected_at.strftime('%b %d')}"
    out = [f"- {head}", f"  - {it.category}: {it.summary}"]
    if it.affected_paths:
        shown = ", ".join(f"`{p}`" for p in it.affected_paths[:max_paths])
        more = (
            f" (+{len(it.affected_paths) - max_paths} more)"
            if len(it.affected_paths) > max_paths
            else ""
        )
        out.append(f"  - affects: {shown}{more}")
    if it.migration_hint:
        out.append(f"  - fix: {it.migration_hint}")
    meta = []
    if it.rule_hits:
        meta.append("rules: " + ", ".join(it.rule_hits[:4]))
    if it.llm_confidence is not None:
        meta.append(f"model confidence {it.llm_confidence:.2f}")
    if it.url:
        meta.append(f"[details]({it.url})")
    if meta:
        out.append("  - " + " · ".join(meta))
    return out


def render_rss(report: WeeklyReport, base_url: str) -> str:
    """RSS 2.0 for ``GET /report/weekly?format=rss``. Items are the non-safe change events."""
    from xml.sax.saxutils import escape

    items_xml = []
    for it in report.items:
        if it.severity == "safe":
            continue
        title = f"[{it.severity}] {it.vendor or it.source_name}: {it.summary}"
        desc = "\n".join(_render_item(it, 10))
        link = it.url or f"{base_url}/report/weekly"
        items_xml.append(
            "<item>"
            f"<title>{escape(title)}</title>"
            f"<link>{escape(link)}</link>"
            f'<guid isPermaLink="false">{escape(it.change_event_id or title)}</guid>'
            f"<pubDate>{it.detected_at.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
            f"<description>{escape(desc)}</description>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f"<title>{escape(report.title)}</title>"
        f"<link>{escape(base_url)}/report/weekly</link>"
        "<description>Breaking changes detected across watched APIs and MCP servers</description>"
        f"<lastBuildDate>{report.generated_at.strftime('%a, %d %b %Y %H:%M:%S +0000')}</lastBuildDate>"
        + "".join(items_xml)
        + "</channel></rss>"
    )


def to_dict(report: WeeklyReport) -> dict[str, Any]:
    return report.model_dump(mode="json")
