"""Framework hooks return False when the framework is absent and patch stand-in framework
modules (installed into sys.modules) otherwise, recording calls and fingerprints. The stand-ins
mirror the real libraries' structure: langchain-mcp-adapters opens every session through
``create_session`` (imported by name into ``tools`` and ``client``), google-adk's ``McpTool`` /
``McpToolset`` are the base classes of the deprecated ``MCPTool`` / ``MCPToolset`` aliases."""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest
from mcp.types import ListToolsResult, Tool

from kintsua.sdk import hook_google_adk, hook_langchain, hook_openai_agents
from kintsua.sdk.wrap import fingerprint

SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}
SCHEMA_V2 = {"type": "object", "properties": {"q": {"type": "integer"}}, "required": ["q"]}


@pytest.mark.parametrize(
    "hook,module",
    [
        (hook_openai_agents, "agents"),
        (hook_langchain, "langchain_mcp_adapters"),
        (hook_google_adk, "google.adk"),
    ],
)
def test_hooks_return_false_when_the_framework_is_absent(hook, module, monkeypatch):
    monkeypatch.setitem(sys.modules, module, None)  # `import <module>` raises ImportError
    assert hook() is False
    assert hook(repo="acme/x", key="kts_k", sample=True) is False


def _install(monkeypatch, name: str, source: str | None = None, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    if "." in name:  # `from pkg import sub` finds it as an attribute too
        parent, _, child = name.rpartition(".")
        monkeypatch.setattr(sys.modules[parent], child, mod, raising=False)
    if source is not None:  # module-level names resolve in this module, like the real library
        exec(source, mod.__dict__)
    return mod


def _state(tmp_path) -> dict:
    return json.loads(next((tmp_path / "state").glob("*.json")).read_text())


def _mismatches(reporter) -> list[dict]:
    return [e for e in reporter._q if e["outcome"] == "schema_mismatch"]


# ---- OpenAI Agents SDK -------------------------------------------------------------------


def _agents(monkeypatch):
    class _Server:
        def __init__(self, name="weather", params=None, schema=SCHEMA):
            self.name, self.params, self.schema = name, params, schema

        async def list_tools(self, *args, **kwargs):
            return [Tool(name="get_weather", inputSchema=self.schema)]

        async def call_tool(self, tool_name, arguments):
            return {"isError": tool_name == "bad"}

    classes = {n: type(n, (_Server,), {}) for n in ("MCPServerStdio", "MCPServerStreamableHttp")}
    _install(monkeypatch, "agents")
    _install(monkeypatch, "agents.mcp", **classes)
    return classes


async def test_openai_agents_hook_records_calls_and_fingerprints(reporter, monkeypatch, tmp_path):
    classes = _agents(monkeypatch)
    assert hook_openai_agents() is True
    assert hook_openai_agents() is True  # idempotent: not wrapped twice
    srv = classes["MCPServerStdio"]()
    assert len(await srv.list_tools()) == 1
    await srv.call_tool("get_weather", {"q": "x"})
    await srv.call_tool(tool_name="bad", arguments={})
    evs = list(reporter._q)
    assert [(e["tool_name"], e["outcome"], e["error_class"]) for e in evs] == [
        ("get_weather", "ok", None),
        ("bad", "error", "ToolError"),
    ]
    assert evs[0]["schema_fingerprint"] == fingerprint(SCHEMA)
    assert "mcp:weather::get_weather" in _state(tmp_path)  # no params: a label, never a locator


async def test_openai_agents_servers_are_keyed_by_their_locator(reporter, monkeypatch, tmp_path):
    classes = _agents(monkeypatch)
    assert hook_openai_agents() is True
    http = classes["MCPServerStreamableHttp"](
        name="w", params={"url": "https://user:pw@mcp.weather.test/mcp?api_key=s3cret"}
    )
    stdio = classes["MCPServerStdio"](
        name="w", params=SimpleNamespace(command="npx", args=["-y", "@w/mcp"]), schema=SCHEMA_V2
    )
    for _ in range(3):  # same tool name on two servers: never a mismatch
        await http.list_tools()
        await stdio.list_tools()
    await http.call_tool("get_weather", {})
    await stdio.call_tool("get_weather", {})
    assert _mismatches(reporter) == []
    state = _state(tmp_path)
    assert set(state) == {"https://mcp.weather.test/mcp::get_weather", "npx -y @w/mcp::get_weather"}
    assert "s3cret" not in json.dumps(state) and "pw@" not in json.dumps(state)
    fps = [e["schema_fingerprint"] for e in reporter._q]  # each call carries its server's
    assert fps == [fingerprint(SCHEMA), fingerprint(SCHEMA_V2)]


# ---- LangChain (langchain-mcp-adapters) ----------------------------------------------------

SESSIONS_SRC = """
from contextlib import asynccontextmanager

OPENED = []


@asynccontextmanager
async def create_session(connection, *, mcp_callbacks=None):
    session = Session(connection)
    OPENED.append(session)
    yield session
"""
TOOLS_SRC = """
from langchain_mcp_adapters.sessions import create_session


async def load_mcp_tools(session, *, connection=None):
    async with create_session(connection) as tool_session:
        await tool_session.initialize()
        listed = await tool_session.list_tools()

    def convert(tool):
        async def call_tool(**arguments):  # every call opens its own session
            async with create_session(connection) as tool_session:
                await tool_session.initialize()
                return await tool_session.call_tool(tool.name, arguments)

        return tool.name, call_tool

    return [convert(t) for t in listed.tools]
"""
CLIENT_SRC = """
from contextlib import asynccontextmanager

from langchain_mcp_adapters.sessions import create_session
from langchain_mcp_adapters.tools import load_mcp_tools


class MultiServerMCPClient:
    def __init__(self, connections):
        self.connections = connections

    @asynccontextmanager
    async def session(self, server_name, *, auto_initialize=True):
        async with create_session(self.connections[server_name]) as session:
            if auto_initialize:
                await session.initialize()
            yield session

    async def get_tools(self, *, server_name=None):
        out = []
        for connection in self.connections.values():
            out.extend(await load_mcp_tools(None, connection=connection))
        return out
"""


class LCSession:
    def __init__(self, connection):
        self.connection = connection

    async def initialize(self):
        return {"serverInfo": {"name": "lc-server", "version": "1"}}

    async def list_tools(self):
        schema = self.connection.get("_schema", SCHEMA)
        return ListToolsResult(tools=[Tool(name=self.connection["_tool"], inputSchema=schema)])

    async def call_tool(self, name, arguments=None):
        return {"isError": False}


def _langchain(monkeypatch) -> types.ModuleType:
    _install(monkeypatch, "langchain_mcp_adapters")
    _install(monkeypatch, "langchain_mcp_adapters.sessions", SESSIONS_SRC, Session=LCSession)
    _install(monkeypatch, "langchain_mcp_adapters.tools", TOOLS_SRC)
    return _install(monkeypatch, "langchain_mcp_adapters.client", CLIENT_SRC)


CONNECTIONS = {
    "weather": {
        "transport": "streamable_http",
        "url": "https://mcp.weather.test/mcp?api_key=s3cret",
        "_tool": "get_weather",
    },
    "docs": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@docs/mcp"],
        "_tool": "search",
    },
}


