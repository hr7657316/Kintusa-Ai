import requests

API = "https://api.github.com"

session = requests.Session()
session.headers["Accept"] = "application/vnd.github+json"


def list_issues(owner: str, repo: str, state: str = "open") -> list[dict]:
    r = session.get(
        f"{API}/repos/{owner}/{repo}/issues",
        params={"state": state, "per_page": 50},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def create_issue(owner: str, repo: str, title: str, body: str) -> dict:
    r = session.post(API + f"/repos/{owner}/{repo}/issues", json={"title": title, "body": body})
    r.raise_for_status()
    return r.json()


def rate_limit() -> dict:
    return requests.get("https://api.github.com/rate_limit", timeout=5).json()
