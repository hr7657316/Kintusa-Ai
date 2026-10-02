"""Build the ReAct agent with tools from two MCP servers."""

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

WEATHER_URL = "http://localhost:8000/mcp"


async def build_agent():
    client = MultiServerMCPClient(
        {
            "math": {
                "command": "python",
                "args": ["servers/math_server.py"],
                "transport": "stdio",
            },
            "weather": {"url": WEATHER_URL, "transport": "streamable_http"},
        }
    )
    tools = await client.get_tools()
    model = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    agent = create_react_agent(model, tools)
    return agent
