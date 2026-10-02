"""MCP watcher diff on fixture pairs (no server needed)."""

from __future__ import annotations

import json
from pathlib import Path

from kintsua.core.events import CanonicalDoc
from kintsua.core.util import utcnow
from kintsua.watchers.base import SnapshotRecord, SourceRef, compare
from kintsua.watchers.mcp import MCPWatcher, build_body, diff_mcp_bodies, tool_fingerprint

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "watchers"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text("utf-8"))


def doc(name: str) -> CanonicalDoc:
    body = load(name)
    return CanonicalDoc(version=body["server"]["version"], body=body)


def kinds(ops):
    return {(o.op, o.path) for o in ops}


def test_fixture_pair_produces_every_op_kind():
    before, after = doc("mcp_before.json"), doc("mcp_after.json")
    ops = MCPWatcher().diff(before, after)
    s = kinds(ops)
    # changed inputSchema -> JSON Schema diff under /tools/<name>/inputSchema
    assert ("required_added", "/tools/get_weather/inputSchema/properties/units") in s
    assert ("type_changed", "/tools/get_weather/inputSchema/properties/days") in s
    assert ("type_changed", "/tools/get_weather/inputSchema/properties/days/maximum") in s
    assert ("enum_changed", "/tools/get_weather/inputSchema/properties/units") in s
    # description drift is recorded alongside the schema change
    assert ("text_changed", "/tools/get_weather/description") in s
    # nested property changes
    assert ("removed", "/tools/search/inputSchema/properties/limit") in s
    assert ("removed", "/tools/search/inputSchema/properties/filters/properties/region") in s
    assert ("added", "/tools/search/inputSchema/properties/filters/properties/country") in s
    assert ("enum_changed", "/tools/search/inputSchema/properties/filters/properties/lang") in s
    # deprecation + added tool
    assert ("deprecated", "/tools/send_email/inputSchema") in s
    assert ("text_changed", "/tools/send_email/description") in s
    assert ("added", "/tools/send_message") in s
    # untouched tool is silent
    assert not any(p.startswith("/tools/stable_tool") for _, p in s)
    # resources keyed by uri (pointer-escaped), prompts by name
    assert ("removed", "/resources/weather:~1~1legacy") in s
    assert ("added", "/resources/weather:~1~1alerts") in s
    assert ("text_changed", "/resources/weather:~1~1cities/description") in s
    assert ("required_added", "/prompts/forecast/arguments/tone") in s
    assert ("added", "/prompts/alert") in s
    # the schema-level required_added carries the property name
    op = next(o for o in ops if o.op == "required_added" and o.path.endswith("/units"))
    assert op.after == "units" and op.before is None
    # self diff is empty
    assert MCPWatcher().diff(before, before) == []


def test_description_only_change_is_always_recorded():
    before = load("mcp_before.json")
    after = json.loads(json.dumps(before))
    after["tools"]["stable_tool"]["description"] = "Never changes (except this once)"
    ops = diff_mcp_bodies(before, after)
    assert kinds(ops) == {("text_changed", "/tools/stable_tool/description")}
    assert ops[0].before == "Never changes" and ops[0].after.endswith("this once)")


def test_removed_tool_carries_fingerprint_and_summary():
    before = load("mcp_before.json")
    after = json.loads(json.dumps(before))
    del after["tools"]["search"]
    ops = diff_mcp_bodies(before, after)
    assert kinds(ops) == {("removed", "/tools/search")}
    assert ops[0].before["fingerprint"] == tool_fingerprint(
        before["tools"]["search"]["inputSchema"]
    )
    assert ops[0].before["description"] == "Search the knowledge base"
    assert "query" in ops[0].before["inputSchema"]["properties"]


def test_tool_fingerprint_is_canonical():
    a = {"type": "object", "properties": {"b": {"type": "string"}, "a": {"type": "integer"}}}
    b = {"properties": {"a": {"type": "integer"}, "b": {"type": "string"}}, "type": "object"}
    assert tool_fingerprint(a) == tool_fingerprint(b)
    assert len(tool_fingerprint(a)) == 64
    assert tool_fingerprint(a) != tool_fingerprint({**a, "required": ["a"]})
    assert tool_fingerprint(None) == tool_fingerprint({})


def test_build_body_strips_volatile_keys_and_sorts():
    init = {
        "protocolVersion": "2025-06-18",
        "serverInfo": {"name": "srv", "version": "3.1.0"},
        "capabilities": {"tools": {"listChanged": True}},
        "_meta": {"requestId": "abc"},
        "instructions": "Be nice",
    }
    tools = [
        {
            "name": "zeta",
            "description": "Z",
            "inputSchema": {"type": "object", "properties": {"b": {}, "a": {}}},
            "_meta": {"ts": 1},
            "icons": [{"src": "x.png"}],
        },
        {"name": "alpha", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
        {"description": "no name -> dropped"},
    ]
    resources = [{"uri": "a://x", "name": "x", "_meta": {"etag": "1"}, "mimeType": "text/plain"}]
    prompts = [{"name": "p", "arguments": [{"name": "q", "required": True, "extra": 1}]}]
    body = build_body(init, tools, resources, prompts)
    assert list(body) == sorted(body)
    assert list(body["tools"]) == ["alpha", "zeta"]
    assert body["server"] == {
        "capabilities": {"tools": {"listChanged": True}},
        "instructions": "Be nice",
        "name": "srv",
        "protocolVersion": "2025-06-18",
        "version": "3.1.0",
    }
    assert "_meta" not in body["tools"]["zeta"] and "icons" not in body["tools"]["zeta"]
    assert list(body["tools"]["zeta"]["inputSchema"]["properties"]) == ["a", "b"]
    assert body["tools"]["alpha"]["annotations"] == {"readOnlyHint": True}
    assert body["resources"]["a://x"] == {"mimeType": "text/plain", "name": "x"}
    assert body["prompts"]["p"]["arguments"] == [{"name": "q", "required": True}]


def test_event_text_lists_new_and_changed_descriptions():
    before, after = doc("mcp_before.json"), doc("mcp_after.json")
    text = MCPWatcher().event_text(before, after)
    assert "get_weather:" in text and "send_message:" in text and "stable_tool" not in text


def test_compare_emits_change_event_with_versions():
    before, after = doc("mcp_before.json"), doc("mcp_after.json")
    src = SourceRef.stable("mcp", "https://weather.example/mcp")
    prev = SnapshotRecord(
        source_id=src.id,
        content_hash=before.content_hash(),
        version=before.version,
        captured_at=utcnow(),
        body=before.body,
    )
    ev = compare(MCPWatcher(), src, prev, after, text="notes")
    assert ev is not None
    assert ev.source_kind == "mcp" and ev.from_version == "1.4.0" and ev.to_version == "2.0.0"
    assert ev.text == "notes" and len(ev.ops) > 10
    # unchanged content -> no event; first sight -> no event
    assert compare(MCPWatcher(), src, prev, before) is None
    assert compare(MCPWatcher(), src, None, after) is None
