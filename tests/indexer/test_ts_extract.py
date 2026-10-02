"""TypeScript / JavaScript extractor edge cases."""

from __future__ import annotations

import json
from pathlib import Path

from kintsua.indexer.ts_extract import declared_npm_packages, extract_ts, extract_ts_source


def run(src: str, path: str = "src/index.ts"):
    r = extract_ts_source(src, path)
    return r, {c.operation: c for c in r.call_sites}


def test_default_named_namespace_and_require_imports():
    src = """
import Stripe from "stripe";
import { WebClient as WC } from "@slack/web-api";
import * as Sentry from "@sentry/node";
const { Octokit } = require("@octokit/rest");
const openai = require("openai");
const stripe = new Stripe("k");
const slack = new WC("t");
const gh = new Octokit({ auth: "x" });
await stripe.charges.create({ amount: 1, currency: "usd" });
await slack.chat.postMessage({ channel: "c", text: "t" });
Sentry.captureException(err);
await gh.rest.issues.create({ owner, repo, title });
const oa = new openai.OpenAI();
await oa.responses.create({ model: "m" });
"""
    r, ops = run(src)
    assert set(ops) == {
        "stripe.charges.create",
        "@slack/web-api.WebClient.chat.postMessage",
        "@sentry/node.captureException",
        "@octokit/rest.Octokit.rest.issues.create",
        "openai.OpenAI.responses.create",
    }
    assert ops["stripe.charges.create"].args_shape == {"keys": ["amount", "currency"]}
    assert ops["@octokit/rest.Octokit.rest.issues.create"].args_shape == {
        "keys": ["owner", "repo", "title"]
    }
    assert {d.name for d in r.dependencies} == {
        "stripe",
        "@slack/web-api",
        "@sentry/node",
        "@octokit/rest",
        "openai",
    }
    assert r.dependency("npm", "@octokit/rest").vendor == "github"


def test_this_instances_class_fields_optional_chaining_and_then():
    src = """
import Stripe from "stripe";
import { WebClient } from "@slack/web-api";
export class Svc {
  private slack = new WebClient(token);
  constructor() { this.stripe = new Stripe(key); }
  async run(id: string) {
    this.slack?.chat.postMessage({ channel, text });
    this.stripe.customers.retrieve(id).then((c) => c);
    const list = await this.stripe.charges.list({ limit: 3 });
  }
}
"""
    r, ops = run(src)
    assert set(ops) == {
        "@slack/web-api.WebClient.chat.postMessage",
        "stripe.customers.retrieve",
        "stripe.charges.list",
    }
    assert ops["@slack/web-api.WebClient.chat.postMessage"].args_shape == {
        "keys": ["channel", "text"]
    }
    assert len(r.call_sites) == 3


def test_fetch_axios_got_ky_with_templates_constants_and_base_urls():
    src = """
import axios from "axios";
import got from "got";
import ky from "ky";
const BASE = process.env.NOTION ?? "https://api.notion.com/v1";
const api = axios.create({ baseURL: "https://api.linear.app" });
const gh = got.extend({ prefixUrl: "https://api.github.com/" });
const k = ky.create({ prefixUrl: "https://api.exa.ai" });
async function go(id: string, owner: string) {
  await fetch(`https://api.github.com/repos/${owner}/acme/issues/${id}`, { method: "PATCH", body: "{}" });
  await fetch("https://api.anthropic.com/v1/messages");
  axios.get(BASE + "/pages/" + id);
  await api.post("/graphql", { query: "{ x }" });
  await axios({ url: "/users/42", baseURL: "https://api.example.com", method: "delete" });
  await gh.get("repos/acme/agent/issues/42").json();
  got(`${BASE}/search`, { method: "POST", json: { query: "q" } });
  await k.post("search", { json: { query: "q" } });
  await fetch(dynamicUrl);
}
"""
    r, ops = run(src)
    got_ops = {(c.dependency_name, c.operation, c.symbol) for c in r.call_sites}
    assert got_ops == {
        ("api.github.com", "PATCH /repos/{id}/acme/issues/{id}", "fetch"),
        ("api.anthropic.com", "GET /v1/messages", "fetch"),
        ("api.notion.com", "GET /v1/pages/{id}", "axios.get"),
        ("api.linear.app", "POST /graphql", "axios.create.post"),
        ("api.example.com", "DELETE /users/{id}", "axios"),
        ("api.github.com", "GET /repos/acme/agent/issues/{id}", "got.extend.get"),
        ("api.notion.com", "POST /v1/search", "got"),
        ("api.exa.ai", "POST /search", "ky.create.post"),
    }
    assert ops["PATCH /repos/{id}/acme/issues/{id}"].args_shape == {"keys": ["method", "body"]}
    assert ops["POST /graphql"].args_shape == {"keys": ["query"]}
    assert ops["POST /v1/search"].args_shape["body_keys"] == ["query"]
    # http client packages never become dependencies
    assert {d.kind for d in r.dependencies} == {"http"}


