"""Live checks against real upstreams, through the real watchers.

Run on demand: ``uv run pytest -m network tests/live``. They prove the seeded sources still
parse and that canonicalisation is stable (fetching twice gives the same content hash), which
is what keeps the hosted watcher from emitting phantom change events.
"""

from __future__ import annotations

import pytest

from kintsua.classify.rules import classify_event
from kintsua.watchers.base import SourceRef, get_watcher

pytestmark = [pytest.mark.network, pytest.mark.timeout(240)]

STRIPE = "https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json"
DRIVE = "https://www.googleapis.com/discovery/v1/apis/drive/v3/rest"
DEEPWIKI = "https://mcp.deepwiki.com/mcp"


async def _fetch(kind: str, locator: str, **meta):
    w = get_watcher(kind)
    return w, await w.fetch(SourceRef.stable(kind, locator, meta=meta))


async def test_stripe_openapi_parses_and_is_stable():
    w, doc = await _fetch("openapi", STRIPE)
    assert doc.version
    assert "/v1/charges" in doc.body["paths"]
    assert "post" in doc.body["paths"]["/v1/charges"]
    _, again = await _fetch("openapi", STRIPE)
    assert again.content_hash() == doc.content_hash()
    assert w.diff(doc, again) == []


async def test_google_discovery_doc_converts_to_paths():
    w, doc = await _fetch("openapi", DRIVE, format="discovery")
    assert doc.body["paths"], "discovery resources should become paths"
    assert any("files" in p for p in doc.body["paths"])
    assert w.diff(doc, doc) == []


@pytest.mark.parametrize(
    ("kind", "name"), [("pypi", "stripe"), ("npm", "@modelcontextprotocol/sdk")]
)
async def test_package_registries(kind: str, name: str):
    w, doc = await _fetch(kind, name)
    assert doc.version and doc.version == doc.body["latest"]
    assert doc.version in doc.body["versions"]


async def test_changelog_page_extracts_paragraphs():
    w, doc = await _fetch("changelog", "https://linear.app/changelog")
    assert len(doc.body["paragraphs"]) >= 3


async def test_public_mcp_server_lists_tools_and_classifies_a_simulated_break():
    w, doc = await _fetch("mcp", DEEPWIKI)
    tools = doc.body["tools"]
    assert tools, "deepwiki should expose tools"
    name, tool = next(iter(sorted(tools.items())))
    assert "inputSchema" in tool
    # simulate the vendor removing that tool: the rules must call it breaking/removal
    after = doc.model_copy(deep=True)
    after.body["tools"].pop(name)
    from kintsua.core.util import utcnow
    from kintsua.watchers.base import SnapshotRecord, compare

    src = SourceRef.stable("mcp", DEEPWIKI)
    prev = SnapshotRecord(
        source_id=src.id,
        content_hash=doc.content_hash(),
        version=doc.version,
        captured_at=utcnow(),
        body=doc.body,
    )
    ev = compare(w, src, prev, after)
    assert ev is not None
    cls = classify_event(ev, display_name="DeepWiki")
    assert cls.severity == "breaking" and cls.category == "removal"
    assert f"tool:{name}" in cls.affected_paths
