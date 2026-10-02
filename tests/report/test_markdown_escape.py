"""Vendor- and model-derived text in the weekly report is inert Markdown (cross-area A5): a
summary, hint, vendor name, version, affected path or failing-source error can never add a
link, a heading, raw HTML or a table cell to the page ``GET /report/weekly`` renders."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import timedelta

from kintsua.api.report import markdown_to_html
from kintsua.core.util import utcnow
from kintsua.report.markdown import (
    FailingSource,
    ReportItem,
    WeeklyReport,
    md_code,
    md_escape,
    render_markdown,
    render_rss,
)

EVIL = "https://evil.example"


def _report() -> WeeklyReport:
    now = utcnow()
    items = [
        ReportItem(
            source_name="Weather MCP",
            source_kind="mcp",
            vendor="weather",
            severity="breaking",
            category="[docs",
            summary="x](https://evil.example) # Heading",
            detected_at=now,
        ),
        ReportItem(
            source_name="[Vendor](https://evil.example)",
            source_kind="openapi",
            severity="degraded",
            category="removal",
            summary="see [the docs](https://evil.example)\n# Heading\n<script>alert(1)</script>",
            detected_at=now,
            from_version="[1](https://evil.example)",
            to_version="2\n## Two",
            affected_paths=["x` [y](https://evil.example) `z", "/tools/get_weather"],
            migration_hint="**click** <img src=x onerror=alert(1)> ![i](https://evil.example/i)",
            rule_hits=["[r](https://evil.example)"],
        ),
    ]
    failing = [
        FailingSource(
            display_name="a | [b](https://evil.example)",
            kind="openapi",
            locator="https://spec.vendor.example",
            last_error="HTTPStatusError: 500 | [x](https://evil.example)",
            failing_since=now - timedelta(days=9),
            consecutive_failures=4,
        )
    ]
    return WeeklyReport(
        since=now - timedelta(days=7),
        until=now,
        generated_at=now,
        items=items,
        failing_sources=failing,
        public=True,
    )


def test_vendor_text_cannot_add_links_headings_or_html_to_the_page():
    md = render_markdown(_report())
    page = markdown_to_html(md)
    assert f'href="{EVIL}' not in page and "evil.example" in page  # shown as text only
    assert page.count("<h1>") == 1  # the report's own title
    assert "<h1>Heading" not in page and "<h2>Two" not in page and "Heading</h" not in page
    assert "<script" not in page and "<img" not in page
    assert "<strong>click" not in page
    # the failing-sources table keeps its four columns
    (row,) = [line for line in md.splitlines() if "HTTPStatusError" in line]
    assert row.replace("\\|", "").count("|") == 5
    # escapes render as the literal characters: no link, no stray backslashes
    assert "<td>a | [b](https://evil.example)</td>" in page
    # paths stay code spans; a backtick in one cannot close it early
    assert "`x' [y](https://evil.example) 'z`" in md and "`/tools/get_weather`" in md
    # the report's own link is untouched
    assert 'href="https://github.com/hr7657316/Kintusa-Ai"' in page


def test_md_escape_and_md_code():
    assert md_escape("x](https://evil.example) # Heading") == (
        "x\\]\\(https://evil.example\\) \\# Heading"
    )
    assert md_escape("a\nb\r\n\tc\x00d e") == "a b cd e"
    assert md_escape("<b>|`*") == "\\<b\\>\\|\\`\\*"
    # a backslash in the text cannot cancel the escape that follows it
    assert md_escape("\\[x]") == "\\\\\\[x\\]"
    # intraword underscores (tool names) stay readable; the rest are escaped
    assert md_escape("get_weather _x_ __init__") == "get_weather \\_x\\_ \\_\\_init\\_\\_"
    assert md_code("a`b\nc") == "`a'b c`"


def test_rss_titles_are_one_line_and_descriptions_are_text():
    rss = render_rss(_report(), "https://kintsua.example")
    root = ET.fromstring(rss)
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        desc = item.findtext("description") or ""
        assert "\n" not in title
        # a feed reader parses the description as HTML: no tag may survive the XML decode
        assert "<script" not in desc and "<img" not in desc
    assert any("&lt;script" in (i.findtext("description") or "") for i in root.iter("item"))
