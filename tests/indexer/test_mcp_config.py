"""MCP config files, framework registrations in code, and tool-name call sites."""

from __future__ import annotations

import json
from pathlib import Path

from kintsua.indexer.mcp_config import (
    extract_mcp,
    package_from_command,
    parse_mcp_config,
    scan_config_files,
)


def test_claude_config_with_comments_env_names_only_and_packages():
    text = """{
      // Claude Code project config
      "mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github@1.2.3"],
                   "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_do_not_store_me"}},
        "linear": {"url": "https://mcp.linear.app/sse", "headers": {"Authorization": "Bearer x"}},
        "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
        "remote": {"command": "npx", "args": ["-y", "mcp-remote", "https://mcp.notion.com/mcp"]},
        "off": {"command": "node", "args": ["server.js"], "disabled": true},
      }
    }"""
    deps = parse_mcp_config(text, ".mcp.json")
    by = {(d.kind, d.name): d for d in deps}
    gh = by[("mcp", "npx -y @modelcontextprotocol/server-github@1.2.3")]
    assert gh.meta["env_keys"] == ["GITHUB_PERSONAL_ACCESS_TOKEN"]
    assert "ghp_do_not_store_me" not in json.dumps([d.model_dump(mode="json") for d in deps])
    assert gh.meta["transport"] == "stdio" and gh.meta["config_file"] == ".mcp.json"
    assert by[("npm", "@modelcontextprotocol/server-github")].version_constraint == "1.2.3"
    assert by[("mcp", "https://mcp.linear.app/sse")].meta["transport"] == "sse"
    assert by[("mcp", "https://mcp.linear.app/sse")].meta["header_keys"] == ["Authorization"]
    assert by[("mcp", "https://mcp.linear.app/sse")].vendor == "linear"
    assert ("pypi", "mcp-server-fetch") in by
    # mcp-remote: the dependency is the remote URL, the proxy package is noted
    assert by[("mcp", "https://mcp.notion.com/mcp")].meta["proxy"] == "mcp-remote"
    assert by[("mcp", "node server.js")].meta["disabled"] is True
    assert ("mcp", "npx -y mcp-remote https://mcp.notion.com/mcp") not in by


def test_vscode_servers_key_and_yaml_and_list_form():
    vs = parse_mcp_config(
        '{"servers": {"fs": {"type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}}}',
        ".vscode/mcp.json",
    )
    assert {d.name for d in vs} == {
        "npx -y @modelcontextprotocol/server-filesystem .",
        "@modelcontextprotocol/server-filesystem",
    }
    y = parse_mcp_config(
        "agent:\n  mcpServers:\n    - name: exa\n      url: https://mcp.exa.ai/mcp\n    - name: local\n      command: python -m my_tools\n",
        "config/agent.yaml",
    )
    names = {d.name: d for d in y}
    assert (
        "https://mcp.exa.ai/mcp" in names and names["https://mcp.exa.ai/mcp"].display_name == "exa"
    )
    assert "python -m my_tools" in names
    assert names["my-tools"].meta["inferred"] is True
    # not an mcp config at all
    assert parse_mcp_config('{"servers": {"a": {"url": "https://x"}}}', "random.json") == []
    assert parse_mcp_config("not json", ".mcp.json") == []


def test_package_from_command_variants():
    assert package_from_command("npx", ["-y", "@scope/pkg@2"])[0].name == "@scope/pkg"
    assert package_from_command("npx", ["-y", "@scope/pkg@2"])[0].version_constraint == "2"
    assert package_from_command("npx", ["--yes", "-p", "foo", "bar"])[0].name == "bar"
    assert package_from_command("pnpm", ["dlx", "some-mcp"])[0].name == "some-mcp"
    assert (
        package_from_command("uvx", ["--from", "mcp-server-git==1", "git-server"])[0].name
        == "mcp-server-git"
    )
    assert package_from_command("uvx", ["mcp-server-time[extra]>=1"])[0].version_constraint == ">=1"
    assert package_from_command("pipx", ["run", "mcp-server-x"])[0].kind == "pypi"
    assert package_from_command("uv", ["tool", "run", "mcp-server-x"])[0].name == "mcp-server-x"
    assert package_from_command("python3", ["-m", "mcp_server_fetch"])[0].name == "mcp-server-fetch"
    assert package_from_command("docker", ["run", "-i", "ghcr.io/x/y"]) == (None, None)
    assert package_from_command("node", ["dist/index.js"]) == (None, None)
    assert (
        package_from_command("/usr/local/bin/npx", ["mcp-remote", "https://h/mcp"])[1]
        == "https://h/mcp"
    )


