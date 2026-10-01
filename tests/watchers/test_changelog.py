"""Changelog watcher: extraction, paragraph diff, robots.txt handling (respx)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from kintsua.watchers.base import SourceRef
from kintsua.watchers.changelog import (
    ChangelogWatcher,
    RobotsDisallowed,
    clear_robots_cache,
    html_to_body,
    page_title,
    split_paragraphs,
    strip_tags,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "watchers"
BEFORE = (FIX / "changelog_before.html").read_text("utf-8")
AFTER = (FIX / "changelog_after.html").read_text("utf-8")
URL = "https://acme.example.com/changelog"


@pytest.fixture(autouse=True)
def _fresh_robots_cache():
    clear_robots_cache()
    yield
    clear_robots_cache()


@respx.mock
async def test_fetch_and_paragraph_diff():
    respx.get("https://acme.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    page = respx.get(URL)
    page.side_effect = [httpx.Response(200, text=BEFORE), httpx.Response(200, text=AFTER)]
    w = ChangelogWatcher()
    src = SourceRef(kind="changelog", locator=URL)
    d1 = await w.fetch(src)
    d2 = await w.fetch(src)

    assert d1.body["title"] == "Acme API Changelog" and d1.body["url"] == URL
    assert d1.version is None
    paras = d1.body["paragraphs"]
    assert any("uploadPhoto" in p for p in paras)
    assert not any("dataLayer" in p for p in paras)
    assert len(paras) == len(set(paras))
    assert d1.content_hash() != d2.content_hash()

    ops = w.diff(d1, d2)
    assert ops and all(o.op == "text_changed" for o in ops)
    assert all(o.path.startswith("/paragraphs/") for o in ops)
    added = [o for o in ops if o.before is None]
    removed = [o for o in ops if o.after is None]
    assert any("tenant" in o.after for o in added)
    assert any("legacyExport" in o.after for o in added)
    assert not removed or all(isinstance(o.before, str) for o in removed)
    text = w.event_text(d1, d2)
    assert "tenant" in text and "legacyExport" in text and "uploadPhoto" not in text
    assert w.diff(d1, d1) == [] and w.event_text(d1, d1) is None


@respx.mock
async def test_robots_disallow_blocks_fetch_with_clear_error():
    respx.get("https://acme.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /changelog\n")
    )
    page = respx.get(URL).mock(return_value=httpx.Response(200, text=BEFORE))
    with pytest.raises(RobotsDisallowed, match="robots.txt disallows"):
        await ChangelogWatcher().fetch(SourceRef(kind="changelog", locator=URL))
    assert not page.called
    # explicit opt-out for pages a customer owns
    d = await ChangelogWatcher().fetch(
        SourceRef(kind="changelog", locator=URL, meta={"ignore_robots": True})
    )
    assert d.body["paragraphs"]


@respx.mock
async def test_missing_robots_counts_as_allow_and_is_cached():
    robots = respx.get("https://acme.example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(URL).mock(return_value=httpx.Response(200, text=BEFORE))
    w = ChangelogWatcher()
    await w.fetch(SourceRef(kind="changelog", locator=URL))
    await w.fetch(SourceRef(kind="changelog", locator=URL))
    assert robots.call_count == 1


@respx.mock
async def test_empty_page_is_an_error():
    respx.get("https://acme.example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(URL).mock(
        return_value=httpx.Response(200, text="<html><body><script>x()</script></body></html>")
    )
    with pytest.raises(ValueError, match="no text content"):
        await ChangelogWatcher().fetch(SourceRef(kind="changelog", locator=URL))


def test_fallback_tag_stripper_and_paragraph_splitting():
    text = strip_tags(BEFORE)
    assert "dataLayer" not in text and "font-family" not in text
    paras = split_paragraphs(text, mode="fallback")
    assert any(p.startswith("Added the uploadPhoto endpoint") for p in paras)
    assert any("X-RateLimit-Remaining" in p for p in paras)
    assert page_title(BEFORE) == "Acme API Changelog"
    # trafilatura mode: one block per line, duplicates dropped, whitespace normalised
    assert split_paragraphs("a  b\n\n a b \nc\n", mode="trafilatura") == ["a b", "c"]
    body = html_to_body(BEFORE, URL)
    assert body["title"] == "Acme API Changelog" and body["paragraphs"]
