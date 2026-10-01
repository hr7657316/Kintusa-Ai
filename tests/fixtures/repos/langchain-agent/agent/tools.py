import httpx
from langchain_core.tools import tool

ZENDESK = "https://acme.zendesk.com/api/v2"


@tool
async def ticket(ticket_id: int) -> dict:
    """Fetch a Zendesk ticket."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ZENDESK}/tickets/{ticket_id}.json")
        r.raise_for_status()
        return r.json()


@tool
async def search_web(query: str) -> list[dict]:
    """Search the web with Exa."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.exa.ai/search", json={"query": query, "numResults": 5}
        )
        return r.json()["results"]