def test_python_framework_registrations(tmp_path: Path):
    (tmp_path / "agent.py").write_text(
        """
import os
from agents.mcp import MCPServerStdio, MCPServerStreamableHttp
from langchain_mcp_adapters.client import MultiServerMCPClient
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters, StreamableHTTPConnectionParams
from mcp.client.streamable_http import streamablehttp_client

WEATHER = "http://localhost:8000/mcp"

async def main():
    fetch = MCPServerStdio(name="fetch", params={"command": "uvx", "args": ["mcp-server-fetch"]})
    stripe = MCPServerStreamableHttp(params={"url": "https://mcp.stripe.com", "headers": {"Authorization": f"Bearer {k}"}})
    client = MultiServerMCPClient({
        "math": {"command": "python", "args": ["math_server.py"], "transport": "stdio"},
        "weather": {"url": WEATHER, "transport": "streamable_http"},
    })
    fs = MCPToolset(connection_params=StdioServerParameters(command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", os.getcwd()]), tool_filter=["read_file", "list_directory"])
    remote = MCPToolset(connection_params=StreamableHTTPConnectionParams(url="https://mcp.example.com/mcp"))
    dyn = MCPServerStreamableHttp(params={"url": os.environ["MCP_URL"]})
    async with streamablehttp_client("https://mcp.exa.ai/mcp") as (r, w, _):
        pass
    client.beta.messages.create(mcp_servers=[{"type": "url", "url": "https://mcp.deepwiki.com/mcp", "name": "deepwiki"}])
    await session.call_tool("get_weather", {"city": "x"})
"""
    )
    r = extract_mcp(tmp_path)
    mcp = {d.name: d for d in r.dependencies if d.kind == "mcp"}
    assert set(mcp) == {
        "uvx mcp-server-fetch",
        "https://mcp.stripe.com",
        "python math_server.py",
        "http://localhost:8000/mcp",
        "npx -y @modelcontextprotocol/server-filesystem",
        "https://mcp.example.com/mcp",
        "https://mcp.exa.ai/mcp",
        "https://mcp.deepwiki.com/mcp",
    }
    assert mcp["https://mcp.stripe.com"].meta["header_keys"] == ["Authorization"]
    assert mcp["https://mcp.stripe.com"].meta["framework"] == "MCPServerStreamableHttp"
    assert mcp["python math_server.py"].meta["server_name"] == "math"
    assert mcp["npx -y @modelcontextprotocol/server-filesystem"].meta["dynamic_args"] is True
    assert mcp["https://mcp.deepwiki.com/mcp"].meta["server_name"] == "deepwiki"
    assert r.stats.dynamic_mcp_registrations == 1
    pkgs = {(d.kind, d.name) for d in r.dependencies if d.kind != "mcp"}
    assert pkgs == {
        ("pypi", "mcp-server-fetch"),
        ("npm", "@modelcontextprotocol/server-filesystem"),
    }
    sites = {(c.dependency_name, c.operation, c.symbol) for c in r.call_sites}
    assert ("npx -y @modelcontextprotocol/server-filesystem", "read_file", "tool_filter") in sites
    assert (
        "npx -y @modelcontextprotocol/server-filesystem",
        "list_directory",
        "tool_filter",
    ) in sites
    # call_tool("get_weather") with no known tool list and several servers in the file:
    # not attributable to one of them
    assert not any(op == "get_weather" for _, op, _ in sites)
    assert r.stats.unattributed_tool_calls == 1


