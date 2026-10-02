import aiohttp

POINTS = "https://api.weather.gov/points"


async def forecast(lat: float, lon: float) -> dict:
    async with aiohttp.ClientSession(headers={"User-Agent": "ops-agent"}) as session:
        async with session.get(f"{POINTS}/{lat},{lon}") as resp:
            data = await resp.json()
        async with session.get(data["properties"]["forecast"]) as resp:
            return await resp.json()
