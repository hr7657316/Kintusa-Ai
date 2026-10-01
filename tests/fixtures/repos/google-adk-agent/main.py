import asyncio

from google.adk.runners import InMemoryRunner

from agent.agent import root_agent


async def main() -> None:
    runner = InMemoryRunner(agent=root_agent, app_name="docs")
    async for event in runner.run_async(user_id="u", session_id="s", new_message="hi"):
        print(event)


if __name__ == "__main__":
    asyncio.run(main())
