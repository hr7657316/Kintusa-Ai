"""The curated package -> vendor map."""

from __future__ import annotations

import pytest

from kintsua.indexer import sdk_map

REQUIRED_PYTHON = [
    "stripe",
    "slack_sdk",
    "twilio",
    "github",
    "notion_client",
    "hubspot",
    "jira",
    "atlassian",
    "googleapiclient",
    "openai",
    "anthropic",
    "cohere",
    "mistralai",
    "groq",
    "together",
    "replicate",
    "pinecone",
    "weaviate",
    "qdrant_client",
    "chromadb",
    "elasticsearch",
    "algoliasearch",
    "sendgrid",
    "mailgun",
    "postmarker",
    "resend",
    "shopify",
    "square",
    "paypalrestsdk",
    "plaid",
    "zenpy",
    "intercom",
    "segment",
    "mixpanel",
    "amplitude",
    "ldclient",
    "sentry_sdk",
    "datadog",
    "pdpyras",
    "pyairtable",
    "asana",
    "trello",
    "dropbox",
    "boxsdk",
    "boto3",
    "azure",
    "google.cloud",
    "supabase",
    "firebase_admin",
    "tavily",
    "exa_py",
    "serpapi",
    "browserbase",
    "e2b",
    "firecrawl",
    "mcp",
    "langchain_mcp_adapters",
    "google.adk",
    "agents",
    "crewai",
    "langchain",
    "langgraph",
    "llama_index",
    "requests",
    "httpx",
    "aiohttp",
]
REQUIRED_NPM = [
    "stripe",
    "@slack/web-api",
    "twilio",
    "@octokit/rest",
    "octokit",
    "@notionhq/client",
    "@hubspot/api-client",
    "@linear/sdk",
    "googleapis",
    "openai",
    "@anthropic-ai/sdk",
    "@shopify/shopify-api",
    "@modelcontextprotocol/sdk",
    "axios",
    "got",
    "node-fetch",
]


def test_map_has_about_sixty_vendors():
    assert len(sdk_map.SDK_ENTRIES) >= 60
    assert len({e.vendor for e in sdk_map.SDK_ENTRIES}) >= 55


@pytest.mark.parametrize("root", REQUIRED_PYTHON)
def test_lookup_python_covers_required_packages(root):
    assert sdk_map.lookup_python(root) is not None, root


@pytest.mark.parametrize("pkg", REQUIRED_NPM)
def test_lookup_npm_covers_required_packages(pkg):
    assert sdk_map.lookup_npm(pkg) is not None, pkg


def test_python_lookup_is_longest_prefix():
    assert sdk_map.lookup_python("google.adk.tools.mcp_tool").vendor == "google"
    assert sdk_map.lookup_python("google.adk.tools.mcp_tool").kind == "mcp-framework"
    assert sdk_map.lookup_python("google.cloud.storage").vendor == "google-cloud"
    assert sdk_map.lookup_python("googleapiclient.discovery").display_name == "Google APIs"
    assert sdk_map.lookup_python("google.generativeai").kind == "llm"
    assert sdk_map.lookup_python("google") is None  # bare namespace is nobody
    assert sdk_map.python_import_root("google.adk.tools") == "google.adk"
    assert sdk_map.python_import_root("stripe.checkout.session") == "stripe"
    assert sdk_map.python_import_root("unknown.thing") == "unknown"


def test_npm_subpath_imports_resolve_to_the_package():
    assert (
        sdk_map.npm_package_root("@modelcontextprotocol/sdk/client/index.js")
        == "@modelcontextprotocol/sdk"
    )
    assert sdk_map.npm_package_root("stripe/lib/x") == "stripe"
    assert sdk_map.lookup_npm("@modelcontextprotocol/sdk/client/index.js").kind == "mcp-framework"


def test_pypi_lookup_is_case_and_separator_insensitive():
    assert sdk_map.lookup_pypi("PyGithub").vendor == "github"
    assert sdk_map.lookup_pypi("pygithub").vendor == "github"
    assert sdk_map.lookup_pypi("slack-sdk").vendor == "slack"
    assert sdk_map.lookup_pypi("Slack_SDK").vendor == "slack"
    assert sdk_map.lookup_pypi("not-a-real-package") is None


def test_vendor_for_host_exact_suffix_and_domain():
    assert sdk_map.vendor_for_host("api.stripe.com").vendor == "stripe"
    assert sdk_map.vendor_for_host("API.STRIPE.COM").vendor == "stripe"
    assert sdk_map.vendor_for_host("acme.zendesk.com").vendor == "zendesk"
    assert sdk_map.vendor_for_host("s3.eu-west-1.amazonaws.com").vendor == "aws"
    assert sdk_map.vendor_for_host("dashboard.stripe.com").vendor == "stripe"
    assert sdk_map.vendor_for_host("api.weather.gov") is None


def test_spec_for_host_requires_exact_host():
    assert sdk_map.spec_for_host("api.stripe.com").openapi_url.endswith("spec3.json")
    assert sdk_map.spec_for_host("dashboard.stripe.com") is None
    assert sdk_map.spec_for_host("api.notion.com") is None  # changelog only


def test_http_clients_are_not_watchable_vendors():
    for root in ("requests", "httpx", "aiohttp"):
        e = sdk_map.lookup_python(root)
        assert e.kind == "http-client" and not e.watchable and not e.is_vendor
    assert sdk_map.lookup_npm("axios").kind == "http-client"


def test_spec_urls_match_the_seed_list():
    assert sdk_map.lookup_python("stripe").openapi_url == (
        "https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json"
    )
    assert sdk_map.lookup_python("github").openapi_url == sdk_map.GITHUB_OPENAPI
    assert sdk_map.lookup_python("googleapiclient").openapi_format == "discovery"
    assert (
        sdk_map.lookup_python("notion_client").changelog_url
        == "https://developers.notion.com/page/changelog"
    )
