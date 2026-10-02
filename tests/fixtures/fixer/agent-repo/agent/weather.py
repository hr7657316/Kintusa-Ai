"""Weather lookups through an MCP server (fixture for the fixer tests)."""

from __future__ import annotations

from typing import Any, Protocol


class ToolClient(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


def get_forecast(client: ToolClient, city: str) -> str:
    """Ask the weather MCP tool for a one-line forecast."""
    result = client.call_tool("get_weather", {"city": city})
    return f"{city}: {result['summary']}"
