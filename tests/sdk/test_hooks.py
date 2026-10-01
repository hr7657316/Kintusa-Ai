"""Framework hooks return False when the framework is absent and patch stand-in framework
modules (installed into sys.modules) otherwise, recording calls and fingerprints."""

from __future__ import annotations

import json
import sys
import types
from contextlib import asynccontextmanager

import pytest
from mcp.types import Tool

from kintsua.sdk import hook_google_adk, hook_langchain, hook_openai_agents

SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


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


def _install(monkeypatch, name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _state(tmp_path) -> dict:
    return json.loads(next((tmp_path / "state").glob("*.json")).read_text())


async def test_openai_agents_hook_records_calls_and_fingerprints(reporter, monkeypatch, tmp_path):
    class MCPServerStdio:
        name = "weather"

        async def list_tools(self, *args, **kwargs):
            return [Tool(name="get_weather", inputSchema=SCHEMA)]

        async def call_tool(self, tool_name, arguments):
            return {"isError": tool_name == "bad"}

    agents = _install(monkeypatch, "agents")
    agents.mcp = _install(monkeypatch, "agents.mcp", MCPServerStdio=MCPServerStdio)
    assert hook_openai_agents() is True
    assert hook_openai_agents() is True  # idempotent: not wrapped twice
    srv = MCPServerStdio()
    assert len(await srv.list_tools()) == 1
    await srv.call_tool("get_weather", {"q": "x"})
    await srv.call_tool("bad", {})
    evs = list(reporter._q)
    assert [(e["tool_name"], e["outcome"], e["error_class"]) for e in evs] == [
        ("get_weather", "ok", None),
        ("bad", "error", "ToolError"),
    ]
    assert "weather::get_weather" in _state(tmp_path)


async def test_langchain_hook_wraps_every_session(reporter, monkeypatch):
    class Session:
        async def call_tool(self, name, arguments=None):
            return {"isError": False}

    class MultiServerMCPClient:
        @asynccontextmanager
        async def session(self, server_name, *, auto_initialize=True):
            yield Session()

    pkg = _install(monkeypatch, "langchain_mcp_adapters")
    pkg.client = _install(
        monkeypatch, "langchain_mcp_adapters.client", MultiServerMCPClient=MultiServerMCPClient
    )
    assert hook_langchain() is True
    async with MultiServerMCPClient().session("weather") as s:
        assert s._kintsua_wrapped is True
        await s.call_tool("get_weather", {"q": 1})
    ev = list(reporter._q)[0]
    assert ev["tool_name"] == "get_weather" and ev["outcome"] == "ok"


async def test_google_adk_hook(reporter, monkeypatch, tmp_path):
    class MCPTool:
        def __init__(self, name):
            self.name = name
            self._mcp_tool = Tool(name=name, inputSchema=SCHEMA)

        async def run_async(self, *, args, tool_context):
            return {"isError": False}

    class MCPToolset:
        async def get_tools(self, readonly_context=None):
            return [MCPTool("get_weather")]

    google = _install(monkeypatch, "google")
    google.adk = _install(monkeypatch, "google.adk")
    google.adk.tools = _install(monkeypatch, "google.adk.tools")
    google.adk.tools.mcp_tool = _install(
        monkeypatch, "google.adk.tools.mcp_tool", MCPTool=MCPTool, MCPToolset=MCPToolset
    )
    assert hook_google_adk(sample=True) is True
    tools = await MCPToolset().get_tools()
    await tools[0].run_async(args={"q": "x"}, tool_context=None)
    ev = list(reporter._q)[0]
    assert ev["tool_name"] == "get_weather" and ev["sample"] == {"arg_keys": ["q"]}
    assert "google-adk::get_weather" in _state(tmp_path)
