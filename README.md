# Kintsua

[![PyPI](https://img.shields.io/pypi/v/kintsua)](https://pypi.org/project/kintsua/) [![CI](https://github.com/hr7657316/Kintusa-Ai/actions/workflows/ci.yml/badge.svg)](https://github.com/hr7657316/Kintusa-Ai/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE) [![HOL Guard](https://img.shields.io/badge/HOL%20Guard-command.kintsua-black)](https://hol.org/guard/extensions/command.kintsua)

**Self-maintaining integrations for AI agents.** Every agent depends on tools it does not own: MCP servers, OpenAPI specs, vendor SDKs. When one of them changes shape, the agent fails mid-task and nothing in your test suite notices, because your tests cover your code, not theirs.

Kintsua watches those upstreams. This repository is the open-source part:

- **`kintsua watch`**, a CLI that snapshots an MCP server's `tools/list` or an OpenAPI spec into a lockfile you commit, diffs it on every run, classifies the change (breaking, degraded, additive) with reproducible rules, and exits non-zero so CI can block the deploy. Also packaged as a GitHub Action.
- **`import kintsua`**, a runtime SDK under 500 lines that wraps your MCP session or HTTP client and reports schema drift and failed tool calls from production agents. No payloads leave your process.

Both are Apache 2.0 and need Python 3.12+.

```bash
pip install kintsua
```

The hosted service, **Kintsua Cloud** (a GitHub App that maps your repo's dependencies, opens an issue within an hour of an upstream break and a tested fix PR for the common cases), is a separate closed-source codebase that builds on this package. The CLI's `--push` flag and the SDK talk to it when you have an account.

---

## Quick start: the CLI

`kintsua watch` is the MCP/OpenAPI watcher and the rules classifier packaged as a CLI (Apache 2.0). It snapshots a server's `tools/list` into a lockfile you commit, so a schema change shows up in a PR diff, and it exits non-zero on breaking changes so it can gate a deploy.

```bash
pip install kintsua

# snapshot: writes .kintsua/<server>.lock.json (tools, inputSchemas, fingerprints)
kintsua watch snapshot --server https://mcp.vendor.com/mcp
kintsua watch snapshot --server "npx -y @vendor/mcp-server"        # stdio servers work too
kintsua watch snapshot --spec https://api.vendor.com/openapi.json  # OpenAPI 3.x / Swagger 2 / Google discovery

# diff: exit 1 on breaking changes, 0 otherwise; appends to .kintsua/history.jsonl
kintsua watch diff --server https://mcp.vendor.com/mcp
kintsua watch diff --server ... --fail-on degraded --github-action   # CI annotations + step summary
kintsua watch diff --server ... --json                               # the ChangeEvent + classification

# report: Markdown drift report from history.jsonl
kintsua watch report --since 7d
```

Useful flags: `--header K=V` (values may use `${VAR}`), `--auth-env NAME` (bearer token from an env var), `--env K=V` (for stdio servers), `--name` (lockfile base name), `--update` (rewrite the lockfile after diffing), `--dir` (default `.kintsua`).

`--push --repo owner/name --key $KINTSUA_KEY` sends the snapshot to a Kintsua Cloud account (see below), so the repo gets issues and fix PRs without CI running.

As a GitHub Action:

```yaml
- uses: hr7657316/Kintusa-Ai@main
  with:
    server: https://mcp.vendor.com/mcp
    fail-on: breaking
```

Exit codes: `0` no gate hit, `1` severity at or above `--fail-on`, `2` usage error or an unreadable lockfile (merge-conflict markers, or a lockfile another server wrote under the same `--name`), `3` could not connect (auth required, bad URL, server down), `4` no lockfile yet (run `snapshot`; the GitHub Action does this itself with `snapshot-if-missing: true`). `--auth-env` applies to `--spec` too, sent as `Authorization: Bearer`.

Exit code semantics are the contract: `0` nothing at or above the gate, `1` a change at or above `--fail-on`, `2` usage error or an unreadable lockfile, `3` the upstream could not be fetched, `4` no lockfile yet.

## Runtime SDK

The SDK sees what tools an agent actually calls and whether they still answer the way they did yesterday. Python only in v0, under 500 lines, one wrapper call, no payloads.

```python
import kintsua

# MCP: wrap any ClientSession from the official mcp package (1.x or 2.x)
session = kintsua.wrap_mcp(session, repo="acme/support-agent", key=KINTSUA_KEY)

# HTTP: wrap an httpx Client/AsyncClient or a requests Session
client = kintsua.wrap_http(httpx.AsyncClient(), repo="acme/support-agent", key=KINTSUA_KEY)

# Frameworks: one-line hooks (no-ops when the framework is not installed)
kintsua.hook_langchain(); kintsua.hook_openai_agents(); kintsua.hook_google_adk()
```

What it records: per MCP tool a `schema_fingerprint` (SHA-256 of the canonical `inputSchema`) compared with the last one stored locally, so a changed schema becomes a `schema_mismatch` event with the diff; per `tools/call` the tool name, latency, outcome and error class; per HTTP call host, method, normalised path, status, latency and, on 4xx/5xx, the first 512 bytes of the error body with secrets redacted. Arguments and results are never sent unless you pass `sample=True`, and then keys only.

Transport: batched every 5 seconds or 100 events to `POST /v1/ingest`, one retry then drop, never blocks or raises (every public function is wrapped; failures log once at debug level). Configure with `kintsua.configure(key=..., repo=..., url=...)` or `KINTSUA_KEY` / `KINTSUA_REPO` / `KINTSUA_URL`.

## Trust and data boundaries

- The CLI runs the MCP command or fetches the URL you pass it and nothing else. It writes only to `--dir` (default `.kintsua/`).
- Every URL is network-guarded: localhost, private, link-local and cloud-metadata addresses are refused, each redirect hop is re-checked, and response bodies are size-capped.
- The SDK sends tool names, latencies, outcomes and schema fingerprints, never arguments or results (keys only with `sample=True`). Secrets in error bodies, URLs and paths are redacted client-side. Without a key every SDK call is a no-op, and nothing ever raises into the agent.
- The command-safety extension for this CLI is listed in [HOL Guard](https://hol.org/guard/extensions/command.kintsua): `snapshot`, `diff --update` and `--push` are the only state-changing operations and are flagged for review there.

## Layout

```
kintsua/
  cli/        kintsua watch: snapshot, diff, report
  watchers/   base, mcp (+ 1.x/2.x compat), openapi, schemadiff, bounded http
  classify/   the rules classifier, severity schema, rename detection
  core/       canonical change events, network guard, small utilities
  report/     Markdown drift report
  sdk/        wrap_mcp, wrap_http, framework hooks, batched reporter
tests/        unit tests, a golden set of labelled change events, a fixture MCP server
action.yml    the composite GitHub Action
```

## Development

```bash
uv sync --all-extras
make test            # unit tests, no network
make test-network    # live checks against real vendor specs and public MCP servers
make lint
```

Releases: bump `version` in `pyproject.toml`, tag `vX.Y.Z`, push the tag. GitHub Actions publishes to PyPI through trusted publishing.

Licence: Apache 2.0.
