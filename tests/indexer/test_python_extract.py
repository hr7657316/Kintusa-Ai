"""Python extractor edge cases on inline snippets."""

from __future__ import annotations

from pathlib import Path

from kintsua.indexer.python_extract import (
    declared_python_packages,
    extract_python,
    extract_python_source,
    normalise_path,
    normalise_url,
)


def sites(src: str, path: str = "app/main.py"):
    r = extract_python_source(src, path)
    return r, {(c.dependency_kind, c.dependency_name, c.symbol, c.operation) for c in r.call_sites}


def test_aliased_imports_bind_to_the_package():
    r, s = sites(
        "import stripe as s\nfrom slack_sdk import WebClient as WC\n"
        "s.Charge.create(amount=1)\nWC(token='x').chat_postMessage(channel='c')\n"
    )
    assert ("pypi", "stripe", "stripe.Charge.create", "stripe.Charge.create") in s
    assert (
        "pypi",
        "slack_sdk",
        "slack_sdk.WebClient.chat_postMessage",
        "slack_sdk.WebClient.chat_postMessage",
    ) in s
    assert {d.name for d in r.dependencies} == {"stripe", "slack_sdk"}
    assert r.dependency("pypi", "stripe").vendor == "stripe"


def test_client_instances_module_self_with_and_typed_params():
    src = """
import httpx
from slack_sdk import WebClient

client = WebClient(token="t")

class Bot:
    def __init__(self):
        self.api = httpx.Client(base_url="https://api.stripe.com")

    def go(self, other: WebClient):
        client.chat_postMessage(channel="#a", text="hi")
        other.conversations_list(limit=5)
        self.api.get("/v1/balance")
        with httpx.Client() as c:
            c.post("https://api.openai.com/v1/chat/completions", json={"model": "x"})
"""
    r, s = sites(src)
    ops = {c.operation for c in r.call_sites}
    assert "slack_sdk.WebClient.chat_postMessage" in ops
    assert "slack_sdk.WebClient.conversations_list" in ops
    assert "GET /v1/balance" in ops
    assert "POST /v1/chat/completions" in ops
    hosts = {d.name for d in r.dependencies if d.kind == "http"}
    assert hosts == {"api.stripe.com", "api.openai.com"}
    # the http client package itself is never a dependency
    assert all(d.name != "httpx" for d in r.dependencies)


def test_factory_with_return_annotation_and_class_level_client():
    src = """
from slack_sdk import WebClient

def make() -> WebClient:
    return WebClient()

class Svc:
    client = WebClient(token="x")

    def run(self):
        c = make()
        c.auth_test()
        self.client.users_info(user="U1")
"""
    _, s = sites(src)
    assert (
        "pypi",
        "slack_sdk",
        "slack_sdk.WebClient.auth_test",
        "slack_sdk.WebClient.auth_test",
    ) in s
    assert (
        "pypi",
        "slack_sdk",
        "slack_sdk.WebClient.users_info",
        "slack_sdk.WebClient.users_info",
    ) in s


def test_constructors_and_factories_are_not_call_sites():
    src = """
import stripe, boto3
from openai import OpenAI
c = OpenAI()
s3 = boto3.client("s3")
x = stripe.StripeClient("k")
s3.put_object(Bucket="b")
boto3.client("s3").get_object(Bucket="b", Key="k")
c.chat.completions.create(model="m", messages=[])
"""
    r, _ = sites(src)
    ops = sorted(c.operation for c in r.call_sites)
    assert ops == [
        "boto3.s3.get_object",
        "boto3.s3.put_object",
        "openai.OpenAI.chat.completions.create",
    ]