async def test_langchain_hook_observes_get_tools_and_every_tool_call(
    reporter, monkeypatch, tmp_path
):
    lc = _langchain(monkeypatch)
    assert hook_langchain() is True
    assert hook_langchain() is True  # idempotent
    client = lc.MultiServerMCPClient(CONNECTIONS)
    tools = dict(await client.get_tools())  # the usual LangGraph path: no session() involved
    await tools["get_weather"](q="Oslo")
    await tools["search"](q="hooks")
    evs = [(e["tool_name"], e["outcome"], e["schema_fingerprint"]) for e in reporter._q]
    assert evs == [
        ("get_weather", "ok", fingerprint(SCHEMA)),
        ("search", "ok", fingerprint(SCHEMA)),
    ]
    state = _state(tmp_path)
    assert set(state) == {"https://mcp.weather.test/mcp::get_weather", "npx -y @docs/mcp::search"}
    assert "s3cret" not in json.dumps(state)
    async with client.session("weather") as s:  # explicit sessions are wrapped too
        assert s._kintsua_wrapped is True
        await s.call_tool("get_weather", {"q": 1})
    assert len(reporter._q) == 3
    sessions = sys.modules["langchain_mcp_adapters.sessions"]
    assert all(getattr(s, "_kintsua_wrapped", False) for s in sessions.OPENED)


