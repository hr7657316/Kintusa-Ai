import os

import httpx


class StripeClient:
    BASE_URL = "https://api.stripe.com"

    def __init__(self, api_key: str | None = None):
        self._http = httpx.AsyncClient(
            base_url=self.BASE_URL,
            auth=(api_key or os.environ["STRIPE_KEY"], ""),
            timeout=20,
        )

    async def create_charge(self, amount: int, currency: str, source: str) -> dict:
        r = await self._http.post(
            "/v1/charges", data={"amount": amount, "currency": currency, "source": source}
        )
        r.raise_for_status()
        return r.json()

    async def get_charge(self, charge_id: str) -> dict:
        r = await self._http.get(f"/v1/charges/{charge_id}")
        return r.json()

    async def list_customers(self, email: str) -> list[dict]:
        r = await self._http.get("/v1/customers", params={"email": email, "limit": 3})
        return r.json()["data"]

    async def close(self) -> None:
        await self._http.aclose()
