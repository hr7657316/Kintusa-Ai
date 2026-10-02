"""Changelog watcher (``kind="changelog"``): fetch a vendor changelog page, extract the main
content with ``trafilatura`` (falling back to a tag stripper), split it into paragraphs
and diff the paragraph sets.

Canonical body: ``{"title": str | None, "url": str, "paragraphs": [str, ...]}``.

Every change is ``text_changed`` at ``/paragraphs/<index>`` (``before=None`` for a new
paragraph, ``after=None`` for a vanished one) and the joined new paragraphs travel with the
event as ``text`` for the LLM classifier. ``robots.txt`` is honoured (``urllib.robotparser``;
unreachable robots files count as allow) and a disallowed page fails the poll with a clear
``RobotsDisallowed`` error. The page body is capped at ``meta["max_bytes"]`` (32 MB) and
guarded sources (``meta["_net_guard"]``) go through ``kintsua.core.netguard``.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
import urllib.robotparser
from typing import Any
from urllib.parse import urlsplit

import httpx

from kintsua.core.events import CanonicalDoc, ChangeOp, json_pointer
from kintsua.watchers._http import (
    ResponseTooLarge,
    client_kwargs,
    get_bounded,
    guard_url,
    max_bytes_for,
)
from kintsua.watchers.base import BaseWatcher, SourceRef, register

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "kintsua-watcher/0.1 (+https://github.com/hr7657316/Kintusa-Ai)"
DEFAULT_TIMEOUT_S = 60.0
ROBOTS_TTL_S = 3600.0
ROBOTS_MAX_BYTES = 512 * 1024
MAX_PARAGRAPHS = 5000
TEXT_LIMIT = 20_000

_robots_cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}


class RobotsDisallowed(PermissionError):
    """The site's robots.txt forbids fetching this page for our user agent."""


def _ua_token(user_agent: str) -> str:
    return user_agent.split("/", 1)[0].split(" ", 1)[0] or "kintsua"


async def robots_allows(client: httpx.AsyncClient, url: str, user_agent: str) -> bool:
    parts = urlsplit(url)
    host = f"{parts.scheme}://{parts.netloc}"
    now = time.monotonic()
    cached = _robots_cache.get(host)
    if cached and cached[0] > now:
        rp = cached[1]
    else:
        rp = None
        try:
            r = await get_bounded(client, host + "/robots.txt", max_bytes=ROBOTS_MAX_BYTES)
            if r.status_code == 200 and r.text:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
        except (httpx.HTTPError, ResponseTooLarge) as e:  # unreachable robots.txt: allow
            log.debug("robots.txt fetch failed for %s: %s", host, e)
        _robots_cache[host] = (now + ROBOTS_TTL_S, rp)
    if rp is None:
        return True
    return rp.can_fetch(_ua_token(user_agent), url) or rp.can_fetch(user_agent, url)


def clear_robots_cache() -> None:
    _robots_cache.clear()


# --------------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------------

