"""Operation normalisation and matching across the three dialects (pure, no database)."""

from __future__ import annotations

import pytest

from kintsua.matcher.operations import (
    SdkRule,
    normalize_operation,
    operations_match,
    parse_operation,
    pointer_operation,
    register_sdk_rule,
    sdk_http_operations,
    sdk_vendor,
)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("POST /v1/charges", "POST /v1/charges"),
        ("post /v1/charges/", "POST /v1/charges"),
        ("GET https://api.stripe.com/v1/charges/ch_1Nxyz123456789?x=1", "GET /v1/charges/{id}"),
        ("GET api.stripe.com/v1/charges/123", "GET /v1/charges/{id}"),
        (
            "GET localhost:8000/api/items/550e8400-e29b-41d4-a716-446655440000",
            "GET /api/items/{id}",
        ),
        ("GET /users/<int:id>/", "GET /users/{id}"),
        ("POST /repos/:owner/:repo/issues", "POST /repos/{owner}/{repo}/issues"),
        ("GET /v1/customers/${customerId}", "GET /v1/customers/{customerid}"),
        ("/v1/charges", "/v1/charges"),
        ("POST /chat.postMessage", "POST /chat.postmessage"),
        ("tool:get_weather", "tool:get_weather"),
        ("get_weather", "tool:get_weather"),
        ("stripe.Charge.create", "sdk:stripe.Charge.create"),
        ("sdk:stripe.Charge.create", "sdk:stripe.Charge.create"),
        ("package:PyGithub", "package:pygithub"),
        ("package:@slack/web-api", "package:@slack/web-api"),
        ("resource:file:///tmp/x", "resource:file:///tmp/x"),
        ("*", "*"),
        ("", ""),
    ],
)
def test_normalize_operation(raw, canonical):
    assert normalize_operation(raw) == canonical


def test_parse_kinds():
    assert parse_operation("POST /v1/charges").kind == "http"
    assert parse_operation("/v1/charges").method == "*"
    assert parse_operation("get_weather").kind == "tool"
    assert parse_operation("stripe.Charge.create").kind == "sdk"
    assert parse_operation("package:stripe").kind == "package"
    assert parse_operation("*").kind == "any"
    assert parse_operation("what is this?!").kind == "other"


@pytest.mark.parametrize(
    ("site", "affected", "expected"),
    [
        # http templates, literals, trailing slashes, methods
        ("POST /v1/charges/{id}", "POST /v1/charges/{charge}", True),
        ("POST /v1/charges/ch_1Nxyz123456789", "POST /v1/charges/{charge}", True),
        ("POST /v1/charges/", "POST /v1/charges", True),
        ("POST /v1/charges", "GET /v1/charges", False),
        ("POST /v1/charges", "/v1/charges", True),  # whole path item
        ("GET /v1/charges/{id}", "POST /v1/charges", False),  # different depth
        ("GET https://api.stripe.com/v1/charges", "GET /v1/charges", True),
        # tools
        ("get_weather", "tool:get_weather", True),
        ("tool:get_weather", "get_weather", True),
        ("get_weather", "tool:get_forecast", False),
        ("get_weather", "*", True),
        ("get_weather", "package:weather-mcp", True),
        # sdk <-> sdk
        ("WebClient.chat_postMessage", "slack_sdk.WebClient.chat_postMessage", True),
        ("stripe.Charge.create", "stripe.Charge.retrieve", False),
        # sdk <-> http through the table
        ("stripe.Charge.create", "POST /v1/charges", True),
        ("stripe.Charge.create", "GET /v1/charges", False),
        ("stripe.Customer.retrieve", "GET /v1/customers/{customer}", True),
        ("stripe.checkout.Session.create", "POST /v1/checkout/sessions", True),
        ("stripe.Stripe.paymentIntents.create", "POST /v1/payment_intents", True),
        ("stripe.StripeClient.v1.payment_intents.create", "POST /v1/payment_intents", True),
        ("stripe.PaymentIntent.confirm", "POST /v1/payment_intents/{intent}/confirm", True),
        ("slack_sdk.WebClient.chat_postMessage", "POST /chat.postMessage", True),
        ("slack_sdk.WebClient.conversations_list", "GET /conversations.list", True),
        ("@slack/web-api.WebClient.chat.postMessage", "POST /chat.postMessage", True),
        ("github.Github.get_repo", "GET /repos/{owner}/{repo}", True),
        ("github.Github.get_repo.create_issue", "POST /repos/{owner}/{repo}/issues", True),
        ("@octokit/rest.Octokit.rest.issues.create", "POST /repos/{owner}/{repo}/issues", True),
        ("openai.OpenAI.chat.completions.create", "POST /chat/completions", True),
        ("openai.OpenAI.completions.create", "POST /chat/completions", False),
        ("anthropic.Anthropic.messages.create", "POST /v1/messages", True),
        ("notion_client.Client.databases.query", "POST /v1/databases/{database_id}/query", True),
        ("hubspot.HubSpot.crm.contacts.basic_api.create", "POST /crm/v3/objects/contacts", True),
        (
            "twilio.rest.Client.messages.create",
            "POST /2010-04-01/Accounts/{AccountSid}/Messages.json",
            True,
        ),
        # http site vs sdk affected (the model sometimes names symbols)
        ("POST /v1/charges", "stripe.Charge.create", True),
        # unknown symbol never matches an http path
        ("acme.Client.frobnicate", "POST /v1/frob", False),
        ("", "POST /v1/charges", False),
    ],
)
def test_operations_match(site, affected, expected):
    assert operations_match(site, affected) is expected


def test_sdk_table_is_data_driven_and_extensible():
    assert sdk_vendor("stripe.Charge.create") == "stripe"
    assert sdk_vendor("acme.Client.frobnicate") is None
    assert sdk_http_operations("acme.Client.frobnicate") == ()
    register_sdk_rule(SdkRule("acme", r"^acme\.Client\.frobnicate$", (("POST", "/v1/frob"),)))
    assert [o.canonical() for o in sdk_http_operations("acme.Client.frobnicate")] == [
        "POST /v1/frob"
    ]
    assert operations_match("acme.Client.frobnicate", "POST /v1/frob")
    assert sdk_vendor("acme.Client.frobnicate") == "acme"


def test_pointer_operation():
    assert pointer_operation("/paths/~1v1~1charges/post/parameters/3") == "POST /v1/charges"
    assert pointer_operation("/paths/~1v1~1charges") == "/v1/charges"
    assert pointer_operation("/tools/get_weather/inputSchema/properties/x") == "tool:get_weather"
    assert pointer_operation("/resources/file:~1~1~1x") == "resource:file:///x"
    assert pointer_operation("/prompts/summarise") == "prompt:summarise"
    assert pointer_operation("/versions/12.0.0") is None
    assert pointer_operation("") is None
