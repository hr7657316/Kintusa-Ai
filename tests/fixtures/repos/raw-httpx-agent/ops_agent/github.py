import httpx


async def open_issue(owner: str, repo: str, title: str, body: str, token: str) -> int:
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client:
        r = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues",
            json={"title": title, "body": body, "labels": ["ops-agent"]},
        )
        r.raise_for_status()
        return r.json()["number"]


def latest_release(owner: str, repo: str) -> str:
    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest", follow_redirects=True
    )
    return resp.json()["tag_name"]
