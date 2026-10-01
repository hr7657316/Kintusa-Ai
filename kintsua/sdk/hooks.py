"""One-line framework hooks: patch the framework's MCP classes when importable; True if hooked."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from kintsua.sdk.report import get_reporter
from kintsua.sdk.wrap import Opt, call_observer, check_tools, observe, wrap_mcp

log = logging.getLogger("kintsua.sdk")


def _patch(cls: Any, attr: str, factory: Any) -> bool:  # cls.attr = factory(original), once
    orig, mark = getattr(cls, attr, None), f"_kintsua_{attr}"
    if callable(orig) and not getattr(cls, mark, False):
        setattr(cls, attr, factory(orig))
        setattr(cls, mark, True)
    return callable(orig)


def _self_name(a: tuple, kw: dict) -> tuple[Any, Any]:  # call_tool(self, tool_name, arguments)
    name = a[1] if len(a) > 1 else kw.get("tool_name")
    return name, (a[2] if len(a) > 2 else kw.get("arguments"))


def _self_attr(a: tuple, kw: dict) -> tuple[Any, Any]:  # run_async(self, *, args, tool_context)
    return (getattr(a[0], "name", None) if a else None), kw.get("args")


def hook_openai_agents(repo: Opt = None, key: Opt = None, sample: bool = False) -> bool:
    """OpenAI Agents SDK: timed ``call_tool`` + fingerprint-checked ``list_tools`` on MCPServer*."""
    try:
        from agents import mcp as agents_mcp

        rep, cache, hooked = get_reporter(repo, key), {}, False
        calls = call_observer(rep, cache, sample or rep.sample, _self_name)
        listed = lambda a, kw, t0, res, exc: check_tools(  # noqa: E731
            rep, cache, res, str(getattr(a[0], "name", None) or "openai-agents")
        )
        for cls_name in ("MCPServerStdio", "MCPServerSse", "MCPServerStreamableHttp"):
            cls = getattr(agents_mcp, cls_name, None)
            if cls is not None:
                hooked |= _patch(cls, "call_tool", lambda o: observe(o, calls))
                hooked |= _patch(cls, "list_tools", lambda o: observe(o, listed))
        return hooked
    except Exception as e:  # noqa: BLE001
        log.debug("kintsua: hook_openai_agents skipped: %s", e)
        return False


def hook_langchain(repo: Opt = None, key: Opt = None, sample: bool = False) -> bool:
    """LangChain: every ``MultiServerMCPClient.session()`` (used per tool call) gets ``wrap_mcp``."""
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient as cls

        def factory(orig: Any) -> Any:
            @asynccontextmanager
            async def session(self: Any, server_name: Any = None, *a: Any, **kw: Any) -> Any:
                async with orig(self, server_name, *a, **kw) as s:
                    yield wrap_mcp(s, repo, key, sample, server=f"langchain:{server_name}")

            return session

        return _patch(cls, "session", factory)
    except Exception as e:  # noqa: BLE001
        log.debug("kintsua: hook_langchain skipped: %s", e)
        return False


def hook_google_adk(repo: Opt = None, key: Opt = None, sample: bool = False) -> bool:
    """Google ADK: timed ``MCPTool.run_async`` + fingerprint-checked ``MCPToolset.get_tools``."""
    try:
        from google.adk.tools import mcp_tool as adk

        rep, cache = get_reporter(repo, key), {}
        calls = call_observer(rep, cache, sample or rep.sample, _self_attr)
        listed = lambda a, kw, t0, res, exc: check_tools(rep, cache, res, "google-adk")  # noqa: E731
        hooked = _patch(adk.MCPTool, "run_async", lambda o: observe(o, calls))
        if getattr(adk, "MCPToolset", None) is not None:
            hooked |= _patch(adk.MCPToolset, "get_tools", lambda o: observe(o, listed))
        return hooked
    except Exception as e:  # noqa: BLE001
        log.debug("kintsua: hook_google_adk skipped: %s", e)
        return False