def test_result_navigation_is_stripped_and_args_come_from_the_operation():
    src = """
import stripe
from googleapiclient.discovery import build
svc = build("drive", "v3")
for ch in stripe.Charge.list(limit=3).auto_paging_iter():
    pass
files = svc.files().list(q="name contains 'x'", pageSize=10).execute()
"""
    r, _ = sites(src)
    by_op = {c.operation: c for c in r.call_sites}
    assert by_op["stripe.Charge.list"].args_shape == {"keys": ["limit"]}
    assert by_op["googleapiclient.discovery.build.files.list"].args_shape == {
        "keys": ["q", "pageSize"]
    }
    assert len(r.call_sites) == 2


def test_fstring_percent_format_concat_and_constants():
    src = """
import requests
BASE = "https://api.notion.com/v1"
def f(page_id, db):
    requests.get(f"{BASE}/pages/{page_id}")
    requests.post(BASE + "/databases/" + db + "/query", json={"filter": {}})
    requests.delete("https://api.notion.com/v1/blocks/%s" % page_id)
    requests.patch("https://api.notion.com/v1/pages/{}".format(page_id), json={"archived": True})
    url = f"{BASE}/users/{page_id}"
    requests.get(url, params={"a": 1})
    requests.get(f"{unknown_base}/x")
    requests.request("PUT", "https://API.Notion.com:443/v1/comments?x=1")
"""
    r, _ = sites(src)
    ops = sorted(c.operation for c in r.call_sites)
    assert ops == [
        "DELETE /v1/blocks/{id}",
        "GET /v1/pages/{id}",
        "GET /v1/users/{id}",
        "PATCH /v1/pages/{id}",
        "POST /v1/databases/{id}/query",
        "PUT /v1/comments",
    ]
    assert {d.name for d in r.dependencies if d.kind == "http"} == {"api.notion.com"}
    assert r.dependency("http", "api.notion.com").vendor == "notion"
    shapes = {c.operation: c.args_shape for c in r.call_sites}
    assert shapes["POST /v1/databases/{id}/query"] == {
        "keys": ["json"],
        "body_keys": {"json": ["filter"]},
    }
    assert shapes["GET /v1/users/{id}"]["body_keys"] == {"params": ["a"]}


def test_base_url_joining_for_httpx_and_aiohttp():
    src = """
import httpx, aiohttp
API = "https://api.stripe.com/"
class C:
    def __init__(self):
        self.h = httpx.AsyncClient(base_url=API)
    async def go(self, cid):
        await self.h.get(f"v1/customers/{cid}")
        await self.h.post("/v1/charges", data={"amount": 1})
        async with aiohttp.ClientSession(base_url="https://api.github.com") as s:
            async with s.get("/rate_limit") as r:
                pass
        await self.h.get(cid)  # fully dynamic: dropped
"""
    r, _ = sites(src)
    ops = {(c.dependency_name, c.operation) for c in r.call_sites}
    assert ops == {
        ("api.stripe.com", "GET /v1/customers/{id}"),
        ("api.stripe.com", "POST /v1/charges"),
        ("api.github.com", "GET /rate_limit"),
    }
    assert {c.symbol for c in r.call_sites} == {
        "httpx.AsyncClient.get",
        "httpx.AsyncClient.post",
        "aiohttp.ClientSession.get",
    }


def test_url_normalisation():
    assert normalise_url("https://api.stripe.com:443/v1/charges/ch_1234567890?x=1#f") == (
        "api.stripe.com",
        "/v1/charges/{id}",
    )
    assert normalise_url("http://localhost:8080/api/") == ("localhost:8080", "/api")
    assert normalise_url("https://user:pw@api.x.com/v1/") == ("api.x.com", "/v1")
    assert normalise_url("https://\x00/v1") is None
    assert normalise_url("/v1/relative") is None
    assert normalise_path("/users/42/keys/550e8400-e29b-41d4-a716-446655440000/") == (
        "/users/{id}/keys/{id}"
    )
    assert normalise_path("/v1/\x00.json/x") == "/v1/{id}/x"


