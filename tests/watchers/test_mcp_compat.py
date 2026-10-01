"""The mcp 1.x/2.x compat layer: headers, stdio parsing, pagination, error flattening."""

from __future__ import annotations

import pytest

from kintsua.watchers._mcp_compat import (
    MCPConnectError,
    MCPError,
    build_http_headers,
    describe_exception,
    expand_env,
    flatten_exception,
    inherited_environment,
    is_http_locator,
    list_tools_all,
    parse_stdio_locator,
    to_wire,
)


def test_is_http_locator():
    assert is_http_locator("https://mcp.example.com/mcp") and is_http_locator("HTTP://x")
    assert not is_http_locator("npx -y pkg") and not is_http_locator("python -m srv")


def test_build_http_headers_with_auth_env(monkeypatch):
    monkeypatch.setenv("ACME_MCP_TOKEN", "sekret")
    monkeypatch.setenv("ACME_ORG", "org-1")
    h = build_http_headers(
        {"auth_env": "ACME_MCP_TOKEN", "headers": {"X-Org": "${ACME_ORG}"}, "user_agent": "ua/1"},
        locator="https://acme/mcp",
    )
    assert h == {"User-Agent": "ua/1", "X-Org": "org-1", "Authorization": "Bearer sekret"}
    monkeypatch.delenv("ACME_MCP_TOKEN")
    with pytest.raises(MCPConnectError, match="ACME_MCP_TOKEN is not set"):
        build_http_headers({"auth_env": "ACME_MCP_TOKEN"}, locator="https://acme/mcp")
    assert build_http_headers(None)["User-Agent"].startswith("kintsua-watcher/")
    assert expand_env("a-${MISSING_VAR_X}-b", env={}) == "a--b"


def test_parse_stdio_locator_merges_args_env_cwd(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("TOOL_KEY", "k-1")
    params = parse_stdio_locator(
        'npx -y @scope/pkg --root "/tmp/my dir"',
        {"args": ["--flag"], "env": {"API_KEY": "${TOOL_KEY}"}, "cwd": "/tmp"},
    )
    assert params.command == "npx"
    assert params.args == ["-y", "@scope/pkg", "--root", "/tmp/my dir", "--flag"]
    assert params.env["API_KEY"] == "k-1"
    assert params.env["HTTPS_PROXY"] == "http://proxy:3128"  # proxy/TLS settings are inherited
    assert "GITHUB_TOKEN" not in params.env  # secrets are not
    assert params.cwd == "/tmp"
    assert "HTTPS_PROXY" in inherited_environment()
    with pytest.raises(MCPConnectError):
        parse_stdio_locator("   ")
    # a stdio server's auth_env goes into its own environment
    monkeypatch.setenv("SRV_TOKEN", "t")
    assert parse_stdio_locator("uvx srv", {"auth_env": "SRV_TOKEN"}).env["SRV_TOKEN"] == "t"


class _Result:
    def __init__(self, tools, cursor):
        self.tools, self.cursor = tools, cursor

    def model_dump(self, **_):
        out = {"tools": self.tools}
        if self.cursor:
            out["nextCursor"] = self.cursor
        return out


class _Session:
    def __init__(self):
        self.pages = {
            None: _Result([{"name": "a"}, {"name": "b"}], "p2"),
            "p2": _Result([{"name": "c"}], "p3"),
            "p3": _Result([{"name": "d"}], "p3"),  # a server that loops its cursor
        }
        self.calls = []

    async def list_tools(self, *, params=None):
        cursor = params.cursor if params is not None else None
        self.calls.append(cursor)
        return self.pages[cursor]


async def test_list_tools_all_paginates_and_stops_on_cursor_loops():
    s = _Session()
    tools = await list_tools_all(s)
    assert [t["name"] for t in tools] == ["a", "b", "c", "d"]
    assert s.calls == [None, "p2", "p3"]


def test_to_wire_and_exception_flattening():
    assert to_wire(None) is None and to_wire({"x": 1}) == {"x": 1}
    inner = MCPError(-32603, "Server returned an error response")
    group = ExceptionGroup("outer", [ExceptionGroup("inner", [inner]), ValueError("bad")])
    leaves = flatten_exception(group)
    assert [type(e).__name__ for e in leaves] == ["MCPError", "ValueError"]
    text = describe_exception(group)
    assert "MCPError: Server returned an error response" in text and "ValueError: bad" in text
