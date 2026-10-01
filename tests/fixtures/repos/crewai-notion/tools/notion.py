import os

import requests
from crewai.tools import BaseTool

NOTION_API = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {os.environ.get('NOTION_TOKEN')}",
    "Notion-Version": "2022-06-28",
}


class NotionTool(BaseTool):
    name: str = "notion_search"
    description: str = "Search Notion pages"

    def _run(self, query: str) -> str:
        resp = requests.post(
            f"{NOTION_API}/search", headers=HEADERS, json={"query": query, "page_size": 5}
        )
        resp.raise_for_status()
        return str(resp.json()["results"])


def get_page(page_id: str) -> dict:
    return requests.get(f"{NOTION_API}/pages/{page_id}", headers=HEADERS, timeout=10).json()


def query_database(database_id: str, filter_: dict) -> dict:
    url = NOTION_API + "/databases/" + database_id + "/query"
    return requests.post(url, headers=HEADERS, json={"filter": filter_}).json()