def test_args_shape_keys_and_dict_positional():
    r, _ = sites(
        "import stripe\nstripe.Charge.create(amount=1, currency='usd', **kw)\n"
        "stripe.Customer.modify('cus_1', {'email': 'a', 'name': 'b'})\n"
    )
    shapes = {c.operation: c.args_shape for c in r.call_sites}
    assert shapes["stripe.Charge.create"] == {"keys": ["amount", "currency"]}
    assert shapes["stripe.Customer.modify"] == {
        "keys": []
    }  # positional dict + id: not a kwargs call
    r2, _ = sites("import stripe\nstripe.Charge.create({'amount': 1, 'currency': 'usd'})\n")
    assert r2.call_sites[0].args_shape == {"keys": ["amount", "currency"]}


def test_call_tool_is_left_to_the_mcp_extractor_and_tests_are_flagged():
    src = "from mcp import ClientSession\nasync with ClientSession(r, w) as s:\n    await s.call_tool('x', {})\n    await s.list_tools()\n"
    r, _ = sites(src, "tests/test_x.py")
    assert [c.operation for c in r.call_sites] == ["mcp.ClientSession.list_tools"]
    assert r.call_sites[0].in_tests is True
    assert r.dependency("pypi", "mcp") is not None


def test_unknown_packages_and_local_modules_are_ignored():
    r, _ = sites(
        "import os\nimport mylib\nfrom . import x\nos.path.join('a')\nmylib.go(a=1)\nx.y()\n"
    )
    assert r.dependencies == [] and r.call_sites == []


def test_syntax_errors_are_counted_but_extraction_continues():
    r, _ = sites("import stripe\nstripe.Charge.create(amount=1)\ndef broken(:\n    pass\n")
    assert r.stats.parse_errors == 1
    assert [c.operation for c in r.call_sites] == ["stripe.Charge.create"]


def test_declared_packages_from_pyproject_and_requirements(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["stripe>=10", "PyGithub", "pydantic>=2", "httpx"]\n'
        '[project.optional-dependencies]\ndev = ["pytest", "respx"]\n'
        "[tool.poetry.dependencies]\npython = '^3.12'\nslack-sdk = {version = '^3'}\n"
    )
    (tmp_path / "requirements.txt").write_text(
        "-r requirements-base.txt\nnotion-client==2.2.1 # pinned\n"
    )
    (tmp_path / "requirements-base.txt").write_text("twilio>=9\n-e .\nhttps://example.com/x.whl\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import stripe\nstripe.Charge.create(amount=1)\n")
    declared = {d.canonical: d for d in declared_python_packages(tmp_path)}
    assert declared["stripe"].constraint == ">=10"
    assert declared["notion-client"].constraint == "==2.2.1"
    assert declared["twilio"].config_file == "requirements-base.txt"
    assert declared["respx"].dev is True
    assert declared["slack-sdk"].constraint == "^3"
    r = extract_python(tmp_path)
    names = {d.name: d for d in r.dependencies}
    # curated spelling wins, http clients and tooling are excluded, unknown ones stay
    assert set(names) == {
        "stripe",
        "PyGithub",
        "pydantic",
        "respx",
        "slack_sdk",
        "notion-client",
        "twilio",
    }
    assert names["pydantic"].vendor is None and names["pydantic"].meta["declared"] is True
    assert names["respx"].meta["dev"] is True
    assert names["stripe"].version_constraint == ">=10"
    assert names["stripe"].meta["config_file"] == "pyproject.toml"
    assert len(r.call_sites) == 1 and r.stats.files_parsed == 1


def test_large_files_and_vendored_dirs_are_skipped(tmp_path: Path):
    (tmp_path / "big.py").write_text("import stripe\n" + "x = 1\n" * 300_000)
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "a.py").write_text("import stripe\nstripe.Charge.create()\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "b.py").write_text("import stripe\nstripe.Charge.create()\n")
    (tmp_path / "ok.py").write_text("import stripe\nstripe.Charge.create()\n")
    r = extract_python(tmp_path)
    assert r.stats.files_seen == 2 and r.stats.skipped_large == 1 and r.stats.files_parsed == 1
    assert len(r.call_sites) == 1