async def test_langchain_schema_change_is_a_mismatch_on_the_real_locator(reporter, monkeypatch):
    lc = _langchain(monkeypatch)
    assert hook_langchain() is True
    await lc.MultiServerMCPClient(CONNECTIONS).get_tools()
    changed = {k: {**v, "_schema": SCHEMA_V2} for k, v in CONNECTIONS.items() if k == "weather"}
    await lc.MultiServerMCPClient(changed).get_tools()
    (ev,) = _mismatches(reporter)
    assert ev["tool_name"] == "get_weather"
    assert ev["sample"]["server"] == "https://mcp.weather.test/mcp"  # a locator, no secrets


async def test_langchain_hook_never_breaks_a_session_it_cannot_describe(reporter, monkeypatch):
    lc = _langchain(monkeypatch)
    assert hook_langchain() is True
    odd = {"x": {"transport": "stdio", "command": "srv", "args": 5, "_tool": "t"}}  # not a list
    tools = dict(await lc.MultiServerMCPClient(odd).get_tools())
    assert await tools["t"](q=1) == {"isError": False}  # the agent's call still works


# ---- Google ADK ----------------------------------------------------------------------------


def _adk(monkeypatch) -> types.ModuleType:
    class McpTool:
        def __init__(self, *, mcp_tool, mcp_session_manager):
            self.name, self._mcp_tool = mcp_tool.name, mcp_tool
            self._mcp_session_manager = mcp_session_manager

        async def run_async(self, *, args, tool_context):
            return {"isError": False}

    class MCPTool(McpTool):  # deprecated alias, what McpToolset.get_tools builds today
        pass

    class MCPSessionManager:
        def __init__(self, connection_params):
            self._connection_params = connection_params

    class McpToolset:
        def __init__(self, *, connection_params, schema=SCHEMA):
            self._connection_params, self.schema = connection_params, schema
            self._mcp_session_manager = MCPSessionManager(connection_params)

        async def get_tools(self, readonly_context=None):
            tool = Tool(name="search", inputSchema=self.schema)
            return [MCPTool(mcp_tool=tool, mcp_session_manager=self._mcp_session_manager)]

    class MCPToolset(McpToolset):  # deprecated alias
        pass

    _install(monkeypatch, "google")
    _install(monkeypatch, "google.adk")
    _install(monkeypatch, "google.adk.tools")
    return _install(
        monkeypatch,
        "google.adk.tools.mcp_tool",
        McpTool=McpTool,
        MCPTool=MCPTool,
        McpToolset=McpToolset,
        MCPToolset=MCPToolset,
    )


HTTP_PARAMS = SimpleNamespace(url="https://api.githubcopilot.com/mcp/", headers={"a": "b"})
STDIO_PARAMS = SimpleNamespace(server_params=SimpleNamespace(command="npx", args=["-y", "@d/mcp"]))


async def test_google_adk_hook_observes_the_current_mcptoolset(reporter, monkeypatch, tmp_path):
    adk = _adk(monkeypatch)
    assert hook_google_adk(sample=True) is True
    assert hook_google_adk(sample=True) is True  # idempotent
    tools = await adk.McpToolset(connection_params=HTTP_PARAMS).get_tools()
    await tools[0].run_async(args={"q": "x"}, tool_context=None)
    (ev,) = list(reporter._q)
    assert ev["tool_name"] == "search" and ev["sample"] == {"arg_keys": ["q"]}
    assert ev["schema_fingerprint"] == fingerprint(SCHEMA)
    assert set(_state(tmp_path)) == {"https://api.githubcopilot.com/mcp/::search"}


async def test_google_adk_toolsets_sharing_a_tool_name_never_flap(reporter, monkeypatch, tmp_path):
    adk = _adk(monkeypatch)
    assert hook_google_adk() is True
    github = adk.McpToolset(connection_params=HTTP_PARAMS)
    docs = adk.MCPToolset(connection_params=STDIO_PARAMS, schema=SCHEMA_V2)  # deprecated alias
    for _ in range(3):  # ADK lists the tools on every agent turn
        gh_tools, docs_tools = await github.get_tools(), await docs.get_tools()
    assert _mismatches(reporter) == []
    assert set(_state(tmp_path)) == {
        "https://api.githubcopilot.com/mcp/::search",
        "npx -y @d/mcp::search",
    }
    await gh_tools[0].run_async(args={}, tool_context=None)
    await docs_tools[0].run_async(args={}, tool_context=None)
    fps = [e["schema_fingerprint"] for e in reporter._q]
    assert fps == [fingerprint(SCHEMA), fingerprint(SCHEMA_V2)]
