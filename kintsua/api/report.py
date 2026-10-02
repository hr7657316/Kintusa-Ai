"""The public weekly drift report: ``GET /report/weekly`` (HTML, ``?format=md|rss|json``)
and ``POST /report/subscribe``.

The page serves ``<data_dir>/reports/latest.md`` + ``latest.json`` written by the
``weekly_report`` job when they exist, else it builds the last-7-days report on the fly and
caches it in-process for ten minutes. Markdown is turned into HTML by the ``markdown``
package when installed, otherwise by the small converter below (headings, lists, tables,
links, emphasis, code).
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kintsua.api.deps import get_db, read_body, render, wants_html
from kintsua.api.schemas import SubscribeBody
from kintsua.core.config import get_settings
from kintsua.core.models import Subscriber
from kintsua.core.util import utcnow
from kintsua.report.build import build_weekly_report
from kintsua.report.markdown import WeeklyReport, render_markdown, render_rss
from kintsua.report.run import read_latest, reports_dir

log = logging.getLogger(__name__)

router = APIRouter(tags=["report"])

CACHE_TTL_S = 600
REPORT_WINDOW = timedelta(days=7)
FORMATS = ("html", "md", "rss", "json")


# --------------------------------------------------------------------------------------
# Loading (files written by the job, else built on the fly and cached)
# --------------------------------------------------------------------------------------


class _Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.report: WeeklyReport | None = None
        self.markdown: str | None = None
        self.origin: str = ""
        self.key: str | None = None
        self.at: float = 0.0


_cache = _Cache()


def invalidate_cache() -> None:
    with _cache.lock:
        _cache.report = None
        _cache.markdown = None
        _cache.key = None
        _cache.at = 0.0


def _files_key(data_dir: Path) -> str | None:
    d = reports_dir(data_dir)
    md, js = d / "latest.md", d / "latest.json"
    if md.exists() and js.exists():
        return f"file:{md.stat().st_mtime_ns}:{js.stat().st_mtime_ns}"
    return None


def load_report(session: Session) -> tuple[WeeklyReport, str, str]:
    """``(report, markdown, origin)`` where origin is ``file`` (written by the weekly job)
    or ``built`` (on the fly, cached ten minutes)."""
    data_dir = Path(get_settings().data_dir)
    key = _files_key(data_dir)
    now = time.monotonic()
    with _cache.lock:
        if _cache.report is not None and _cache.markdown is not None:
            if key is not None and _cache.key == key:
                return _cache.report, _cache.markdown, _cache.origin
            if key is None and _cache.key is None and now - _cache.at < CACHE_TTL_S:
                return _cache.report, _cache.markdown, _cache.origin
    report: WeeklyReport | None = None
    markdown: str | None = None
    origin = "built"
    if key is not None:
        text, data = read_latest(data_dir)
        if text and data:
            try:
                report = WeeklyReport.model_validate(data)
                markdown = text
                origin = "file"
            except ValidationError as e:
                # keep the file key: the built report is cached until the file changes
                log.warning("latest.json is not a WeeklyReport (%s); rebuilding", e)
    if report is None or markdown is None:
        until = utcnow()
        report = build_weekly_report(session, until - REPORT_WINDOW, until)
        markdown = render_markdown(report)
        origin = "built"
    with _cache.lock:
        _cache.report, _cache.markdown, _cache.origin = report, markdown, origin
        _cache.key, _cache.at = key, now
    return report, markdown, origin


# --------------------------------------------------------------------------------------
# Markdown -> HTML
# --------------------------------------------------------------------------------------

_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])[_*](?=\S)(.+?)(?<=\S)[_*](?![\w*])")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_SAFE_HREF_RE = re.compile(r"^(https?://|mailto:|/|#)", re.IGNORECASE)


def _safe_href(url: str) -> str | None:
    url = url.strip()
    return html.escape(url, quote=True) if _SAFE_HREF_RE.match(url) else None


def render_inline(text: str) -> str:
    """Escape, then code spans, links, bold and italic."""
    escaped = html.escape(text, quote=False)
    spans: list[str] = []

    def _code(m: re.Match[str]) -> str:
        spans.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(spans) - 1}\x00"

    out = _CODE_SPAN_RE.sub(_code, escaped)

    def _link(m: re.Match[str]) -> str:
        href = _safe_href(html.unescape(m.group(2)))
        if href is None:
            return m.group(1)
        return f'<a href="{href}" rel="noopener">{m.group(1)}</a>'

    out = _LINK_RE.sub(_link, out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], out)


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = re.split(r"(?<!\\)\|", s)
    return [c.replace("\\|", "|").strip() for c in cells]


class _Converter:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.para: list[str] = []
        self.list_stack: list[tuple[int, str]] = []  # (indent, tag)
        self.table: list[list[str]] | None = None
        self.table_header: list[str] | None = None
        self.code: list[str] | None = None

    # -- helpers --------------------------------------------------------------------
    def flush_para(self) -> None:
        if self.para:
            self.out.append(f"<p>{render_inline(' '.join(self.para))}</p>")
            self.para = []

    def close_lists(self, to_indent: int = -1) -> None:
        while self.list_stack and self.list_stack[-1][0] > to_indent:
            _, tag = self.list_stack.pop()
            self.out.append(f"</li></{tag}>")

    def flush_table(self) -> None:
        if self.table is None:
            return
        rows = self.table
        parts = ["<table>"]
        if self.table_header is not None:
            parts.append(
                "<thead><tr>"
                + "".join(f"<th>{render_inline(c)}</th>" for c in self.table_header)
                + "</tr></thead>"
            )
        if rows:
            parts.append("<tbody>")
            for r in rows:
                parts.append("<tr>" + "".join(f"<td>{render_inline(c)}</td>" for c in r) + "</tr>")
            parts.append("</tbody>")
        parts.append("</table>")
        self.out.append("".join(parts))
        self.table = None
        self.table_header = None

    def flush_all(self) -> None:
        self.flush_para()
        self.close_lists()
        self.flush_table()

    # -- lines ----------------------------------------------------------------------
    def feed(self, line: str) -> None:
        if self.code is not None:
            if line.strip().startswith("```"):
                self.out.append(f"<pre><code>{html.escape(chr(10).join(self.code))}</code></pre>")
                self.code = None
            else:
                self.code.append(line)
            return
        stripped = line.strip()
        if stripped.startswith("```"):
            self.flush_all()
            self.code = []
            return
        if not stripped:
            self.flush_para()
            self.flush_table()
            if self.list_stack:
                self.close_lists()
            return
        if stripped.startswith("|") and "|" in stripped[1:]:
            self.flush_para()
            self.close_lists()
            if _TABLE_SEP_RE.match(stripped):
                if self.table is not None and self.table_header is None and self.table:
                    self.table_header = self.table.pop(0)
                return
            if self.table is None:
                self.table = []
            self.table.append(_split_row(stripped))
            return
        self.flush_table()
        m = _HEADING_RE.match(stripped)
        if m:
            self.flush_para()
            self.close_lists()
            level = len(m.group(1))
            self.out.append(f"<h{level}>{render_inline(m.group(2))}</h{level}>")
            return
        if _HR_RE.match(stripped):
            self.flush_para()
            self.close_lists()
            self.out.append("<hr>")
            return
        m = _LIST_RE.match(line)
        if m:
            self.flush_para()
            indent = len(m.group(1).expandtabs(4))
            tag = "ol" if m.group(2)[0].isdigit() else "ul"
            content = render_inline(m.group(3))
            if self.list_stack and indent > self.list_stack[-1][0]:
                self.out.append(f"<{tag}><li>{content}")
                self.list_stack.append((indent, tag))
                return
            self.close_lists(indent)
            if self.list_stack and self.list_stack[-1][0] == indent:
                self.out.append(f"</li><li>{content}")
            else:
                self.out.append(f"<{tag}><li>{content}")
                self.list_stack.append((indent, tag))
            return
        if self.list_stack:
            # continuation line inside the current item
            self.out.append(" " + render_inline(stripped))
            return
        self.para.append(stripped)

    def result(self) -> str:
        if self.code is not None:
            self.out.append(f"<pre><code>{html.escape(chr(10).join(self.code))}</code></pre>")
            self.code = None
        self.flush_all()
        return "\n".join(self.out)


def minimal_markdown(text: str) -> str:
    conv = _Converter()
    for line in text.splitlines():
        conv.feed(line)
    return conv.result()


def markdown_to_html(text: str) -> str:
    try:
        import markdown as _md  # type: ignore[import-not-found]
    except ImportError:
        return minimal_markdown(text)
    try:
        return _md.markdown(text, extensions=["tables", "fenced_code"])
    except Exception as e:  # noqa: BLE001 - fall back rather than 500
        log.warning("markdown package failed (%s); using the minimal converter", e)
        return minimal_markdown(text)


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------


def _negotiate(request: Request, fmt: str | None) -> str:
    if fmt:
        f = fmt.strip().lower()
        if f in ("markdown", "text"):
            f = "md"
        if f == "xml":
            f = "rss"
        if f not in FORMATS:
            raise HTTPException(
                status_code=400, detail=f"format must be one of {', '.join(FORMATS)}"
            )
        return f
    accept = request.headers.get("accept", "")
    if "application/rss+xml" in accept or "application/xml" in accept:
        return "rss"
    if "text/markdown" in accept:
        return "md"
    if "application/json" in accept and "text/html" not in accept:
        return "json"
    return "html"


@router.get("/report/weekly")
def weekly_report_page(
    request: Request, format: str | None = None, session: Session = Depends(get_db)
) -> Response:
    fmt = _negotiate(request, format)
    report, markdown, origin = load_report(session)
    base_url = get_settings().base_url.rstrip("/")
    if fmt == "md":
        return PlainTextResponse(markdown, media_type="text/markdown; charset=utf-8")
    if fmt == "json":
        return JSONResponse(report.model_dump(mode="json"))
    if fmt == "rss":
        return Response(
            render_rss(report, base_url), media_type="application/rss+xml; charset=utf-8"
        )
    return render(
        request,
        "report.html",
        report=report,
        body_html=markdown_to_html(markdown),
        origin=origin,
        subscribers=_subscriber_count(session),
    )


def _subscriber_count(session: Session) -> int:
    from sqlalchemy import func

    return int(session.scalar(select(func.count(Subscriber.id))) or 0)


def subscribe_email(session: Session, email: str) -> bool:
    """Idempotent: returns True when a new subscriber row was created."""
    existing = session.scalar(select(Subscriber).where(Subscriber.email == email))
    if existing is not None:
        return False
    try:
        with session.begin_nested():
            session.add(Subscriber(email=email))
            session.flush()
    except IntegrityError:
        return False
    return True


@router.post("/report/subscribe")
async def subscribe(request: Request, session: Session = Depends(get_db)) -> Response:
    body = await read_body(request, SubscribeBody)
    return await run_in_threadpool(_subscribe, request, session, body)


def _subscribe(request: Request, session: Session, body: SubscribeBody) -> Response:
    created = subscribe_email(session, body.email)
    session.commit()
    log.info("subscriber %s %s", body.email, "added" if created else "already present")
    if wants_html(request):
        return render(request, "partials/subscribed.html", email=body.email, created=created)
    return JSONResponse({"subscribed": True, "email": body.email, "created": created})


__all__ = [
    "CACHE_TTL_S",
    "invalidate_cache",
    "load_report",
    "markdown_to_html",
    "minimal_markdown",
    "render_inline",
    "router",
    "subscribe_email",
]