_BLOCK_RE = re.compile(
    r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|header|footer|blockquote|pre|dd|dt)\b[^>]*>",
    re.IGNORECASE,
)
_SCRIPT_RE = re.compile(
    r"<(script|style|noscript|svg|template)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"[ \t\r\f\v ]+")


def strip_tags(html_text: str) -> str:
    text = _COMMENT_RE.sub("", html_text)
    text = _SCRIPT_RE.sub("", text)
    text = _BLOCK_RE.sub("\n\n", text)
    text = _TAG_RE.sub(" ", text)
    return html.unescape(text)


def page_title(html_text: str) -> str | None:
    m = _TITLE_RE.search(html_text)
    if not m:
        return None
    t = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", m.group(1)))).strip()
    return t[:300] or None


def extract_text(html_text: str, url: str | None = None) -> tuple[str, str]:
    """Main content via trafilatura (links and comments excluded); tag-stripping fallback.
    Returns ``(text, mode)`` where mode is ``"trafilatura"`` (one block per line) or
    ``"fallback"`` (blocks separated by blank lines)."""
    text: str | None = None
    try:
        import trafilatura  # heavy; imported lazily so the CLI stays light

        text = trafilatura.extract(
            html_text,
            url=url,
            include_links=False,
            include_comments=False,
            include_tables=True,
            output_format="txt",
        )
    except Exception as e:  # noqa: BLE001 - any extraction failure falls back
        log.debug("trafilatura failed for %s: %s", url, e)
        text = None
    if text and text.strip():
        return text, "trafilatura"
    return strip_tags(html_text), "fallback"


def split_paragraphs(
    text: str, *, mode: str = "trafilatura", max_paragraphs: int = MAX_PARAGRAPHS
) -> list[str]:
    """Split extracted text into paragraphs: one per line for trafilatura output, one per
    blank-line separated block for the fallback. Whitespace-normalised, empty and duplicate
    blocks dropped (order kept) so a paragraph set diff is meaningful."""
    if mode == "trafilatura":
        blocks = text.splitlines()
    else:
        blocks = re.split(r"\n\s*\n", text)
    out: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        lines = [_WS_RE.sub(" ", ln).strip() for ln in block.splitlines()]
        para = " ".join(ln for ln in lines if ln).strip()
        if not para or para in seen:
            continue
        seen.add(para)
        out.append(para)
        if len(out) >= max_paragraphs:
            break
    return out


def html_to_body(html_text: str, url: str) -> dict[str, Any]:
    text, mode = extract_text(html_text, url)
    return {
        "title": page_title(html_text),
        "url": url,
        "paragraphs": split_paragraphs(text, mode=mode),
    }


# --------------------------------------------------------------------------------------
# Watcher
# --------------------------------------------------------------------------------------


@register
class ChangelogWatcher(BaseWatcher):
    kind = "changelog"

    async def fetch(self, source: SourceRef) -> CanonicalDoc:
        meta = source.meta or {}
        url = source.locator.strip()
        ua = str(meta.get("user_agent") or DEFAULT_USER_AGENT)
        headers = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        for k, v in (meta.get("headers") or {}).items():
            headers[str(k)] = os.path.expandvars(str(v))
        timeout = float(meta.get("timeout_s") or DEFAULT_TIMEOUT_S)
        guard_url(url, meta)
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            headers=headers,
            **client_kwargs(meta),  # guarded: robots.txt, the page and every redirect hop
        ) as client:
            if not meta.get("ignore_robots") and not await robots_allows(client, url, ua):
                raise RobotsDisallowed(f"robots.txt disallows fetching {url} for {_ua_token(ua)}")
            r = await get_bounded(client, url, max_bytes=max_bytes_for(meta))
            r.raise_for_status()
            html_text = r.text
        body = html_to_body(html_text, url)
        if not body["paragraphs"]:
            raise ValueError(f"no text content extracted from {url}")
        return CanonicalDoc(version=None, body=body)

    def diff(self, before: CanonicalDoc, after: CanonicalDoc) -> list[ChangeOp]:
        b_paras = list(before.body.get("paragraphs") or [])
        a_paras = list(after.body.get("paragraphs") or [])
        b_set, a_set = set(b_paras), set(a_paras)
        ops: list[ChangeOp] = []
        if before.body.get("title") != after.body.get("title"):
            ops.append(
                ChangeOp(
                    op="text_changed",
                    path="/title",
                    before=before.body.get("title"),
                    after=after.body.get("title"),
                )
            )
        for i, p in enumerate(a_paras):
            if p not in b_set:
                ops.append(
                    ChangeOp(
                        op="text_changed", path=json_pointer("paragraphs", i), before=None, after=p
                    )
                )
        for i, p in enumerate(b_paras):
            if p not in a_set:
                ops.append(
                    ChangeOp(
                        op="text_changed", path=json_pointer("paragraphs", i), before=p, after=None
                    )
                )
        return ops

    def event_text(self, before: CanonicalDoc, after: CanonicalDoc) -> str | None:
        b_set = set(before.body.get("paragraphs") or [])
        new = [p for p in after.body.get("paragraphs") or [] if p not in b_set]
        text = "\n\n".join(new).strip()
        return text[:TEXT_LIMIT] or None