def test_mcp_calltool_is_left_to_the_mcp_extractor():
    src = """
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
const client = new Client({ name: "a", version: "1" });
await client.connect(transport);
await client.callTool({ name: "x", arguments: {} });
"""
    r, ops = run(src)
    assert set(ops) == {"@modelcontextprotocol/sdk.Client.connect"}


def test_jsx_tsx_and_js_files_parse():
    r = extract_ts_source(
        'import Stripe from "stripe";\nconst s = new Stripe("k");\nexport const C = () => <div onClick={() => s.charges.list({ limit: 1 })} />;\n',
        "src/App.tsx",
    )
    assert [c.operation for c in r.call_sites] == ["stripe.charges.list"]
    r2 = extract_ts_source(
        'const Stripe = require("stripe");\nconst s = new Stripe("k");\ns.customers.create({ email: "e" });\n',
        "lib/a.cjs",
    )
    assert [c.operation for c in r2.call_sites] == ["stripe.customers.create"]


def test_test_files_are_flagged_and_syntax_errors_counted():
    r = extract_ts_source(
        'import Stripe from "stripe";\nconst s = new Stripe("k");\ns.charges.list({});\nconst = ;\n',
        "src/a.test.ts",
    )
    assert r.call_sites[0].in_tests is True
    assert r.stats.parse_errors == 1


def test_package_json_dependencies_and_dev_flag(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {
                    "stripe": "^17",
                    "axios": "^1",
                    "zod": "^3",
                    "@modelcontextprotocol/sdk": "1.0.0",
                },
                "devDependencies": {
                    "typescript": "^5",
                    "@types/node": "*",
                    "vitest": "*",
                    "@slack/web-api": "^7",
                },
            }
        )
    )
    (tmp_path / "packages" / "api").mkdir(parents=True)
    (tmp_path / "packages" / "api" / "package.json").write_text(
        json.dumps({"dependencies": {"openai": "^4"}})
    )
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "package.json").write_text(
        json.dumps({"dependencies": {"twilio": "^5"}})
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text(
        'import Stripe from "stripe";\nnew Stripe("k").charges.list({});\n'
    )
    declared = {d.name: d for d in declared_npm_packages(tmp_path)}
    assert (
        "twilio" not in declared and declared["openai"].config_file == "packages/api/package.json"
    )
    r = extract_ts(tmp_path)
    names = {d.name: d for d in r.dependencies}
    assert set(names) == {"stripe", "zod", "@modelcontextprotocol/sdk", "@slack/web-api", "openai"}
    assert names["@slack/web-api"].meta["dev"] is True
    assert names["zod"].vendor is None
    assert names["stripe"].version_constraint == "^17"
    assert [c.operation for c in r.call_sites] == ["stripe.charges.list"]


def test_dynamic_fetch_method_is_recorded_as_the_bare_path_and_matches_any_method():
    from kintsua.matcher.operations import matches_any

    r, ops = run(
        """
export async function a(id: string, method: string) {
  return fetch(`https://api.stripe.com/v1/charges/${id}`, { method });
}
export async function b(m: string) {
  return fetch("https://api.stripe.com/v1/charges", { method: m, headers: {} });
}
export async function c() {
  return fetch("https://api.stripe.com/v1/refunds", { headers: {} });
}
export async function d() {
  return fetch("https://api.stripe.com/v1/customers", { method: "post" });
}
"""
    )
    assert set(ops) == {"/v1/charges/{id}", "/v1/charges", "GET /v1/refunds", "POST /v1/customers"}
    assert matches_any("/v1/charges/{id}", ["POST /v1/charges/{id}"])
    assert matches_any("/v1/charges", ["POST /v1/charges"])
    assert not matches_any("GET /v1/refunds", ["POST /v1/refunds"])
