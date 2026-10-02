"""Exact dependency and call-site counts for the five Python agent fixtures and the
TypeScript one (``tests/fixtures/repos``)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from kintsua.indexer.run import extract_tree

REPOS = Path(__file__).resolve().parents[1] / "fixtures" / "repos"

# repo -> (dependencies, call sites, {(kind, name): call_site_count}, files parsed)
EXPECTED = {
    "langchain-agent": (
        15,
        8,
        {
            ("pypi", "langchain"): 0,
            ("pypi", "langchain-openai"): 0,
            ("pypi", "langchain-core"): 0,
            ("pypi", "langgraph"): 1,
            ("pypi", "langchain-mcp-adapters"): 1,
            ("pypi", "slack_sdk"): 2,
            ("pypi", "mcp"): 2,
            ("pypi", "pydantic"): 0,
            ("npm", "@modelcontextprotocol/server-github"): 0,
            ("mcp", "npx -y @modelcontextprotocol/server-github"): 0,
            ("mcp", "https://mcp.deepwiki.com/mcp"): 0,
            ("mcp", "python servers/math_server.py"): 0,
            ("mcp", "http://localhost:8000/mcp"): 0,
            ("http", "acme.zendesk.com"): 1,
            ("http", "api.exa.ai"): 1,
        },
        7,
    ),
    "google-adk-agent": (
        8,
        6,
        {
            ("pypi", "google-adk"): 1,
            ("pypi", "google-genai"): 0,
            ("pypi", "python-dotenv"): 0,
            ("pypi", "mcp"): 0,
            ("npm", "@modelcontextprotocol/server-filesystem"): 0,
            ("mcp", "npx -y @modelcontextprotocol/server-filesystem"): 2,
            ("mcp", "https://mcp.notion.com/mcp"): 0,
            ("http", "api.github.com"): 3,
        },
        4,
    ),
    "openai-agents-stripe": (
        5,
        6,
        {
            ("pypi", "openai-agents"): 1,
            ("pypi", "stripe"): 5,
            ("pypi", "mcp-server-fetch"): 0,
            ("mcp", "uvx mcp-server-fetch"): 0,
            ("mcp", "https://mcp.stripe.com"): 0,
        },
        5,
    ),
    "crewai-notion": (
        4,
        7,
        {
            ("pypi", "crewai"): 1,
            ("pypi", "crewai-tools"): 0,
            ("pypi", "slack_sdk"): 3,
            ("http", "api.notion.com"): 3,
        },
        4,
    ),
    "raw-httpx-agent": (
        7,
        8,
        {
            ("pypi", "anthropic"): 2,
            ("pypi", "pyyaml"): 0,
            ("pypi", "respx"): 0,
            ("http", "api.stripe.com"): 3,
            ("http", "api.github.com"): 2,
            ("http", "api.weather.gov"): 1,
            ("mcp", "https://mcp.deepwiki.com/mcp"): 0,
        },
        5,
    ),
    "ts-agent": (
        12,
        12,
        {
            ("npm", "stripe"): 3,
            ("npm", "@slack/web-api"): 1,
            ("npm", "@modelcontextprotocol/sdk"): 2,
            ("npm", "openai"): 1,
            ("npm", "zod"): 0,
            ("npm", "@modelcontextprotocol/server-filesystem"): 0,
            ("mcp", "https://mcp.linear.app/sse"): 0,
            ("mcp", "npx -y @modelcontextprotocol/server-filesystem ."): 0,
            ("mcp", "https://mcp.deepwiki.com/mcp"): 1,
            ("http", "api.github.com"): 1,
            ("http", "api.notion.com"): 2,
            ("http", "api.weather.gov"): 1,
        },
        6,
    ),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_counts(name):
    deps, sites, per_dep, files = EXPECTED[name]
    r = extract_tree(REPOS / name)
    assert r.stats.parse_errors == 0, r.stats.errors
    assert r.stats.files_parsed == files
    assert {d.key for d in r.dependencies} == set(per_dep)
    assert len(r.dependencies) == deps
    counts = Counter(c.dependency_key for c in r.call_sites)
    assert {k: counts.get(k, 0) for k in per_dep} == per_dep
    assert len(r.call_sites) == sites
    # every call site points at a dependency that exists
    assert all(r.dependency(*c.dependency_key) is not None for c in r.call_sites)


def test_langchain_fixture_details():
    r = extract_tree(REPOS / "langchain-agent")
    ops = {(c.file_path, c.operation) for c in r.call_sites}
    assert ("agent/tools.py", "GET /api/v2/tickets/{id}") in ops
    assert ("agent/tools.py", "POST /search") in ops
    assert ("agent/notify.py", "slack_sdk.WebClient.chat_postMessage") in ops
    gh = r.dependency("mcp", "npx -y @modelcontextprotocol/server-github")
    assert gh.meta["env_keys"] == ["GITHUB_PERSONAL_ACCESS_TOKEN"]
    assert gh.meta["config_file"] == ".mcp.json"
    weather = r.dependency("mcp", "http://localhost:8000/mcp")
    assert (
        weather.meta["server_name"] == "weather" and weather.meta["config_file"] == "agent/graph.py"
    )
    assert r.dependency("pypi", "pydantic").vendor is None
    assert r.dependency("pypi", "slack_sdk").version_constraint == ">=3.33"


def test_adk_fixture_details():
    r = extract_tree(REPOS / "google-adk-agent")
    fs = r.dependency("mcp", "npx -y @modelcontextprotocol/server-filesystem")
    assert fs.meta["dynamic_args"] is True  # TARGET_DIR was not a literal
    tools = sorted(c.operation for c in r.call_sites if c.dependency_kind == "mcp")
    assert tools == ["list_directory", "read_file"]
    http_ops = sorted(c.operation for c in r.call_sites if c.dependency_kind == "http")
    assert http_ops == [
        "GET /rate_limit",
        "GET /repos/{id}/{id}/issues",
        "POST /repos/{id}/{id}/issues",
    ]
    notion = r.dependency("mcp", "https://mcp.notion.com/mcp")
    assert notion.meta["header_keys"] == ["Authorization"] and notion.vendor == "notion"


def test_openai_agents_fixture_details():
    r = extract_tree(REPOS / "openai-agents-stripe")
    stripe_sites = [c for c in r.call_sites if c.dependency_name == "stripe"]
    assert sorted(c.operation for c in stripe_sites) == [
        "stripe.Charge.list",
        "stripe.Charge.list",
        "stripe.Customer.retrieve",
        "stripe.Refund.create",
        "stripe.StripeClient.v1.payment_intents.create",
    ]
    assert [c.in_tests for c in stripe_sites if c.file_path.startswith("tests/")] == [True]
    pi = next(c for c in stripe_sites if c.operation.endswith("payment_intents.create"))
    assert pi.args_shape == {
        "keys": ["params"],
        "body_keys": {"params": ["amount", "currency", "customer", "confirm"]},
    }
    assert r.dependency("pypi", "stripe").version_constraint == ">=11,<13"
    assert r.dependency("mcp", "https://mcp.stripe.com").meta["header_keys"] == ["Authorization"]


def test_raw_httpx_fixture_details():
    r = extract_tree(REPOS / "raw-httpx-agent")
    stripe_ops = sorted(c.operation for c in r.call_sites if c.dependency_name == "api.stripe.com")
    assert stripe_ops == ["GET /v1/charges/{id}", "GET /v1/customers", "POST /v1/charges"]
    assert {c.symbol for c in r.call_sites if c.dependency_name == "api.stripe.com"} == {
        "httpx.AsyncClient.get",
        "httpx.AsyncClient.post",
    }
    wx = next(c for c in r.call_sites if c.dependency_name == "api.weather.gov")
    assert wx.symbol == "aiohttp.ClientSession.get" and wx.operation == "GET /points/{id}"
    assert r.dependency("pypi", "respx").meta["dev"] is True


def test_ts_fixture_details():
    r = extract_tree(REPOS / "ts-agent")
    tool = next(c for c in r.call_sites if c.dependency_kind == "mcp")
    assert (tool.operation, tool.symbol, tool.args_shape) == (
        "ask_question",
        "call_tool",
        {"keys": ["repoName", "question"]},
    )
    assert tool.file_path == "src/mcp.ts"
    ops = {c.operation for c in r.call_sites if c.dependency_kind == "http"}
    assert ops == {
        "POST /repos/{id}/{id}/issues",
        "POST /v1/search",
        "GET /v1/pages/{id}",
        "GET /points/{id}",
    }
    assert r.dependency("npm", "@modelcontextprotocol/sdk").version_constraint == "^1.10.0"
    assert (
        r.dependency("mcp", "https://mcp.linear.app/sse").meta["config_file"] == ".cursor/mcp.json"
    )
