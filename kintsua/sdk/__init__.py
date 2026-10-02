"""Kintsua runtime SDK (stdlib + httpx): ``wrap_mcp``, ``wrap_http``, ``hook_*``. No key: no-ops;
nothing raises into the agent; no payloads leave unless ``sample=True`` (argument keys only)."""

from __future__ import annotations

from kintsua.sdk.hooks import hook_google_adk, hook_langchain, hook_openai_agents
from kintsua.sdk.report import Reporter, configure, flush, get_reporter
from kintsua.sdk.wrap import wrap_http, wrap_mcp

__all__ = ["Reporter", "configure", "flush", "get_reporter", "wrap_http", "wrap_mcp"]
__all__ += ["hook_google_adk", "hook_langchain", "hook_openai_agents"]
