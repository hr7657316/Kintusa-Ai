"""The Kintsua runtime SDK (``pip install kintsua``; stdlib + httpx only): ``wrap_mcp`` for any
MCP client session, ``wrap_http`` for httpx / requests clients, ``hook_*`` for frameworks. No key
(``KINTSUA_KEY``) means every call is a no-op; nothing raises into the agent; no payloads leave
the process unless ``sample=True`` (argument keys only, never values)."""

from __future__ import annotations

from kintsua.sdk.hooks import hook_google_adk, hook_langchain, hook_openai_agents
from kintsua.sdk.report import Reporter, configure, flush, get_reporter
from kintsua.sdk.wrap import wrap_http, wrap_mcp

__all__ = ["Reporter", "configure", "flush", "get_reporter", "wrap_http", "wrap_mcp"]
__all__ += ["hook_google_adk", "hook_langchain", "hook_openai_agents"]