def test_typescript_registrations_and_calltool(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mcp.ts").write_text(
        """
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
const client = new Client({ name: "agent", version: "1.0" });
const transport = new StreamableHTTPClientTransport(new URL("https://mcp.example.com/mcp"));
const stdio = new StdioClientTransport({ command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "."], env: { HOME: "x" } });
const multi = new MultiServerMCPClient({ mcpServers: { linear: { url: "https://mcp.linear.app/sse", transport: "sse" } } });
const agent = new Agent({ mcpServers: { fetch: { command: "uvx", args: ["mcp-server-fetch"] } } });
await client.connect(transport);
const res = await client.callTool({ name: "search_docs", arguments: { query: q, limit: 3 } });
await anthropic.beta.messages.create({ model: "x", mcp_servers: [{ type: "url", url: "https://mcp.deepwiki.com/mcp", name: "dw" }] });
"""
    )
    r = extract_mcp(tmp_path)
    mcp = {d.name: d for d in r.dependencies if d.kind == "mcp"}
    assert set(mcp) == {
        "https://mcp.example.com/mcp",
        "npx -y @modelcontextprotocol/server-filesystem .",
        "https://mcp.linear.app/sse",
        "uvx mcp-server-fetch",
        "https://mcp.deepwiki.com/mcp",
    }
    assert mcp["npx -y @modelcontextprotocol/server-filesystem ."].meta["env_keys"] == ["HOME"]
    assert mcp["https://mcp.linear.app/sse"].meta["transport"] == "sse"
    # search_docs is unknown and the file registers several servers -> unattributed
    assert r.stats.unattributed_tool_calls == 1


def test_tool_names_from_callback_literal_scan_and_single_server_fallback(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"wx": {"url": "https://mcp.weather.example/mcp"}}})
    )
    (tmp_path / "bot.py").write_text(
        'TOOLS = ["get_forecast", "get_alerts"]\n'
        "async def run(session, city):\n"
        '    await session.call_tool("get_forecast", {"city": city, "days": 3})\n'
        '    await session.call_tool(name="unknown_tool", arguments={"a": 1})\n'
        '    r = await session.call_tool("get_forecast", arguments={"city": city})\n'
        '    print("get_alerts is nice")\n'
    )
    known = {"https://mcp.weather.example/mcp": ["get_forecast", "get_alerts"]}
    r = extract_mcp(tmp_path, tools_for_locator=lambda loc: known.get(loc))
    sites = sorted((c.line, c.operation, c.symbol, c.args_shape["keys"]) for c in r.call_sites)
    assert sites == [
        (1, "get_alerts", "literal", []),
        (1, "get_forecast", "literal", []),
        (3, "get_forecast", "call_tool", ["city", "days"]),
        (4, "unknown_tool", "call_tool", ["a"]),  # single server in the repo: attributed
        (5, "get_forecast", "call_tool", ["city"]),
    ]
    assert all(c.dependency_name == "https://mcp.weather.example/mcp" for c in r.call_sites)
    dep = r.dependency("mcp", "https://mcp.weather.example/mcp")
    assert dep.meta["tool_names"] == ["get_alerts", "get_forecast"]
    # no callback, single server: call_tool literals still attach, nothing else does
    r2 = extract_mcp(tmp_path)
    assert sorted(c.operation for c in r2.call_sites) == [
        "get_forecast",
        "get_forecast",
        "unknown_tool",
    ]


def test_callback_errors_do_not_break_extraction(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"url": "https://a.example/mcp"}}})
    )

    def boom(_loc: str):
        raise RuntimeError("server down")

    r = extract_mcp(tmp_path, tools_for_locator=boom)
    assert [d.name for d in r.dependencies] == ["https://a.example/mcp"]


def test_scan_skips_large_files_lockfiles_and_package_json(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps({"mcpServers": {"x": {"url": "https://x/mcp"}}})
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps({"mcpServers": {"y": {"url": "https://y/mcp"}}})
    )
    (tmp_path / "huge.json").write_text(
        '{"mcpServers": {"z": {"url": "https://z/mcp"}}, "pad": "' + "x" * 1_100_000 + '"}'
    )
    (tmp_path / "tiny.json").write_text(
        json.dumps({"mcpServers": {"ok": {"url": "https://ok/mcp"}}})
    )
    r = scan_config_files(tmp_path)
    assert [d.name for d in r.dependencies] == ["https://ok/mcp"]
