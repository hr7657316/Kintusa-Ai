"""The report page's fallback Markdown converter honours backslash escapes, so the
escaped vendor text written by kintsua.report.markdown renders as literal characters:
no injected links, headings or emphasis, and no stray backslashes."""

from __future__ import annotations

from kintsua.api.report import markdown_to_html, render_inline
from kintsua.report.markdown import md_code, md_escape


def test_escaped_vendor_text_renders_literally():
    html = markdown_to_html("- " + md_escape("stripe [v2] removed `x` (see #12) *y* <script>"))
    assert "stripe [v2] removed `x` (see #12) *y* &lt;script&gt;" in html
    assert "\\" not in html and "<em>" not in html and "<code>" not in html


def test_injected_link_and_heading_stay_text():
    html = markdown_to_html("- " + md_escape("x](https://evil.example) # Heading"))
    assert "<a " not in html and "<h1" not in html
    assert "x](https://evil.example) # Heading" in html


def test_real_formatting_still_works():
    out = render_inline("**bold** `code` [link](https://ok.example) \\*literal\\*")
    assert "<strong>bold</strong>" in out and "<code>code</code>" in out
    assert '<a href="https://ok.example" rel="noopener">link</a>' in out
    assert out.endswith("*literal*")
    assert render_inline(md_code("tool:get_weather")) == "<code>tool:get_weather</code>"
