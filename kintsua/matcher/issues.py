"""GitHub issues for impacts: body rendering, idempotent creation, comments, closing.

Everything fetched from vendors (summaries, migration hints, op before/after values,
tool descriptions) is rendered as plain text: Markdown that could hide or restyle
content is escaped and ``<``/``>`` become entities so no HTML survives. Kintsua's own
marker (``<!-- kintsua:change_event:<id> -->``) is the only HTML comment in a body and is
what makes creation idempotent across worker retries.

All GitHub writes go through ``get_github_app().installation_client(...)`` under the
per-org ``write_lock``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from kintsua.core.github import (
    ISSUE_PREFIX,
    LABEL,
    LABEL_BREAKING,
    LABEL_DEGRADED,
    get_github_app,
)
from kintsua.core.models import ChangeEventRow, Classification, Impact, Org, Repo, Source
from kintsua.core.util import utcnow
from kintsua.matcher.operations import operations_match, pointer_operation

log = logging.getLogger(__name__)

FIXABLE_CATEGORIES: frozenset[str] = frozenset({"rename", "required_param", "deprecation"})
ACTIONABLE_SEVERITIES: tuple[str, ...] = ("breaking", "degraded")
FIX_MODES: tuple[str, ...] = ("pr", "issue_only")
TITLE_MAX = 240
MAX_OPS_SHOWN = 15
MAX_SITES_SHOWN = 60
MAX_VALUE_CHARS = 200
RUNTIME_FILE_PATH = "<runtime>"  # any file_path starting with "<" is a pseudo path
PUSH_SITE_SYMBOL = "kintsua watch --push"  # symbol of the call sites a pushed snapshot adds
RUNTIME_COMMENT_MARKER = "<!-- kintsua:runtime -->"

LABEL_COLOURS: dict[str, tuple[str, str]] = {
    LABEL: ("5319e7", "Opened by Kintsua"),
    LABEL_BREAKING: ("b60205", "Breaking upstream change (Kintsua)"),
    LABEL_DEGRADED: ("d93f0b", "Degraded / deprecated upstream (Kintsua)"),
}

_MD_ESCAPE_RE = re.compile(r"([\\`*_\[\]#|~])")
_LINE_START_RE = re.compile(r"^(\s*)([-+]|\d+\.)(\s)")
_ZWSP = "\u200b"  # zero-width space: breaks GitHub @mentions and autolinks, invisible
_WWW_RE = re.compile(r"\b(www)\.", re.IGNORECASE)


# --------------------------------------------------------------------------------------
# Plain-text rendering of untrusted text
# --------------------------------------------------------------------------------------


def plain(text: Any, *, max_len: int | None = None) -> str:
    """Render untrusted text as inert Markdown on one line: no HTML, no emphasis, no
    links (bare URLs and ``www.`` are not autolinked), no ``@mentions`` (vendor release
    notes are full of ``by @user in <url>``), no headings, no list items."""
    s = "" if text is None else str(text)
    s = s.replace("\r", "").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"\s{2,}", " ", s).strip()
    if max_len is not None and len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = _MD_ESCAPE_RE.sub(r"\\\1", s)
    s = _LINE_START_RE.sub(r"\1\\\2\3", s)
    s = s.replace("@", "@" + _ZWSP).replace("://", ":" + _ZWSP + "//")
    s = _WWW_RE.sub(r"\1" + _ZWSP + ".", s)
    return s


def code(text: Any, *, max_len: int | None = MAX_VALUE_CHARS) -> str:
    """An inline code span whose content cannot break out of it (no backticks, no
    newlines, pipes escaped for tables)."""
    s = "" if text is None else str(text)
    s = s.replace("\r", "").replace("\n", " ").replace("`", "'").replace("|", "\\|").strip()
    if max_len is not None and len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return f"`{s}`" if s else "`(empty)`"


def _compact(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _fmt_dt(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d %H:%M UTC") if d else "unknown time"


# --------------------------------------------------------------------------------------
# Policy helpers shared with match.py / runtime.py
# --------------------------------------------------------------------------------------


def effective_fix_mode(
    repo_config: dict[str, Any] | None, org_settings: dict[str, Any] | None
) -> str:
    """Repo ``fix.mode`` if set, else the org default, else ``issue_only``."""
    fix = (repo_config or {}).get("fix") or {}
    mode = fix.get("mode") if isinstance(fix, dict) else None
    if mode in FIX_MODES:
        return str(mode)
    org_mode = (org_settings or {}).get("fix_mode")
    if org_mode in FIX_MODES:
        return str(org_mode)
    return "issue_only"


def is_fixable(classification: Classification | None) -> bool:
    return (
        classification is not None
        and classification.category in FIXABLE_CATEGORIES
        and classification.severity in ACTIONABLE_SEVERITIES
    )


def is_pseudo_path(file_path: Any) -> bool:
    """A call-site ``file_path`` that is not a file in the repo: empty, or a pseudo path such
    as ``<runtime>`` (runtime evidence, pushed snapshots)."""
    return not file_path or str(file_path).startswith("<")


def has_file_located_site(sites: Any) -> bool:
    """Does any matched call-site record point at a file in the repo? Pseudo-path
    (``<runtime>``, ...) sites and package impacts without call sites give the fixer nothing
    to patch."""
    return any(isinstance(s, dict) and not is_pseudo_path(s.get("file_path")) for s in sites or [])


def change_event_marker(change_event_id: Any) -> str:
    return f"<!-- kintsua:change_event:{change_event_id} -->"


def severity_label(severity: str | None) -> str | None:
    if severity == "breaking":
        return LABEL_BREAKING
    if severity == "degraded":
        return LABEL_DEGRADED
    return None


# --------------------------------------------------------------------------------------
# Body
# --------------------------------------------------------------------------------------


def _source_for(change_event_row: ChangeEventRow, source: Source | None) -> Source | None:
    if source is not None:
        return source
    sess = object_session(change_event_row)
    if sess is None:
        return None
    try:
        return sess.get(Source, change_event_row.source_id)
    except Exception:  # noqa: BLE001 - detached rows; the body degrades gracefully
        return None


def _ops_of(change_event_row: ChangeEventRow) -> list[dict[str, Any]]:
    diff = change_event_row.diff or {}
    ops = diff.get("ops")
    return [o for o in ops if isinstance(o, dict)] if isinstance(ops, list) else []


def relevant_ops(ops: list[dict[str, Any]], operations: list[str]) -> list[dict[str, Any]]:
    """Ops whose pointer refers to one of the matched operations; all ops when the pointer
    dialect carries no operation (package events) or nothing is matched."""
    if not operations:
        return ops
    picked: list[dict[str, Any]] = []
    for op in ops:
        ptr_op = pointer_operation(str(op.get("path") or ""))
        if ptr_op is None:
            picked.append(op)
            continue
        if any(operations_match(site, ptr_op) for site in operations):
            picked.append(op)
    return picked or ops


def blob_url(repo: Repo, file_path: str, line: int | None) -> str | None:
    """The GitHub link to ``file_path``; None for a pseudo path (``<runtime>``, ...)."""
    if is_pseudo_path(file_path):
        return None
    path = quote(file_path.lstrip("/"), safe="/")
    url = f"https://github.com/{repo.full_name}/blob/{repo.default_branch or 'main'}/{path}"
    if line:
        url += f"#L{int(line)}"
    return url


def _site_row(repo: Repo, site: dict[str, Any]) -> str:
    fp = str(site.get("file_path") or "")
    line = site.get("line") or 0
    symbol = site.get("symbol") or ""
    operation = site.get("operation") or ""
    if symbol == PUSH_SITE_SYMBOL and is_pseudo_path(fp):
        where = f"pushed by {code(PUSH_SITE_SYMBOL)}"
        symbol = ""
    elif fp == RUNTIME_FILE_PATH or not fp:
        where = "runtime (observed by the SDK)"
    elif is_pseudo_path(fp):
        where = plain(f"{fp}:{line}" if line else fp)
    else:
        label = f"{fp}:{line}" if line else fp
        url = blob_url(repo, fp, int(line) if line else None)
        where = f"[{plain(label)}]({url})" if url else plain(label)
    return (
        f"| {where} | {code(symbol) if symbol else '-'} | {code(operation) if operation else '-'} |"
    )


def build_issue_body(
    impact: Impact,
    change_event_row: ChangeEventRow,
    classification: Classification,
    repo: Repo,
    *,
    source: Source | None = None,
    org: Org | None = None,
) -> tuple[str, str]:
    """``(title, body)`` for the issue of ``impact``."""
    source = _source_for(change_event_row, source)
    org = org or getattr(repo, "org", None)
    diff = change_event_row.diff or {}
    source_kind = str(diff.get("source_kind") or (source.kind if source else "unknown"))
    display = (
        (source.display_name or source.vendor or source.locator) if source else "Unknown source"
    )
    vendor = (source.vendor if source else None) or ""
    sites = [s for s in (impact.matched_call_sites or []) if isinstance(s, dict)]
    operations = [str(s.get("operation")) for s in sites if s.get("operation")]

    summary = (classification.summary or "").strip() or "Upstream change"
    title = f"{ISSUE_PREFIX} {re.sub(r'\s+', ' ', summary)}"
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1].rstrip() + "…"

    lines: list[str] = [change_event_marker(change_event_row.id), ""]

    # ---- What changed upstream
    lines.append("## What changed upstream")
    lines.append("")
    kind_label = "observed at runtime" if source_kind == "observed" else source_kind
    from_v, to_v = diff.get("from_version"), diff.get("to_version")
    if from_v or to_v:
        span = f"{code(from_v or '?')} → {code(to_v or '?')}"
    else:
        span = "version unknown"
    lines.append(
        f"**{plain(display)}** ({plain(kind_label)}) · {span} · detected {_fmt_dt(change_event_row.detected_at)}"
    )
    lines.append("")
    lines.append(
        f"**{plain(classification.severity)}** · {plain(classification.category)} · {plain(summary)}"
    )
    lines.append("")
    ops = relevant_ops(_ops_of(change_event_row), operations)
    if ops:
        for op in ops[:MAX_OPS_SHOWN]:
            kind = plain(op.get("op") or "changed")
            path = code(op.get("path") or "/")
            before = code(_compact(op.get("before")))
            after = code(_compact(op.get("after")))
            lines.append(f"- {kind} at {path}: {before} → {after}")
        if len(ops) > MAX_OPS_SHOWN:
            lines.append(f"- _+{len(ops) - MAX_OPS_SHOWN} more_")
    else:
        lines.append("_No structured diff; see the summary above._")
    text = diff.get("text")
    if isinstance(text, str) and text.strip() and source_kind in ("changelog", "pypi", "npm"):
        lines.append("")
        lines.append(f"Upstream notes (quoted as data): {plain(text, max_len=600)}")
    lines.append("")

    # ---- Where this repo calls it
    lines.append("## Where this repo calls it")
    lines.append("")
    if sites:
        lines.append("| File | Symbol | Operation |")
        lines.append("|---|---|---|")
        for site in sites[:MAX_SITES_SHOWN]:
            lines.append(_site_row(repo, site))
        if len(sites) > MAX_SITES_SHOWN:
            lines.append("")
            lines.append(f"_+{len(sites) - MAX_SITES_SHOWN} more call sites_")
    else:
        lines.append(
            "_No call sites recorded; the dependency is declared in this repo but the indexer "
            "found no direct call (dynamic names or URLs from config). The runtime SDK can fill this in._"
        )
    lines.append("")

    # ---- Suggested migration
    lines.append("## Suggested migration")
    lines.append("")
    hint = (classification.migration_hint or "").strip()
    lines.append(plain(hint, max_len=2000) if hint else "none yet")
    lines.append("")

    # ---- Runtime evidence
    if (impact.runtime_error_count or 0) > 0:
        lines.append("## Runtime evidence")
        lines.append("")
        lines.append(f"**{int(impact.runtime_error_count)}** failed calls in the last hour.")
        lines.append("")

    # ---- Status
    lines.append("## Status")
    lines.append("")
    mode = effective_fix_mode(repo.config, org.settings if org is not None else None)
    if mode == "pr" and is_fixable(classification) and has_file_located_site(sites):
        lines.append("Kintsua will open a PR within an hour.")
    elif mode == "pr" and is_fixable(classification):
        lines.append(
            "Fix mode is pr, but no call sites were found in this repo's files; "
            "this stays an issue."
        )
    elif mode == "pr":
        lines.append(
            f"Fix mode is pr, but {plain(classification.category)} changes are not auto-fixed in v0; "
            "this stays an issue."
        )
    else:
        lines.append("Fix mode is issue_only; switch it in .kintsua.yml or the settings page.")
    lines.append("")
    lines.append("---")
    mute_target = (
        f"{vendor}:{operations[0]}"
        if vendor and operations
        else (operations[0] if operations else f"{vendor}:*")
    )
    lines.append(
        f"Opened by Kintsua · change event {code(str(change_event_row.id))} · mute: add "
        f"{code(mute_target)} to .kintsua.yml"
    )
    return title, "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------------------
# GitHub plumbing
# --------------------------------------------------------------------------------------


def _load(
    session: Session, impact: Impact
) -> tuple[Repo, Org, ChangeEventRow, Classification, Source | None]:
    repo = session.get(Repo, impact.repo_id)
    if repo is None:
        raise ValueError(f"impact {impact.id}: repo {impact.repo_id} does not exist")
    org = session.get(Org, repo.org_id)
    if org is None:
        raise ValueError(f"impact {impact.id}: org {repo.org_id} does not exist")
    row = session.get(ChangeEventRow, impact.change_event_id)
    if row is None:
        raise ValueError(
            f"impact {impact.id}: change_event {impact.change_event_id} does not exist"
        )
    cls: Classification | None = None
    if impact.classification_id is not None:
        cls = session.get(Classification, impact.classification_id)
    if cls is None:
        cls = session.scalar(select(Classification).where(Classification.change_event_id == row.id))
    if cls is None:
        raise ValueError(f"impact {impact.id}: change_event {row.id} has no classification")
    source = session.get(Source, row.source_id)
    return repo, org, row, cls, source


def ensure_labels(gh_repo: Any, names: list[str]) -> None:
    for name in names:
        try:
            gh_repo.get_label(name)
            continue
        except Exception:  # noqa: BLE001 - UnknownObjectException (real) / KeyError (fake)
            pass
        colour, description = LABEL_COLOURS.get(name, ("ededed", ""))
        try:
            gh_repo.create_label(name=name, color=colour, description=description)
        except Exception as e:  # noqa: BLE001 - a label race is not a failure
            log.info("could not create label %s: %s", name, e)


def find_issue_with_marker(gh_repo: Any, marker: str) -> Any | None:
    """An open Kintsua issue whose body carries ``marker`` (a retry after a crash must not
    open a second one)."""
    try:
        for issue in gh_repo.get_issues(state="open", labels=[LABEL]):
            if marker in (getattr(issue, "body", "") or ""):
                return issue
    except Exception as e:  # noqa: BLE001
        log.warning("listing issues on %s failed: %s", getattr(gh_repo, "full_name", "?"), e)
    return None


def _gh_repo(app: Any, org: Org, repo: Repo) -> Any:
    return app.installation_client(org.github_installation_id).get_repo(repo.full_name)


def create_issue_for_impact(session: Session, impact: Impact) -> int:
    """Open the GitHub issue for ``impact`` and return its number. Idempotent: an impact
    that already has a number returns it; an open issue carrying this change event's
    marker is reused. Sets ``status='issued'`` and commits."""
    if impact.github_issue_number:
        return int(impact.github_issue_number)
    repo, org, row, cls, source = _load(session, impact)
    title, body = build_issue_body(impact, row, cls, repo, source=source, org=org)
    labels = [LABEL]
    sev = severity_label(cls.severity)
    if sev:
        labels.append(sev)
    marker = change_event_marker(row.id)

    app = get_github_app()
    with app.write_lock(org.github_installation_id):
        gh_repo = _gh_repo(app, org, repo)
        existing = find_issue_with_marker(gh_repo, marker)
        if existing is not None:
            number = int(existing.number)
            log.info("impact %s: reusing issue #%s on %s", impact.id, number, repo.full_name)
        else:
            ensure_labels(gh_repo, labels)
            issue = gh_repo.create_issue(title=title, body=body, labels=labels)
            number = int(issue.number)
            log.info("impact %s: opened issue #%s on %s", impact.id, number, repo.full_name)

    impact.github_issue_number = number
    if impact.status == "detected":
        impact.status = "issued"
    session.commit()
    return number


def _issue_for(session: Session, impact: Impact) -> tuple[Any, Any] | None:
    """``(app, issue)`` for the impact's GitHub issue, or None when it has none."""
    if not impact.github_issue_number:
        return None
    repo = session.get(Repo, impact.repo_id)
    org = session.get(Org, repo.org_id) if repo is not None else None
    if repo is None or org is None:
        return None
    app = get_github_app()
    gh_repo = _gh_repo(app, org, repo)
    return app, gh_repo.get_issue(int(impact.github_issue_number))


