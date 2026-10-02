import os

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StdioConnectionParams,
    StreamableHTTPConnectionParams,
)
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from mcp import StdioServerParameters

TARGET_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "docs"))

filesystem = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", TARGET_DIR],
        ),
        timeout=30,
    ),
    tool_filter=["read_file", "list_directory"],
)

notes = MCPToolset(
    connection_params=StreamableHTTPConnectionParams(
        url="https://mcp.notion.com/mcp",
        headers={"Authorization": f"Bearer {os.environ.get('NOTION_TOKEN', '')}"},
    ),
)

root_agent = LlmAgent(
    model="gemini-2.5-flash",
    name="docs_agent",
    instruction="Help the user with the docs folder and their Notion notes.",
    tools=[filesystem, notes],
)
