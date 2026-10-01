import asyncio

from agents import Agent, Runner
from agents.mcp import MCPServerStdio, MCPServerStreamableHttp

from billing.tools import charge_customer, refund


async def main(question: str) -> str:
    async with MCPServerStdio(
        name="fetch", params={"command": "uvx", "args": ["mcp-server-fetch"]}
    ) as fetch_server:
        async with MCPServerStreamableHttp(
            name="stripe",
            params={
                "url": "https://mcp.stripe.com",
                "headers": {"Authorization": "Bearer sk_test_placeholder"},
            },
            cache_tools_list=True,
        ) as stripe_server:
            agent = Agent(
                name="Billing",
                instructions="You handle billing questions.",
                mcp_servers=[fetch_server, stripe_server],
                tools=[charge_customer, refund],
            )
            result = await Runner.run(agent, question)
            return result.final_output


if __name__ == "__main__":
    print(asyncio.run(main("refund order 42")))