def comment_on_impact(session: Session, impact: Impact, body: str) -> None:
    """Add a comment to the impact's issue; a no-op when there is no issue."""
    if not impact.github_issue_number:
        return
    repo = session.get(Repo, impact.repo_id)
    org = session.get(Org, repo.org_id) if repo is not None else None
    if repo is None or org is None:
        return
    app = get_github_app()
    with app.write_lock(org.github_installation_id):
        issue = _gh_repo(app, org, repo).get_issue(int(impact.github_issue_number))
        issue.create_comment(body)


def _list_comments(issue: Any) -> list[Any]:
    getter = getattr(issue, "get_comments", None)
    if callable(getter):
        try:
            return list(getter())
        except Exception as e:  # noqa: BLE001
            log.info("listing comments on issue #%s failed: %s", getattr(issue, "number", "?"), e)
            return []
    return list(getattr(issue, "comments", None) or [])


def _edit_comment(comment: Any, body: str) -> None:
    editor = getattr(comment, "edit", None)
    if callable(editor):
        editor(body)
        return
    comment.body = body  # minimal fakes without ``edit``


def upsert_impact_comment(session: Session, impact: Impact, marker: str, body: str) -> str | None:
    """Create one comment carrying ``marker`` and edit it on later calls. Returns
    ``"created"``, ``"edited"`` or None (no issue)."""
    if not impact.github_issue_number:
        return None
    repo = session.get(Repo, impact.repo_id)
    org = session.get(Org, repo.org_id) if repo is not None else None
    if repo is None or org is None:
        return None
    if marker not in body:
        body = f"{marker}\n{body}"
    app = get_github_app()
    with app.write_lock(org.github_installation_id):
        issue = _gh_repo(app, org, repo).get_issue(int(impact.github_issue_number))
        for comment in _list_comments(issue):
            if marker in (getattr(comment, "body", "") or ""):
                _edit_comment(comment, body)
                return "edited"
        issue.create_comment(body)
        return "created"


