"""A tiny MCP server over stdio for the watcher integration test.

``KINTSUA_MCP_VARIANT=v1`` (default), ``units-required`` or ``v2``:

* v1: tools ``get_weather(city: str, units: str = "metric", days: int = 1)`` and
  ``search(query: str, limit: int = 10)``; one resource.
* v2: ``get_weather`` makes ``units`` required (required_added), changes ``days`` to a
  string (type_changed) and reworded its description (text_changed); ``search`` is gone
  (removed); ``lookup`` is new (added); a prompt appears (added).

Works with mcp 2.x (``MCPServer``) and 1.x (``FastMCP``).
"""

from __future__ import annotations

import os

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

VARIANT = os.environ.get("KINTSUA_MCP_VARIANT", "v1")

VERSIONS = {"v1": "1.0.0", "units-required": "1.1.0"}
srv = MCPServer("kintsua-fixture", version=VERSIONS.get(VARIANT, "2.0.0"))

if VARIANT == "v1":

    @srv.tool(description="Get the current weather for a city")
    def get_weather(city: str, units: str = "metric", days: int = 1) -> str:
        return f"{city} {units} {days}"

    @srv.tool(description="Search the knowledge base")
    def search(query: str, limit: int = 10) -> str:
        return f"{query} {limit}"

elif VARIANT == "units-required":
    # Only one change: ``units`` becomes required (the fixer's required_param case).

    @srv.tool(description="Get the current weather for a city")
    def get_weather(city: str, units: str, days: int = 1) -> str:
        return f"{city} {units} {days}"

    @srv.tool(description="Search the knowledge base")
    def search(query: str, limit: int = 10) -> str:
        return f"{query} {limit}"

else:

    @srv.tool(description="Get the current weather for a city (units are now required)")
    def get_weather(city: str, units: str, days: str = "1") -> str:
        return f"{city} {units} {days}"

    @srv.tool(description="Look up an entity by id")
    def lookup(entity_id: str) -> str:
        return entity_id

    @srv.prompt(description="Summarise a document")
    def summarize(text: str) -> str:
        return f"Summarise: {text}"


@srv.resource("kintsua://readme", description="Fixture readme")
def readme() -> str:
    return "hello"


if __name__ == "__main__":
    srv.run(transport="stdio")
