"""End-to-end: start a tiny MCP server over stdio, change a tool schema, assert the
watcher's fetch + diff produce required_added / type_changed / removed / text_changed."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kintsua.core.util import utcnow
from kintsua.watchers._mcp_compat import MCP_MAJOR, MCPConnectError
from kintsua.watchers.base import SnapshotRecord, SourceRef, compare
from kintsua.watchers.mcp import MCPWatcher, fetch_tools, tool_fingerprint

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "tests" / "fixtures" / "watchers" / "mcp_server.py"


def source(variant: str) -> SourceRef:
    return SourceRef(
        kind="mcp",
        locator=f"{sys.executable} {SERVER}",
        meta={
            "env": {"KINTSUA_MCP_VARIANT": variant, "PYTHONPATH": str(REPO_ROOT)},
            "quiet": True,
            "timeout_s": 60,
        },
    )


@pytest.mark.timeout(120)
async def test_stdio_fetch_and_diff_against_changed_schema():
    w = MCPWatcher()
    before = await w.fetch(source("v1"))
    after = await w.fetch(source("v2"))

    assert MCP_MAJOR in (1, 2)
    assert before.version == "1.0.0" and after.version == "2.0.0"
    assert set(before.body["tools"]) == {"get_weather", "search"}
    assert set(after.body["tools"]) == {"get_weather", "lookup"}
    gw = before.body["tools"]["get_weather"]
    assert gw["inputSchema"]["required"] == ["city"]
    assert gw["inputSchema"]["properties"]["days"]["type"] == "integer"
    assert gw["description"] == "Get the current weather for a city"
    assert "kintsua://readme" in before.body["resources"]
    assert before.body["prompts"] == {} and "summarize" in after.body["prompts"]
    assert before.body["server"]["name"] == "kintsua-fixture"
    assert before.content_hash() != after.content_hash()

    ops = w.diff(before, after)
    s = {(o.op, o.path) for o in ops}
    assert ("required_added", "/tools/get_weather/inputSchema/properties/units") in s
    assert ("type_changed", "/tools/get_weather/inputSchema/properties/days") in s
    assert ("text_changed", "/tools/get_weather/description") in s
    assert ("removed", "/tools/search") in s
    assert ("added", "/tools/lookup") in s
    assert ("added", "/prompts/summarize") in s
    days = next(o for o in ops if o.op == "type_changed" and o.path.endswith("/days"))
    assert days.before == {"type": "integer"} and days.after == {"type": "string"}

    src = source("v1")
    prev = SnapshotRecord(
        source_id=src.id,
        content_hash=before.content_hash(),
        version=before.version,
        captured_at=utcnow(),
        body=before.body,
    )
    ev = compare(w, src, prev, after, text=w.event_text(before, after))
    assert ev is not None and ev.from_version == "1.0.0" and ev.to_version == "2.0.0"
    assert ev.text and "get_weather:" in ev.text
    # same server twice -> identical canonical document (no volatile fields leaked)
    again = await w.fetch(source("v1"))
    assert again.content_hash() == before.content_hash()


@pytest.mark.timeout(60)
async def test_fetch_tools_and_fingerprint_match_the_snapshot():
    src = source("v1")
    tools = await fetch_tools(src.locator, src.meta)
    names = sorted(t["name"] for t in tools)
    assert names == ["get_weather", "search"]
    doc = await MCPWatcher().fetch(src)
    for t in tools:
        assert tool_fingerprint(t["inputSchema"]) == tool_fingerprint(
            doc.body["tools"][t["name"]]["inputSchema"]
        )
        assert "_meta" not in t


@pytest.mark.timeout(60)
async def test_bad_stdio_locator_gives_a_clear_error():
    with pytest.raises(MCPConnectError) as ei:
        await MCPWatcher().fetch(SourceRef(kind="mcp", locator="kintsua-no-such-binary --x"))
    assert "kintsua-no-such-binary" in str(ei.value)


async def test_missing_auth_env_fails_before_connecting():
    with pytest.raises(MCPConnectError, match="KINTSUA_TEST_TOKEN_UNSET is not set"):
        await MCPWatcher().fetch(
            SourceRef(
                kind="mcp",
                locator="https://127.0.0.1:9/mcp",
                meta={"auth_env": "KINTSUA_TEST_TOKEN_UNSET"},
            )
        )