def close_impact_issue(session: Session, impact: Impact, reason: str) -> None:
    """Close the impact's issue with a comment saying why and mark the impact dismissed
    (``resolved_at`` set). Impacts already in a terminal state keep their status."""
    body = f"Kintsua closed this: {plain(reason, max_len=1000)}"
    repo = session.get(Repo, impact.repo_id)
    org = session.get(Org, repo.org_id) if repo is not None else None
    if impact.github_issue_number and repo is not None and org is not None:
        app = get_github_app()
        with app.write_lock(org.github_installation_id):
            issue = _gh_repo(app, org, repo).get_issue(int(impact.github_issue_number))
            issue.create_comment(body)
            issue.edit(state="closed")
    if impact.status not in ("merged", "failed", "dismissed"):
        impact.status = "dismissed"
    impact.fix_reason = impact.fix_reason or reason[:4000]
    impact.resolved_at = impact.resolved_at or utcnow()
    session.commit()


__all__ = [
    "ACTIONABLE_SEVERITIES",
    "FIXABLE_CATEGORIES",
    "PUSH_SITE_SYMBOL",
    "RUNTIME_COMMENT_MARKER",
    "RUNTIME_FILE_PATH",
    "blob_url",
    "build_issue_body",
    "change_event_marker",
    "close_impact_issue",
    "code",
    "comment_on_impact",
    "create_issue_for_impact",
    "effective_fix_mode",
    "ensure_labels",
    "find_issue_with_marker",
    "has_file_located_site",
    "is_fixable",
    "is_pseudo_path",
    "plain",
    "relevant_ops",
    "severity_label",
    "upsert_impact_comment",
]
