"""Framework hooks: patch MCP classes when importable (True if hooked), servers keyed by locator."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from kintsua.sdk.report import Opt, Reporter, get_reporter
from kintsua.sdk.wrap import call_observer, check_tools, observe, pick_name, server_id, wrap_mcp

log = logging.getLogger("kintsua.sdk")


def _patch(cls: Any, attr: str, factory: Any) -> bool:  # cls.attr = factory(original), once
    orig, mark = getattr(cls, attr, None), f"_kintsua_{attr}"
    if callable(orig) and not getattr(cls, mark, False):
        setattr(cls, attr, factory(orig))
        setattr(cls, mark, True)
    return callable(orig)


def _hook(rep: Reporter, sample: bool, classes: list, attrs: tuple, srv: Any, name_of: Any) -> bool:
    caches: dict[str, dict[str, str]] = {}  # time attrs[0], fingerprint attrs[1], per srv(self)
    cache = lambda a: caches.setdefault(srv(a[0]), {})  # noqa: E731
    calls = call_observer(rep, cache, sample or rep.sample, name_of)
    listed = lambda a, kw, t0, res, exc: check_tools(rep, cache(a), res, srv(a[0]))  # noqa: E731
    hooked = [_patch(c, attrs[0], lambda o: observe(o, calls)) for c in classes if c]
    return any(hooked + [_patch(c, attrs[1], lambda o: observe(o, listed)) for c in classes if c])


def hook_openai_agents(repo: Opt = None, key: Opt = None, sample: bool = False) -> bool:
    """OpenAI Agents SDK: timed ``call_tool`` + fingerprint-checked ``list_tools`` on MCPServer*."""
    try:
        from agents import mcp as m

        classes = [getattr(m, f"MCPServer{n}", None) for n in ("Stdio", "Sse", "StreamableHttp")]
        srv = lambda s: server_id(getattr(s, "params", None), f"mcp:{getattr(s, 'name', '')}")  # noqa: E731
        nm = lambda a, kw: pick_name(a[1:], {"name": kw.get("tool_name"), **kw})  # noqa: E731
        return _hook(get_reporter(repo, key), sample, classes, ("call_tool", "list_tools"), srv, nm)
    except Exception as e:  # noqa: BLE001
        log.debug("kintsua: hook_openai_agents skipped: %s", e)
        return False


def hook_langchain(repo: Opt = None, key: Opt = None, sample: bool = False) -> bool:
    """LangChain: sessions from ``create_session`` (``get_tools()``, tool calls) get ``wrap_mcp``."""
    try:
        from langchain_mcp_adapters import client, sessions, tools

        def factory(orig: Any) -> Any:
            @asynccontextmanager
            async def create_session(connection: Any, *a: Any, **kw: Any) -> Any:
                async with orig(connection, *a, **kw) as s:
                    yield wrap_mcp(s, repo, key, sample, server=connection)

            return create_session

        return any([_patch(mod, "create_session", factory) for mod in (sessions, tools, client)])
    except Exception as e:  # noqa: BLE001
        log.debug("kintsua: hook_langchain skipped: %s", e)
        return False


def hook_google_adk(repo: Opt = None, key: Opt = None, sample: bool = False) -> bool:
    """Google ADK: timed ``McpTool.run_async`` + fingerprint-checked ``McpToolset.get_tools``."""
    try:
        from google.adk.tools import mcp_tool as adk

        names = ("McpTool", "McpToolset")  # MCPTool / MCPToolset: deprecated subclasses
        classes = [getattr(adk, n, getattr(adk, n.replace("Mcp", "MCP"), None)) for n in names]
        cp = lambda o: getattr(getattr(o, "_mcp_session_manager", o), "_connection_params", None)  # noqa: E731
        srv = lambda o: server_id(getattr(cp(o), "server_params", None) or cp(o), "mcp:google-adk")  # noqa: E731
        nm = lambda a, kw: (getattr(a[0], "name", None) if a else None, kw.get("args"))  # noqa: E731
        return _hook(get_reporter(repo, key), sample, classes, ("run_async", "get_tools"), srv, nm)
    except Exception as e:  # noqa: BLE001
        log.debug("kintsua: hook_google_adk skipped: %s", e)
        return False
