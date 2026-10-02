"""wrap_mcp: fingerprints persisted under $KINTSUA_STATE_DIR and equal to the watcher's,
schema_mismatch with a diff, ok / error / exception outcomes, sampling, sync and async
duck-typed sessions, real mcp.types results, no-op without a key, idempotent, never raises."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.types import (
    CallToolResult,
    Implementation,
    InitializeResult,
    ListToolsResult,
    ServerCapabilities,
    TextContent,
    Tool,
)

from kintsua.sdk import wrap_mcp
from kintsua.sdk.wrap import fingerprint, schema_diff, server_id, tools_of
from kintsua.watchers.mcp import tool_fingerprint

SCHEMA_V1 = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
    "required": ["city"],
}
SCHEMA_V2 = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "days": {"type": "string"},
        "units": {"type": "string", "enum": ["metric", "imperial"]},
    },
    "required": ["city", "units"],
}
SERVER = "https://mcp.example.com/mcp"


class FakeSession:
    """Duck-typed async MCP session (what raw mcp, langchain and openai-agents look like)."""

    def __init__(self, schema, fail: bool = False):
        self.schema, self.fail, self.calls = schema, fail, []

    async def initialize(self):
        return {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "weather-mcp", "version": "1.0"},
            "capabilities": {},
        }

    async def list_tools(self):
        return {
            "tools": [
                {"name": "get_weather", "description": "d", "inputSchema": self.schema},
                {"name": "search", "inputSchema": {"type": "object"}},
            ]
        }

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self.fail:
            raise ConnectionError("server gone")
        return {"content": [], "isError": name == "broken"}


def events(rep) -> list[dict]:
    return list(rep._q)


def state_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "state").glob("*.json"))


def test_fingerprint_matches_the_watcher_algorithm():
    for schema in (SCHEMA_V1, SCHEMA_V2, {}, None, {"b": 1, "a": {"z": [1, 2], "y": "ünï"}}):
        assert fingerprint(schema) == tool_fingerprint(schema)


async def test_first_sight_persists_fingerprints_without_a_mismatch(reporter, tmp_path):
    s = wrap_mcp(FakeSession(SCHEMA_V1), server=SERVER)
    await s.initialize()
    await s.list_tools()
    assert events(reporter) == []
    files = state_files(tmp_path)
    assert len(files) == 1 and files[0].name == "acme-agent.json"  # <repo-slug>.json
    state = json.loads(files[0].read_text())
    entry = state[f"{SERVER}::get_weather"]
    assert entry["fingerprint"] == tool_fingerprint(SCHEMA_V1) and entry["schema"] == SCHEMA_V1
    assert f"{SERVER}::search" in state


async def test_schema_change_emits_a_mismatch_with_the_diff(reporter, tmp_path):
    await wrap_mcp(FakeSession(SCHEMA_V1), server=SERVER).list_tools()
    await wrap_mcp(FakeSession(SCHEMA_V2), server=SERVER).list_tools()
    evs = events(reporter)
    assert len(evs) == 1
    ev = evs[0]
    assert (ev["tool_name"], ev["kind"], ev["outcome"]) == ("get_weather", "mcp", "schema_mismatch")
    assert ev["schema_fingerprint"] == tool_fingerprint(SCHEMA_V2) and ev["error_class"] is None
    assert ev["sample"]["server"] == SERVER
    ops = {(o["op"], o["path"]) for o in ev["sample"]["diff"]}
    base = "/tools/get_weather/inputSchema/properties/"
    assert ("required_added", base + "units") in ops
    assert ("added", base + "units") in ops
    assert ("type_changed", base + "days") in ops
    state = json.loads(state_files(tmp_path)[0].read_text())
    assert state[f"{SERVER}::get_weather"]["fingerprint"] == tool_fingerprint(SCHEMA_V2)
    await wrap_mcp(FakeSession(SCHEMA_V2), server=SERVER).list_tools()  # unchanged -> quiet
    assert len(events(reporter)) == 1


async def test_server_name_comes_from_initialize_when_no_locator_is_known(reporter, tmp_path):
    s = wrap_mcp(FakeSession(SCHEMA_V1))
    await s.initialize()
    await s.list_tools()
    assert "mcp:weather-mcp::get_weather" in json.loads(state_files(tmp_path)[0].read_text())


async def test_the_server_locator_never_carries_credentials(reporter, tmp_path):
    url = "https://user:pw@mcp.example.com/mcp?api_key=s3cret#frag"
    await wrap_mcp(FakeSession(SCHEMA_V1), server=url).list_tools()
    session = FakeSession(SCHEMA_V2)
    session.url = url  # a session that knows its own URL
    await wrap_mcp(session).list_tools()
    (ev,) = events(reporter)
    assert ev["sample"]["server"] == SERVER  # what sample.server carries to the hosted side
    text = state_files(tmp_path)[0].read_text()
    assert "s3cret" not in text and "pw@" not in text and "frag" not in text
    assert f"{SERVER}::get_weather" in json.loads(text)


async def test_sessions_of_one_server_share_its_fingerprints(reporter):
    await wrap_mcp(FakeSession(SCHEMA_V1), server=SERVER).list_tools()
    await wrap_mcp(FakeSession(SCHEMA_V1), server=SERVER).call_tool("get_weather", {})  # per call
    await wrap_mcp(FakeSession(SCHEMA_V1), server="https://other.test/mcp").call_tool("search")
    first, other = events(reporter)
    assert first["schema_fingerprint"] == tool_fingerprint(SCHEMA_V1)
    assert other["schema_fingerprint"] is None  # another server's cache is not consulted


def test_server_id_forms():
    assert server_id({"url": "https://h.test/mcp/?k=v"}) == "https://h.test/mcp/"
    assert server_id({"url": "wss://u:p@h.test:8443/ws#x"}) == "wss://h.test:8443/ws"
    assert server_id(type("P", (), {"url": None, "command": "npx", "args": ["-y", "a b"]})()) == (
        "npx -y 'a b'"
    )
    line = server_id({"command": "uvx", "args": ["srv", "--api-key=abcdef123456"]})
    assert line.startswith("uvx srv ") and "abcdef123456" not in line
    assert server_id(None, "mcp:weather") == "mcp:weather" and server_id({}) == ""
    assert server_id({"url": "http://[::1"}).startswith("http://")  # malformed: never raises


async def test_call_tool_outcomes_and_sampling(reporter):
    s = wrap_mcp(FakeSession(SCHEMA_V1), sample=True)
    await s.list_tools()
    res = await s.call_tool("get_weather", {"city": "Oslo", "days": 2})
    assert res["isError"] is False
    await s.call_tool("broken", arguments={"b": 1, "a": 2})
    with pytest.raises(ConnectionError):
        await wrap_mcp(FakeSession(SCHEMA_V1, fail=True), sample=True).call_tool(
            "get_weather", {"city": "x"}
        )
    ok, bad, exc = events(reporter)
    assert (ok["tool_name"], ok["outcome"], ok["error_class"]) == ("get_weather", "ok", None)
    assert ok["sample"] == {"arg_keys": ["city", "days"]}  # keys only, never values
    assert ok["schema_fingerprint"] == tool_fingerprint(SCHEMA_V1)
    assert (bad["outcome"], bad["error_class"]) == ("error", "ToolError")
    assert bad["sample"] == {"arg_keys": ["a", "b"]}
    assert (exc["outcome"], exc["error_class"]) == ("error", "ConnectionError")
    assert all(isinstance(e["latency_ms"], int) for e in (ok, bad, exc))


async def test_no_sampling_by_default(reporter):
    s = wrap_mcp(FakeSession(SCHEMA_V1))
    await s.call_tool("get_weather", {"city": "Oslo"})
    assert events(reporter)[0]["sample"] is None


async def test_real_mcp_types(reporter, tmp_path):
    class TypedSession:
        async def initialize(self):
            return InitializeResult(
                protocolVersion="2025-06-18",
                capabilities=ServerCapabilities(),
                serverInfo=Implementation(name="typed", version="2"),
            )

        async def list_tools(self):
            return ListToolsResult(tools=[Tool(name="get_weather", inputSchema=SCHEMA_V1)])

        async def call_tool(self, name, arguments=None, **kw):
            return CallToolResult(content=[TextContent(type="text", text="x")], isError=True)

    assert tools_of(ListToolsResult(tools=[Tool(name="t", inputSchema=SCHEMA_V1)])) == [
        ("t", SCHEMA_V1)
    ]
    s = wrap_mcp(TypedSession())
    await s.initialize()
    await s.list_tools()
    res = await s.call_tool("get_weather", {"city": "x"}, read_timeout_seconds=5)
    assert isinstance(res, CallToolResult)
    ev = events(reporter)[0]
    assert (ev["outcome"], ev["error_class"]) == ("error", "ToolError")
    assert ev["schema_fingerprint"] == tool_fingerprint(SCHEMA_V1)
    assert "mcp:typed::get_weather" in json.loads(state_files(tmp_path)[0].read_text())


def test_sync_sessions_are_supported(reporter):
    class SyncSession:
        def list_tools(self):
            return [{"name": "t", "inputSchema": SCHEMA_V1}]

        def call_tool(self, name, arguments=None):
            return {"isError": False}

    s = wrap_mcp(SyncSession(), server="stdio:local")
    s.list_tools()
    assert s.call_tool("t", {}) == {"isError": False}
    ev = events(reporter)[0]
    assert ev["tool_name"] == "t" and ev["outcome"] == "ok"
    assert ev["schema_fingerprint"] == tool_fingerprint(SCHEMA_V1)


async def test_noop_without_a_key():
    s = FakeSession(SCHEMA_V1)
    original = s.call_tool
    assert wrap_mcp(s) is s
    assert s.call_tool == original and not hasattr(s, "_kintsua_wrapped")
    await s.call_tool("get_weather", {})


async def test_wrapping_twice_records_once(reporter):
    s = wrap_mcp(wrap_mcp(FakeSession(SCHEMA_V1)))
    await s.call_tool("get_weather", {})
    assert len(events(reporter)) == 1


def test_unpatchable_objects_are_returned_untouched(reporter):
    class Rigid:
        __slots__ = ("x",)

        def call_tool(self, name, arguments=None):
            return {}

    r = Rigid()
    assert wrap_mcp(r) is r


def test_schema_diff_ops():
    ops = schema_diff(SCHEMA_V1, SCHEMA_V2, "/tools/get_weather/inputSchema")
    assert {(o["op"], o["path"].rsplit("/", 1)[1]) for o in ops} == {
        ("type_changed", "days"),
        ("added", "units"),
        ("required_added", "units"),
    }
    assert all(o["path"].startswith("/tools/get_weather/inputSchema/properties/") for o in ops)
    back = {(o["op"], o["path"].rsplit("/", 1)[1]) for o in schema_diff(SCHEMA_V2, SCHEMA_V1)}
    assert back == {("type_changed", "days"), ("removed", "units"), ("required_removed", "units")}
    nested_a = {"properties": {"f": {"type": "object", "properties": {"x": {"enum": ["a"]}}}}}
    nested_b = {"properties": {"f": {"type": "object", "properties": {"x": {"enum": ["a", "b"]}}}}}
    nested = schema_diff(nested_a, nested_b)
    assert [(o["op"], o["path"]) for o in nested] == [
        ("enum_changed", "/properties/f/properties/x")
    ]
    assert schema_diff(None, {}) == [] and schema_diff(SCHEMA_V1, SCHEMA_V1) == []
