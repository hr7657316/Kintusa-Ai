"""Curated map of SDK packages to vendors, API hosts, spec URLs and changelog pages.

The indexer uses it three ways: ``lookup_python(import_root)`` turns an ``import stripe``
into the Stripe vendor, ``lookup_npm(package)`` does the same for ``package.json`` and
``import ... from "stripe"``, and ``vendor_for_host(host)`` resolves a raw HTTP host to the
vendor's OpenAPI source. Spec / changelog URLs are the ones ``kintsua/seed/sources.yml``
uses so the sources created by the indexer and by the seed are the same rows.

Kinds: ``sdk`` (a vendor API client), ``llm`` (model provider), ``mcp-framework`` (MCP client
libraries and agent frameworks that register MCP servers), ``agent-framework`` (LangChain,
CrewAI ...: watched on the registry because major bumps break agents), ``http-client``
(requests/httpx/axios: the mechanism, not a dependency; the host is the dependency).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from packaging.utils import canonicalize_name

SdkKind = Literal["sdk", "llm", "mcp-framework", "agent-framework", "http-client"]

GITHUB_OPENAPI = (
    "https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/"
    "api.github.com/api.github.com.json"
)
HUBSPOT_CONTACTS_OPENAPI = (
    "https://raw.githubusercontent.com/HubSpot/HubSpot-public-api-spec-collection/HEAD/"
    "PublicApiSpecs/CRM/Contacts/Rollouts/424/v3/contacts.json"
)


@dataclass(frozen=True)
class SdkEntry:
    vendor: str  # slug used on Source.vendor / Dependency.meta["vendor"]
    display_name: str
    kind: SdkKind = "sdk"
    # dotted import prefixes ("stripe", "google.adk", "google.cloud") -> this entry
    python: tuple[str, ...] = ()
    # PyPI distribution names, spelled as the package watcher locator should be
    pypi: tuple[str, ...] = ()
    npm: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    openapi_url: str | None = None
    openapi_format: str | None = None  # "discovery" for Google discovery docs
    changelog_url: str | None = None
    extra_openapi: tuple[str, ...] = field(default=())  # other specs the seed watches

    @property
    def is_vendor(self) -> bool:
        return self.kind in ("sdk", "llm")

    @property
    def watchable(self) -> bool:
        """Packages of this kind get a registry source (http clients do not)."""
        return self.kind != "http-client"


def _e(vendor: str, display_name: str, **kw) -> SdkEntry:  # type: ignore[no-untyped-def]
    for key in ("python", "pypi", "npm", "hosts", "extra_openapi"):
        if key in kw and isinstance(kw[key], str):
            kw[key] = (kw[key],)
    return SdkEntry(vendor=vendor, display_name=display_name, **kw)


SDK_ENTRIES: tuple[SdkEntry, ...] = (
    # ------------------------------------------------------------- payments / commerce
    _e(
        "stripe",
        "Stripe",
        python="stripe",
        pypi="stripe",
        npm="stripe",
        hosts=("api.stripe.com", "files.stripe.com"),
        openapi_url="https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json",
        changelog_url="https://docs.stripe.com/changelog",
    ),
    _e(
        "shopify",
        "Shopify",
        python=("shopify", "ShopifyAPI"),
        pypi="ShopifyAPI",
        npm=("@shopify/shopify-api", "@shopify/admin-api-client", "shopify-api-node"),
        hosts=("admin.shopify.com",),
        changelog_url="https://shopify.dev/changelog",
    ),
    _e(
        "square",
        "Square",
        python=("square", "squareup"),
        pypi=("squareup", "square"),
        npm=("square", "squareup"),
        hosts=("connect.squareup.com", "connect.squareupsandbox.com"),
        changelog_url="https://developer.squareup.com/docs/changelog/connect",
    ),
    _e(
        "paypal",
        "PayPal",
        python=("paypalrestsdk", "paypalserversdk", "paypalcheckoutsdk"),
        pypi=("paypalrestsdk", "paypal-server-sdk", "paypal-checkout-serversdk"),
        npm=("@paypal/checkout-server-sdk", "@paypal/paypal-server-sdk", "paypal-rest-sdk"),
        hosts=("api-m.paypal.com", "api.paypal.com", "api-m.sandbox.paypal.com"),
        changelog_url="https://developer.paypal.com/api/rest/release-notes/",
    ),
    _e(
        "plaid",
        "Plaid",
        python="plaid",
        pypi="plaid-python",
        npm="plaid",
        hosts=("production.plaid.com", "sandbox.plaid.com", "development.plaid.com"),
        openapi_url="https://raw.githubusercontent.com/plaid/plaid-openapi/master/2020-09-14.yml",
        changelog_url="https://plaid.com/docs/api/versioning/",
    ),
    # ------------------------------------------------------------- collaboration
    _e(
        "slack",
        "Slack",
        python=("slack_sdk", "slack", "slack_bolt"),
        pypi=("slack_sdk", "slack-bolt", "slackclient"),
        npm=("@slack/web-api", "@slack/bolt", "@slack/webhook"),
        hosts=("slack.com", "api.slack.com", "hooks.slack.com"),
        openapi_url=(
            "https://raw.githubusercontent.com/slackapi/slack-api-specs/master/web-api/"
            "slack_web_openapi_v2.json"
        ),
        changelog_url="https://api.slack.com/changelog",
    ),
    _e(
        "github",
        "GitHub",
        python=("github", "githubkit", "ghapi"),
        pypi=("PyGithub", "githubkit", "ghapi"),
        npm=("@octokit/rest", "octokit", "@octokit/core", "@octokit/graphql", "@octokit/request"),
        hosts=("api.github.com", "uploads.github.com"),
        openapi_url=GITHUB_OPENAPI,
        changelog_url="https://docs.github.com/en/rest/overview/breaking-changes",
    ),
    _e(
        "notion",
        "Notion",
        python="notion_client",
        pypi="notion-client",
        npm="@notionhq/client",
        hosts=("api.notion.com",),
        changelog_url="https://developers.notion.com/page/changelog",
    ),
    _e(
        "hubspot",
        "HubSpot",
        python="hubspot",
        pypi="hubspot-api-client",
        npm="@hubspot/api-client",
        hosts=("api.hubapi.com", "api.hubspot.com"),
        openapi_url=HUBSPOT_CONTACTS_OPENAPI,
        changelog_url="https://developers.hubspot.com/changelog",
    ),
    _e(
        "linear",
        "Linear",
        python=("linear_sdk", "linear"),
        pypi=("linear-sdk",),
        npm="@linear/sdk",
        hosts=("api.linear.app",),
        changelog_url="https://linear.app/changelog",
    ),
    _e(
        "atlassian",
        "Atlassian (Jira / Confluence)",
        python=("jira", "atlassian"),
        pypi=("jira", "atlassian-python-api"),
        npm=("jira-client", "jira.js", "atlassian-connect-express"),
        hosts=("api.atlassian.com",),
        openapi_url="https://developer.atlassian.com/cloud/jira/platform/swagger-v3.v3.json",
        changelog_url="https://developer.atlassian.com/cloud/jira/platform/changelog/",
        extra_openapi=("https://developer.atlassian.com/cloud/confluence/swagger.v3.json",),
    ),
    _e(
        "google",
        "Google APIs",
        python=("googleapiclient", "google.oauth2", "google_auth_oauthlib"),
        pypi=("google-api-python-client", "google-auth", "google-auth-oauthlib"),
        npm=("googleapis", "@googleapis/drive", "@googleapis/gmail", "@googleapis/sheets"),
        hosts=("www.googleapis.com", "gmail.googleapis.com", "sheets.googleapis.com"),
        openapi_url="https://www.googleapis.com/discovery/v1/apis/drive/v3/rest",
        openapi_format="discovery",
        changelog_url="https://developers.google.com/gmail/api/release-notes",
        extra_openapi=(
            "https://www.googleapis.com/discovery/v1/apis/gmail/v1/rest",
            "https://www.googleapis.com/discovery/v1/apis/calendar/v3/rest",
            "https://www.googleapis.com/discovery/v1/apis/sheets/v4/rest",
            "https://www.googleapis.com/discovery/v1/apis/docs/v1/rest",
        ),
    ),
    _e(
        "google-cloud",
        "Google Cloud",
        python=("google.cloud",),
        pypi=(
            "google-cloud-storage",
            "google-cloud-bigquery",
            "google-cloud-pubsub",
            "google-cloud-firestore",
            "google-cloud-secret-manager",
            "google-cloud-aiplatform",
        ),
        npm=("@google-cloud/storage", "@google-cloud/bigquery", "@google-cloud/pubsub"),
        hosts=("storage.googleapis.com", "bigquery.googleapis.com", "pubsub.googleapis.com"),
        changelog_url="https://cloud.google.com/release-notes",
    ),
    _e(
        "asana",
        "Asana",
        python="asana",
        pypi="asana",
        npm="asana",
        hosts=("app.asana.com",),
        openapi_url="https://raw.githubusercontent.com/asana/openapi/master/defs/asana_oas.yaml",
        changelog_url="https://developers.asana.com/docs/changelog",
    ),
    _e(
        "trello",
        "Trello",
        python=("trello", "py_trello"),
        pypi=("py-trello",),
        npm=("trello", "node-trello"),
        hosts=("api.trello.com",),
        openapi_url="https://developer.atlassian.com/cloud/trello/swagger.v3.json",
        changelog_url="https://developer.atlassian.com/cloud/trello/changelog/",
    ),
    _e(
        "airtable",
        "Airtable",
        python=("pyairtable", "airtable"),
        pypi=("pyairtable", "airtable-python-wrapper"),
        npm="airtable",
        hosts=("api.airtable.com",),
        changelog_url="https://airtable.com/developers/web/api/changelog",
    ),
    _e(
        "zendesk",
        "Zendesk",
        python=("zenpy", "zendesk"),
        pypi=("zenpy",),
        npm=("node-zendesk",),
        hosts=("zendesk.com",),
        openapi_url="https://developer.zendesk.com/zendesk/oas.yaml",
        changelog_url="https://developer.zendesk.com/api-reference/introduction/changelog/",
    ),
    _e(
        "intercom",
        "Intercom",
        python=("intercom", "python_intercom"),
        pypi=("python-intercom", "intercom-python"),
        npm=("intercom-client",),
        hosts=("api.intercom.io",),
        openapi_url=(
            "https://raw.githubusercontent.com/intercom/Intercom-OpenAPI/main/descriptions/"
            "2.11/api.intercom.io.yaml"
        ),
        changelog_url="https://developers.intercom.com/docs/references/rest-api/api.intercom.io/changelog",
    ),
    _e(
        "dropbox",
        "Dropbox",
        python="dropbox",
        pypi="dropbox",
        npm="dropbox",
        hosts=("api.dropboxapi.com", "content.dropboxapi.com"),
        changelog_url="https://www.dropbox.com/developers/reference/changelog",
    ),
    _e(
        "box",
        "Box",
        python=("boxsdk", "box_sdk_gen"),
        pypi=("boxsdk", "box-sdk-gen"),
        npm=("box-node-sdk", "box-typescript-sdk-gen"),
        hosts=("api.box.com", "upload.box.com"),
        openapi_url="https://raw.githubusercontent.com/box/box-openapi/main/openapi.json",
        changelog_url="https://developer.box.com/changelog/",
    ),
    # ------------------------------------------------------------- comms
    _e(
        "twilio",
        "Twilio",
        python="twilio",
        pypi="twilio",
        npm="twilio",
        hosts=("api.twilio.com", "messaging.twilio.com", "lookups.twilio.com"),
        openapi_url=(
            "https://raw.githubusercontent.com/twilio/twilio-oai/main/spec/json/twilio_api_v2010.json"
        ),
        changelog_url="https://www.twilio.com/en-us/changelog",
        extra_openapi=(
            "https://raw.githubusercontent.com/twilio/twilio-oai/main/spec/json/twilio_messaging_v1.json",
        ),
    ),
    _e(
        "sendgrid",
        "SendGrid",
        python="sendgrid",
        pypi="sendgrid",
        npm="@sendgrid/mail",
        hosts=("api.sendgrid.com",),
        openapi_url="https://raw.githubusercontent.com/sendgrid/sendgrid-oai/main/oai.json",
        changelog_url="https://docs.sendgrid.com/release-notes",
    ),
    _e(
        "mailgun",
        "Mailgun",
        python="mailgun",
        pypi=("mailgun", "mailgun2"),
        npm=("mailgun.js", "mailgun-js"),
        hosts=("api.mailgun.net", "api.eu.mailgun.net"),
        changelog_url="https://documentation.mailgun.com/docs/mailgun/api-reference/changelog/",
    ),
    _e(
        "postmark",
        "Postmark",
        python="postmarker",
        pypi="postmarker",
        npm="postmark",
        hosts=("api.postmarkapp.com",),
        changelog_url="https://postmarkapp.com/developer/api/overview",
    ),
    _e(
        "resend",
        "Resend",
        python="resend",
        pypi="resend",
        npm="resend",
        hosts=("api.resend.com",),
        openapi_url="https://raw.githubusercontent.com/resend/resend-openapi/main/resend.yaml",
        changelog_url="https://resend.com/changelog",
    ),
    _e(
        "discord",
        "Discord",
        python=("discord", "discord_webhook"),
        pypi=("discord.py", "discord-webhook"),
        npm=("discord.js",),
        hosts=("discord.com", "discordapp.com"),
        openapi_url="https://raw.githubusercontent.com/discord/discord-api-spec/main/specs/openapi.json",
        changelog_url="https://discord.com/developers/docs/change-log",
    ),
    # ------------------------------------------------------------- analytics / product
    _e(
        "segment",
        "Segment",
        python=("segment", "analytics"),
        pypi=("segment-analytics-python", "analytics-python"),
        npm=("@segment/analytics-node", "analytics-node"),
        hosts=("api.segment.io",),
        changelog_url="https://segment.com/docs/connections/sources/catalog/libraries/server/python/changelog/",
    ),
    _e(
        "mixpanel",
        "Mixpanel",
        python="mixpanel",
        pypi="mixpanel",
        npm="mixpanel",
        hosts=("api.mixpanel.com",),
        changelog_url="https://developer.mixpanel.com/changelog",
    ),
    _e(
        "amplitude",
        "Amplitude",
        python=("amplitude", "amplitude_analytics"),
        pypi=("amplitude-analytics",),
        npm=("@amplitude/analytics-node", "@amplitude/node"),
        hosts=("api2.amplitude.com", "api.amplitude.com"),
        changelog_url="https://amplitude.com/docs/changelog",
    ),
    _e(
        "launchdarkly",
        "LaunchDarkly",
        python=("ldclient", "launchdarkly_api"),
        pypi=("launchdarkly-server-sdk", "launchdarkly-api"),
        npm=("@launchdarkly/node-server-sdk", "launchdarkly-node-server-sdk"),
        hosts=("app.launchdarkly.com", "sdk.launchdarkly.com"),
        openapi_url="https://app.launchdarkly.com/api/v2/openapi.json",
        changelog_url="https://launchdarkly.com/docs/api/changelog",
    ),
    # ------------------------------------------------------------- observability
    _e(
        "sentry",
        "Sentry",
        python="sentry_sdk",
        pypi="sentry-sdk",
        npm=("@sentry/node", "@sentry/browser", "@sentry/nextjs"),
        hosts=("sentry.io",),
        changelog_url="https://changelog.sentry.io/",
    ),
    _e(
        "datadog",
        "Datadog",
        python=("datadog", "datadog_api_client", "ddtrace"),
        pypi=("datadog", "datadog-api-client", "ddtrace"),
        npm=("@datadog/datadog-api-client", "dd-trace"),
        hosts=("api.datadoghq.com", "api.datadoghq.eu"),
        openapi_url=(
            "https://raw.githubusercontent.com/DataDog/datadog-api-client-python/master/"
            ".generator/schemas/v2/openapi.yaml"
        ),
        changelog_url="https://docs.datadoghq.com/api/latest/",
    ),
    _e(
        "pagerduty",
        "PagerDuty",
        python=("pdpyras", "pagerduty"),
        pypi=("pdpyras", "pagerduty"),
        npm=("@pagerduty/pdjs",),
        hosts=("api.pagerduty.com", "events.pagerduty.com"),
        openapi_url="https://raw.githubusercontent.com/PagerDuty/api-schema/main/reference/REST/openapiv3.json",
        changelog_url="https://developer.pagerduty.com/changelog/",
    ),
    # ------------------------------------------------------------- cloud / infra
    _e(
        "aws",
        "AWS",
        python=("boto3", "botocore", "aioboto3", "aiobotocore"),
        pypi=("boto3", "botocore", "aioboto3"),
        npm=("aws-sdk", "@aws-sdk/client-s3", "@aws-sdk/client-dynamodb", "@aws-sdk/client-sqs"),
        hosts=("amazonaws.com",),
        changelog_url="https://github.com/boto/boto3/blob/develop/CHANGELOG.rst",
    ),
    _e(
        "azure",
        "Azure",
        python=("azure",),
        pypi=("azure-identity", "azure-storage-blob", "azure-keyvault-secrets", "azure-ai-ml"),
        npm=("@azure/identity", "@azure/storage-blob", "@azure/openai"),
        hosts=("management.azure.com", "graph.microsoft.com"),
        changelog_url="https://azure.github.io/azure-sdk/releases/latest/python.html",
    ),
    _e(
        "supabase",
        "Supabase",
        python=("supabase", "postgrest", "gotrue"),
        pypi=("supabase", "postgrest"),
        npm=("@supabase/supabase-js",),
        hosts=("api.supabase.com", "supabase.co"),
        openapi_url="https://api.supabase.com/api/v1-json",
        changelog_url="https://supabase.com/changelog",
    ),
    _e(
        "firebase",
        "Firebase",
        python=("firebase_admin",),
        pypi=("firebase-admin",),
        npm=("firebase-admin", "firebase"),
        hosts=("firestore.googleapis.com", "firebase.googleapis.com"),
        changelog_url="https://firebase.google.com/support/release-notes/admin/python",
    ),
    # ------------------------------------------------------------- LLM providers
    _e(
        "openai",
        "OpenAI",
        kind="llm",
        python="openai",
        pypi="openai",
        npm="openai",
        hosts=("api.openai.com",),
        openapi_url="https://raw.githubusercontent.com/openai/openai-openapi/manual_spec/openapi.yaml",
        changelog_url="https://platform.openai.com/docs/changelog",
    ),
    _e(
        "anthropic",
        "Anthropic",
        kind="llm",
        python="anthropic",
        pypi="anthropic",
        npm="@anthropic-ai/sdk",
        hosts=("api.anthropic.com",),
        changelog_url="https://docs.claude.com/en/release-notes/api",
    ),
    _e(
        "cohere",
        "Cohere",
        kind="llm",
        python="cohere",
        pypi="cohere",
        npm="cohere-ai",
        hosts=("api.cohere.com", "api.cohere.ai"),
        changelog_url="https://docs.cohere.com/changelog",
    ),
    _e(
        "mistral",
        "Mistral AI",
        kind="llm",
        python="mistralai",
        pypi="mistralai",
        npm="@mistralai/mistralai",
        hosts=("api.mistral.ai",),
        openapi_url="https://raw.githubusercontent.com/mistralai/platform-docs-public/main/openapi.yaml",
        changelog_url="https://docs.mistral.ai/getting-started/changelog/",
    ),
    _e(
        "groq",
        "Groq",
        kind="llm",
        python="groq",
        pypi="groq",
        npm="groq-sdk",
        hosts=("api.groq.com",),
        changelog_url="https://console.groq.com/docs/changelog",
    ),
    _e(
        "together",
        "Together AI",
        kind="llm",
        python="together",
        pypi="together",
        npm="together-ai",
        hosts=("api.together.xyz",),
        changelog_url="https://docs.together.ai/changelog",
    ),
    _e(
        "replicate",
        "Replicate",
        kind="llm",
        python="replicate",
        pypi="replicate",
        npm="replicate",
        hosts=("api.replicate.com",),
        openapi_url="https://api.replicate.com/openapi.json",
        changelog_url="https://replicate.com/changelog",
    ),
    _e(
        "google-genai",
        "Google Gemini",
        kind="llm",
        python=("google.generativeai", "google.genai", "google.ai"),
        pypi=("google-generativeai", "google-genai"),
        npm=("@google/generative-ai", "@google/genai"),
        hosts=("generativelanguage.googleapis.com",),
        changelog_url="https://ai.google.dev/gemini-api/docs/changelog",
    ),
    # ------------------------------------------------------------- vector / search
    _e(
        "pinecone",
        "Pinecone",
        python="pinecone",
        pypi=("pinecone", "pinecone-client"),
        npm=("@pinecone-database/pinecone",),
        hosts=("api.pinecone.io",),
        changelog_url="https://docs.pinecone.io/release-notes/",
    ),
    _e(
        "weaviate",
        "Weaviate",
        python="weaviate",
        pypi="weaviate-client",
        npm=("weaviate-client", "weaviate-ts-client"),
        hosts=("weaviate.cloud",),
        changelog_url="https://weaviate.io/developers/weaviate/release-notes",
    ),
    _e(
        "qdrant",
        "Qdrant",
        python="qdrant_client",
        pypi="qdrant-client",
        npm="@qdrant/js-client-rest",
        hosts=("cloud.qdrant.io",),
        openapi_url="https://raw.githubusercontent.com/qdrant/qdrant/master/docs/redoc/master/openapi.json",
        changelog_url="https://github.com/qdrant/qdrant/releases",
    ),
    _e(
        "chroma",
        "Chroma",
        python="chromadb",
        pypi="chromadb",
        npm="chromadb",
        hosts=("api.trychroma.com",),
        changelog_url="https://docs.trychroma.com/updates/migration",
    ),
    _e(
        "elastic",
        "Elasticsearch",
        python="elasticsearch",
        pypi="elasticsearch",
        npm="@elastic/elasticsearch",
        hosts=("elastic-cloud.com", "elastic.co"),
        openapi_url=(
            "https://raw.githubusercontent.com/elastic/elasticsearch-specification/main/"
            "output/openapi/elasticsearch-openapi.json"
        ),
        changelog_url="https://www.elastic.co/guide/en/elasticsearch/reference/current/es-release-notes.html",
    ),
    _e(
        "algolia",
        "Algolia",
        python="algoliasearch",
        pypi="algoliasearch",
        npm="algoliasearch",
        hosts=("algolia.net", "algolianet.com"),
        changelog_url="https://www.algolia.com/doc/libraries/python/changelog/",
    ),
    # ------------------------------------------------------------- agent tooling
    _e(
        "tavily",
        "Tavily",
        python="tavily",
        pypi="tavily-python",
        npm="@tavily/core",
        hosts=("api.tavily.com",),
        changelog_url="https://docs.tavily.com/documentation/api-reference/endpoint/search",
    ),
    _e(
        "exa",
        "Exa",
        python="exa_py",
        pypi="exa_py",
        npm="exa-js",
        hosts=("api.exa.ai",),
        changelog_url="https://docs.exa.ai/changelog/changelog",
    ),
    _e(
        "serpapi",
        "SerpApi",
        python=("serpapi", "google_search_results"),
        pypi=("serpapi", "google-search-results"),
        npm="serpapi",
        hosts=("serpapi.com",),
        changelog_url="https://serpapi.com/change-log",
    ),
    _e(
        "browserbase",
        "Browserbase",
        python="browserbase",
        pypi="browserbase",
        npm=("@browserbasehq/sdk", "@browserbasehq/stagehand"),
        hosts=("api.browserbase.com",),
        changelog_url="https://docs.browserbase.com/changelog",
    ),
    _e(
        "e2b",
        "E2B",
        python=("e2b", "e2b_code_interpreter"),
        pypi=("e2b", "e2b-code-interpreter"),
        npm=("e2b", "@e2b/code-interpreter"),
        hosts=("api.e2b.dev",),
        changelog_url="https://e2b.dev/docs/changelog",
    ),
    _e(
        "firecrawl",
        "Firecrawl",
        python="firecrawl",
        pypi=("firecrawl-py", "firecrawl"),
        npm=("@mendable/firecrawl-js", "firecrawl"),
        hosts=("api.firecrawl.dev",),
        openapi_url="https://raw.githubusercontent.com/mendableai/firecrawl/main/apps/api/v1-openapi.json",
        changelog_url="https://docs.firecrawl.dev/changelog",
    ),
    # ------------------------------------------------------------- MCP + agent frameworks
    _e(
        "modelcontextprotocol",
        "MCP SDK",
        kind="mcp-framework",
        python=("mcp", "fastmcp"),
        pypi=("mcp", "fastmcp"),
        npm=("@modelcontextprotocol/sdk",),
        changelog_url="https://modelcontextprotocol.io/specification/2025-11-25/changelog",
    ),
    _e(
        "langchain",
        "LangChain MCP adapters",
        kind="mcp-framework",
        python=("langchain_mcp_adapters",),
        pypi=("langchain-mcp-adapters",),
        npm=("@langchain/mcp-adapters",),
        changelog_url="https://github.com/langchain-ai/langchain-mcp-adapters/releases",
    ),
    _e(
        "google",
        "Google ADK",
        kind="mcp-framework",
        python=("google.adk",),
        pypi=("google-adk",),
        npm=("@google/adk",),
        changelog_url="https://github.com/google/adk-python/blob/main/CHANGELOG.md",
    ),
    _e(
        "openai",
        "OpenAI Agents SDK",
        kind="mcp-framework",
        python=("agents",),
        pypi=("openai-agents",),
        npm=("@openai/agents",),
        changelog_url="https://github.com/openai/openai-agents-python/releases",
    ),
    _e(
        "crewai",
        "CrewAI",
        kind="agent-framework",
        python=("crewai", "crewai_tools"),
        pypi=("crewai", "crewai-tools"),
        npm=(),
        changelog_url="https://github.com/crewAIInc/crewAI/releases",
    ),
    _e(
        "langchain",
        "LangChain",
        kind="agent-framework",
        python=(
            "langchain",
            "langchain_core",
            "langchain_community",
            "langchain_openai",
            "langchain_anthropic",
        ),
        pypi=(
            "langchain",
            "langchain-core",
            "langchain-community",
            "langchain-openai",
            "langchain-anthropic",
        ),
        npm=("langchain", "@langchain/core", "@langchain/openai", "@langchain/anthropic"),
        changelog_url="https://github.com/langchain-ai/langchain/releases",
    ),
    _e(
        "langchain",
        "LangGraph",
        kind="agent-framework",
        python=("langgraph",),
        pypi=("langgraph",),
        npm=("@langchain/langgraph",),
        changelog_url="https://github.com/langchain-ai/langgraph/releases",
    ),
    _e(
        "llamaindex",
        "LlamaIndex",
        kind="agent-framework",
        python=("llama_index",),
        pypi=("llama-index", "llama-index-core"),
        npm=("llamaindex",),
        changelog_url="https://github.com/run-llama/llama_index/blob/main/CHANGELOG.md",
    ),
    # ------------------------------------------------------------- HTTP clients (mechanism)
    _e(
        "requests",
        "requests",
        kind="http-client",
        python="requests",
        pypi="requests",
    ),
    _e(
        "httpx",
        "httpx",
        kind="http-client",
        python="httpx",
        pypi="httpx",
    ),
    _e(
        "aiohttp",
        "aiohttp",
        kind="http-client",
        python="aiohttp",
        pypi="aiohttp",
    ),
    _e(
        "urllib3",
        "urllib3",
        kind="http-client",
        python="urllib3",
        pypi="urllib3",
    ),
    _e("axios", "axios", kind="http-client", npm="axios"),
    _e("got", "got", kind="http-client", npm="got"),
    _e("node-fetch", "node-fetch", kind="http-client", npm=("node-fetch", "cross-fetch")),
    _e("undici", "undici", kind="http-client", npm="undici"),
    _e("ky", "ky", kind="http-client", npm="ky"),
)


# --------------------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------------------


def _normalize_npm(name: str) -> str:
    return name.strip().lower()


@lru_cache(maxsize=1)
def _python_index() -> dict[str, SdkEntry]:
    out: dict[str, SdkEntry] = {}
    for e in SDK_ENTRIES:
        for p in e.python:
            out.setdefault(p, e)
    return out


@lru_cache(maxsize=1)
def _pypi_index() -> dict[str, SdkEntry]:
    out: dict[str, SdkEntry] = {}
    for e in SDK_ENTRIES:
        for p in e.pypi:
            out.setdefault(canonicalize_name(p), e)
    return out


@lru_cache(maxsize=1)
def _npm_index() -> dict[str, SdkEntry]:
    out: dict[str, SdkEntry] = {}
    for e in SDK_ENTRIES:
        for p in e.npm:
            out.setdefault(_normalize_npm(p), e)
    return out


@lru_cache(maxsize=1)
def _host_index() -> dict[str, SdkEntry]:
    out: dict[str, SdkEntry] = {}
    for e in SDK_ENTRIES:
        for h in e.hosts:
            out.setdefault(h.lower(), e)
    return out


def lookup_python(import_path: str) -> SdkEntry | None:
    """``"stripe"`` / ``"stripe.checkout"`` / ``"google.adk.tools"`` -> entry (longest prefix)."""
    idx = _python_index()
    parts = import_path.split(".")
    for n in range(len(parts), 0, -1):
        hit = idx.get(".".join(parts[:n]))
        if hit is not None:
            return hit
    return None


def python_import_root(import_path: str) -> str:
    """The prefix of the dotted path that identifies the package (``google.adk.tools`` ->
    ``google.adk``; unknown packages -> first segment)."""
    idx = _python_index()
    parts = import_path.split(".")
    for n in range(len(parts), 0, -1):
        cand = ".".join(parts[:n])
        if cand in idx:
            return cand
    return parts[0]


def lookup_pypi(distribution: str) -> SdkEntry | None:
    return _pypi_index().get(canonicalize_name(distribution))


def pypi_name_for_import(import_root: str) -> str | None:
    """Preferred PyPI distribution name for a known import root."""
    e = lookup_python(import_root)
    if e is None:
        return None
    root = python_import_root(import_root)
    # prefer the distribution whose canonical form matches the import root, else the first
    for dist in e.pypi:
        if canonicalize_name(dist) == canonicalize_name(root.replace(".", "-")):
            return dist
    if root == "google.adk":
        return "google-adk"
    return e.pypi[0] if e.pypi else None


def lookup_npm(package: str) -> SdkEntry | None:
    """``"stripe"`` or ``"@modelcontextprotocol/sdk/client/index.js"`` -> entry."""
    return _npm_index().get(_normalize_npm(npm_package_root(package)))


def npm_package_root(specifier: str) -> str:
    """Strip subpaths: ``@scope/pkg/sub`` -> ``@scope/pkg``, ``pkg/sub`` -> ``pkg``."""
    s = specifier.strip()
    if s.startswith("@"):
        parts = s.split("/")
        return "/".join(parts[:2])
    return s.split("/")[0]


def vendor_for_host(host: str) -> SdkEntry | None:
    """Exact host first, then a registered host that is a parent domain of ``host``
    (``api.eu.mailgun.net`` -> mailgun via ``mailgun.net``)."""
    h = host.lower().strip(".")
    idx = _host_index()
    if h in idx:
        return idx[h]
    best: tuple[int, SdkEntry] | None = None
    for reg, entry in idx.items():
        if h.endswith("." + reg) and (best is None or len(reg) > best[0]):
            best = (len(reg), entry)
    if best is not None:
        return best[1]
    # second-level fallback: "api.stripe.com" registered, "dashboard.stripe.com" seen
    for reg, entry in idx.items():
        labels = reg.split(".")
        if len(labels) >= 2:
            domain = ".".join(labels[-2:])
            if h == domain or h.endswith("." + domain):
                if best is None or len(domain) > best[0]:
                    best = (len(domain), entry)
    return best[1] if best else None


def spec_for_host(host: str) -> SdkEntry | None:
    """Only an exact (registered) host match is trusted to pick an OpenAPI spec."""
    e = _host_index().get(host.lower().strip("."))
    return e if e is not None and e.openapi_url else None


def entries_by_kind(kind: SdkKind) -> list[SdkEntry]:
    return [e for e in SDK_ENTRIES if e.kind == kind]


def all_python_roots() -> set[str]:
    return set(_python_index())


def known_http_client_roots() -> set[str]:
    return {p for e in SDK_ENTRIES if e.kind == "http-client" for p in e.python}
