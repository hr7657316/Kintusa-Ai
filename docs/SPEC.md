# Kintsua Build Spec v0: Self-Maintaining Integrations for AI Agents

(Transcribed from the v0 build spec PDF, 1 October 2026. This is the source of truth for scope.)

## Scope

Kintsua v0 is a GitHub App plus a hosted watcher that finds every external API and MCP tool an agent repo depends on, detects breaking changes upstream, and opens a tested fix PR. It must be live with design partners and producing weekly numbers by 7 November 2026.

**Goals for v0.** Install in under five minutes from GitHub. Dependency map for a Python or TypeScript repo within one hour of install. Daily watch of every mapped dependency. A GitHub issue within one hour of a breaking change landing upstream. A fix PR with a passing test run for the common 80% of breaks (renamed field, new required parameter with an inferable default, SDK major bump with migration notes). A Python SDK that reports failed tool calls and schema mismatches at runtime. An open-source CLI (`kintsua watch`) that snapshots and diffs MCP servers in CI.

**Non-goals for v0.** No dashboard beyond settings and a changes list. No languages beyond Python and TypeScript. No vendor-side product. No Slack or email alerts until three customers ask. No self-hosting. No rollback or undo of agent actions (that is v1).

**Success metrics by 7 November.** Ten repos installed, five with weekly activity. Twenty breaking changes detected across watched tools. At least three PRs opened by Kintsua and merged by a customer. Two paying teams. A public weekly drift report with 200 subscribers.

**Validation gate before building past week 1.** Five or more replies from the 30-lead outreach asking for alerts on their own integrations. Fewer than five means the wedge pivots to runtime failed-call capture (section 9) as the first product and the watcher becomes secondary.

## Architecture

One Python monorepo, one Postgres, one FastAPI service, one worker pool. Five components talk through the database and a job queue, never directly to each other, so each can be built and tested alone.

The **watcher** polls sources (OpenAPI specs, MCP servers, package registries, changelog pages) on a schedule, stores snapshots, and emits a `change_event` per diff. The **classifier** consumes change events, applies rules then a model, and writes a severity and a structured description. The **indexer** clones a customer repo on install and on every push to the default branch, extracts external dependencies and call sites, and writes `dependency` and `call_site` rows. The **matcher** joins new change events against call sites and creates an `impact` row per affected repo, which is what opens an issue. The **fixer** takes an impact, runs a coding agent in a sandbox, validates the patch, and opens a PR. The **runtime SDK** sends `runtime_event` rows from customer processes and feeds both the matcher (undocumented drift) and the watcher (new dependencies seen in the wild).

**Stack.** Python 3.12, FastAPI, SQLAlchemy 2 with Alembic, Postgres 16, a Postgres-backed queue (SKIP LOCKED pattern) rather than Redis, httpx, tree-sitter with the Python and TypeScript grammars, PyGithub for the App, Docker for sandboxes, Anthropic or OpenAI SDK behind a single `llm.py` adapter, Pydantic for every event schema. Deploy on Fly.io or Railway with one web process and two worker processes; S3-compatible storage for snapshots over 1 MB.

**Repo layout.**

```
kintsua/
  api/          FastAPI app: GitHub webhooks, SDK ingest, settings pages
  core/         models.py, db.py, queue.py, llm.py, config.py
  watchers/     base.py, openapi.py, mcp.py, packages.py, changelog.py
  classify/     rules.py, llm_classify.py, schema.py
  indexer/      clone.py, python_extract.py, ts_extract.py, mcp_config.py
  matcher/      match.py
  fixer/        sandbox.py, agent.py, validate.py, pr.py
  sdk/          kintsua-python: wrap.py, report.py (published to PyPI)
  cli/          kintsua watch (published as the open-source core)
  tests/
  alembic/
```

**Data flow in one sentence.** Watcher snapshot → diff → change_event → classifier severity → matcher joins call_sites → impact → GitHub issue → fixer PR, with runtime events entering at the matcher as a second source of change events.

## Data model

Eleven tables. Every event is immutable; state lives on `impact` and `job`. All ids are UUIDs, all timestamps are `timestamptz`, and every table carries `created_at`.

```
-- tenancy
org            (id, github_installation_id, name, plan, stripe_customer_id)
repo           (id, org_id, github_repo_id, full_name, default_branch, last_indexed_sha, active)

-- what we watch
source         (id, kind, locator, display_name, poll_interval_s, last_polled_at, last_error)
               -- kind: openapi | mcp | pypi | npm | changelog
               -- locator: spec URL | MCP server URL or stdio command | package name | page URL
snapshot       (id, source_id, content_hash, storage_ref, captured_at)
               -- storage_ref: inline jsonb if < 256 KB else s3 key
change_event   (id, source_id, from_snapshot_id, to_snapshot_id, diff jsonb, detected_at)
classification (id, change_event_id, severity, category, summary, affected_paths jsonb,
                rule_hits jsonb, llm_model, llm_confidence, created_at)
               -- severity: breaking | degraded | safe | unknown

-- what customers depend on
dependency     (id, repo_id, source_id, kind, name, version_constraint, first_seen_sha)
call_site      (id, dependency_id, file_path, line, symbol, operation, args_shape jsonb)
               -- operation: HTTP method+path | tool name | SDK method

-- what we do about it
impact         (id, repo_id, change_event_id, classification_id, status, github_issue_number,
                github_pr_number, fix_job_id, created_at, resolved_at)
               -- status: detected | issued | fixing | pr_open | merged | dismissed | failed
runtime_event  (id, repo_id, sdk_key_hash, tool_name, schema_fingerprint, outcome,
                error_class, latency_ms, sample jsonb, observed_at)
               -- outcome: ok | error | schema_mismatch
job            (id, kind, payload jsonb, status, attempts, run_after, locked_at, last_error)
```

The normalised change event is a Pydantic model shared by every watcher so the classifier never sees source-specific shapes:

```python
class ChangeEvent(BaseModel):
    source_id: UUID
    source_kind: Literal["openapi", "mcp", "pypi", "npm", "changelog"]
    from_version: str | None     # semver, spec version, or snapshot hash
    to_version: str | None
    ops: list[ChangeOp]          # one per atomic change
    raw_diff_ref: str            # storage pointer to the full diff

class ChangeOp(BaseModel):
    op: Literal["added", "removed", "renamed", "type_changed",
                "required_added", "required_removed", "enum_changed",
                "auth_changed", "deprecated", "text_changed"]
    path: str                    # JSON pointer: /paths/~1v1~1charges/post/parameters/3
    before: Any | None
    after: Any | None
```

Indexes that matter: `snapshot(source_id, captured_at desc)`, `call_site(dependency_id, operation)`, `impact(repo_id, status)`, `runtime_event(repo_id, tool_name, observed_at desc)`, and a partial index on `job(run_after) where status = 'queued'` for the queue poll.

## Watchers

Four adapters implement one interface. Each fetches the current state of a source, returns a canonical document, and the base class handles hashing, snapshot storage, diffing and event emission. Build them in this order because each is harder than the last: MCP, OpenAPI, packages, changelog.

```python
class Watcher(Protocol):
    kind: str
    async def fetch(self, source: Source) -> CanonicalDoc: ...
    def diff(self, before: CanonicalDoc, after: CanonicalDoc) -> list[ChangeOp]: ...

class CanonicalDoc(BaseModel):
    version: str | None
    body: dict            # normalised, key-sorted, no volatile fields
```

**MCP watcher.** Connect to the server (streamable HTTP or stdio via the official `mcp` Python client), call `initialize`, `tools/list`, `resources/list` and `prompts/list`, and store the full tool definitions including `inputSchema`. Strip volatile fields (timestamps, request ids). Diff tool by tool: a removed tool is `removed`, a changed `inputSchema` is walked with a JSON Schema diff that yields `required_added`, `type_changed`, `enum_changed`, and a changed description with an unchanged schema is `text_changed` (still recorded, because the September 2026 research showed drift behind unchanged descriptions). Poll every 6 hours; every hour for sources a paying customer depends on.

**OpenAPI watcher.** Fetch the spec URL, parse 3.x (and 2.0 via conversion), dereference `$ref`, sort keys. Diff with `oasdiff` as a subprocess for the first version (it is mature and emits breaking-change categories), mapped into `ChangeOp`; replace with a native walker later if the subprocess becomes a bottleneck. Seed list: Stripe, Slack, GitHub, Notion, HubSpot, Linear, Jira, Google Workspace discovery docs, Twilio, Shopify, plus any spec URL a customer adds. Poll daily.

**Package watcher.** Poll the PyPI JSON API and the npm registry for the SDKs found in customer repos (section 6). A new version is a change event whose `ops` come from two places: semver (major bump is `deprecated` on the old major, flagged for the classifier) and the release notes fetched from GitHub Releases or CHANGELOG.md, which are passed to the classifier as text. Poll every 6 hours.

**Changelog watcher.** For vendors without a spec (or whose spec lags the docs), fetch a changelog page, extract the main content with `trafilatura`, hash it, and diff paragraphs. Every change is `text_changed` and goes straight to the LLM classifier. Poll daily. Keep this adapter small; it exists so the weekly report can cover vendors the other three miss.

**Scheduling.** One `poll_source` job per source, enqueued by a tick job every 15 minutes that selects sources where `last_polled_at + poll_interval_s < now()`. Failures record `last_error` and back off exponentially to a 48-hour ceiling. A source that fails for 7 days is flagged in the weekly report, since a vanished spec is itself a signal.

**Week-1 deliverable.** The MCP and OpenAPI watchers running against 40 seeded sources with a daily `python -m kintsua.watchers.run` and a Markdown report generator that lists every change event by severity. That report is the artefact you email to 30 engineering leads.

## Classifier

Rules decide first and the model only handles what rules cannot, so the cost per change event stays near zero and every decision is reproducible. Output is one `Classification` row per change event.

```python
class Classification(BaseModel):
    severity: Literal["breaking", "degraded", "safe", "unknown"]
    category: Literal["removal", "rename", "required_param", "type_change",
                      "auth", "deprecation", "behaviour", "docs_only"]
    summary: str                  # one sentence, plain English, for the issue title
    affected_paths: list[str]     # operations / tool names a consumer could call
    migration_hint: str | None    # what a fix looks like, if obvious
    rule_hits: list[str]
    llm_confidence: float | None
```

**Rules (`classify/rules.py`).** Applied in order; the first matching rule sets severity and the rest add context. Removed path, operation, tool or response field → breaking, removal. `required_added` on a request parameter or tool input → breaking, required_param. `type_changed` on any request or response field → breaking, type_change. `renamed` (detected when a removal and an addition share a Levenshtein distance under 3 or identical schema) → breaking, rename, with `migration_hint` naming old and new. `enum_changed` with values removed → breaking; values added → degraded. `auth_changed` (scopes, security schemes) → breaking, auth. `deprecated` or a semver major bump → degraded, deprecation, with the next poll promoted to breaking if the deprecated thing disappears. `added` anything optional → safe. `text_changed` alone → unknown, which routes to the model.

**Model residue (`classify/llm_classify.py`).** Only `unknown` events and all `changelog` and package release-note events reach the model. Prompt: the raw diff or release text, the list of operations and tool names consumers use (from `call_site` across all repos, anonymised), and the `Classification` JSON schema as the required output. Use structured output mode so parsing never fails. Temperature 0. Log the prompt hash, model, and confidence. Below 0.6 confidence the severity is forced to `unknown` and the event appears in the weekly report under "needs a human look" rather than opening issues.

**Promotion from runtime.** When the matcher sees three or more `runtime_event` rows with `outcome = schema_mismatch` for a tool within 24 hours and no corresponding change event, the classifier synthesises a change event of kind `observed` with severity breaking and category behaviour. This is the signal that the vendor changed something they never documented.

**Evaluation.** Keep a `tests/fixtures/changes/` directory of real diffs collected in week 1, each with a hand-labelled severity. The rules must reach 100% on their covered cases and the model must reach 90% agreement on the residue before the GitHub App opens issues automatically; until then issues are created in draft and reviewed by you.

## Repo indexer

The indexer answers one question per repo: which external things does this code call, and from which lines. It runs on install and on every push to the default branch, in a sandbox, with a 10-minute budget, and writes `dependency` and `call_site` rows. Three extractors feed it.

**MCP config extractor (`indexer/mcp_config.py`).** Cheapest and highest-value. Scan for `.mcp.json`, `mcp.json`, `claude_desktop_config.json`, `.cursor/mcp.json`, `.vscode/mcp.json`, and any JSON or YAML file containing an `mcpServers` key. Each entry yields a `dependency` of kind `mcp` with its URL or stdio command as the locator; a stdio command referencing an npm or PyPI package also yields a package dependency. Then grep the code for tool-name string literals that match the server's `tools/list` (fetched by the MCP watcher) and record each as a `call_site` with `operation = tool name`. Also detect framework-level registrations: `MCPServerStdio(...)`, `MCPServerHTTP(...)` in Google ADK, LangChain's `MultiServerMCPClient`, OpenAI Agents SDK `MCPServer*`, and Anthropic SDK `mcp_servers=[...]`.

**Python extractor (`indexer/python_extract.py`).** Parse every `.py` with tree-sitter. Record imports of known SDK packages (a curated map of about 60 packages to vendors, extended by `pyproject.toml` and `requirements*.txt`). Record calls where the callee is a method on an object created from one of those imports, capturing the method chain (`stripe.Charge.create`, `slack_client.chat_postMessage`) as `symbol` and the keyword-argument names as `args_shape`. Record raw HTTP calls (`httpx`, `requests`, `aiohttp`) whose URL argument is a string literal or f-string with a literal host, normalising the path (`/v1/charges/{id}`) and method; those become `call_site` rows against an `openapi` dependency keyed by host.

**TypeScript extractor (`indexer/ts_extract.py`).** Same shape with the TypeScript and TSX grammars: `package.json` for SDK dependencies, `import`/`require` for bindings, member-call chains for SDK methods, `fetch`/`axios`/`got` for raw HTTP.

**Resolution.** Each dependency is resolved to a `source` row: SDK package → package source plus the vendor's OpenAPI source from the curated map; raw HTTP host → the OpenAPI source for that host if known, else a changelog source if one is configured, else recorded as unwatched (shown in the dependency map as grey). MCP → an MCP source keyed by URL or command.

**Output.** A `dependency_map.json` committed nowhere but rendered into the install issue ("Kintsua found 14 external dependencies and 61 call sites; 11 watched, 3 unwatched") and visible in the settings page. The install issue is the first thing a user sees and must land within one hour; measure this.

**Known gaps to state in the README.** Dynamic tool names built at runtime, URLs assembled from config, and monorepos with multiple services are indexed partially; the runtime SDK (section 9) closes these gaps by observing real calls.

## GitHub App

GitHub is the entire v0 user interface: install is the signup, the install issue is the dependency map, issues are alerts, and PRs are fixes. Register the App as `kintsua` with these permissions: Contents read and write (clone, branch, commit), Pull requests read and write, Issues read and write, Metadata read, Checks read (to read the customer's CI result on our PR), and the `installation`, `push`, `pull_request` and `issues` webhook events. No org-member or email scopes.

**Install flow (`api/github/webhooks.py`).** On `installation.created`, create an `org` and one `repo` row per selected repository, then enqueue `index_repo` for each. On `installation_repositories.added` do the same for the new repos. On `installation.deleted`, mark repos inactive and stop watching sources no other org depends on. The install issue is created by the indexer when it finishes, titled "Kintsua is watching 11 of your 14 external dependencies", with a table of dependency, kind, watch status and call-site count, plus a line on how to add a spec or changelog URL for the unwatched ones via a `.kintsua.yml` file.

**Configuration (`.kintsua.yml`, optional).** Lets a team add sources the indexer missed, mute noisy ones, choose whether PRs open automatically or only issues, and set the base branch for fixes.

```yaml
sources:
  - kind: openapi
    url: https://api.vendor.com/openapi.json
  - kind: changelog
    url: https://vendor.com/changelog
mute:
  - stripe:/v1/legacy/*
fix:
  mode: pr          # pr | issue_only
  base: main
```

**Push handling.** On `push` to the default branch, enqueue `index_repo` with a 5-minute debounce so a burst of commits indexes once. Re-index diffs old and new call sites and closes any open impact whose call site disappeared, with a comment saying why.

**Issue creation (`matcher/match.py` → `api/github/issues.py`).** When a `breaking` or `degraded` classification matches a repo's call sites, create one issue per change event and repo. Title: the classifier summary. Body: what changed (before/after), when detected, which files and lines in this repo call it, the migration hint, and a status line that Kintsua will open a PR within an hour if fix mode is `pr`. Label `kintsua`, `severity:breaking`. Never open duplicates: key on `(repo_id, change_event_id)`.

**PR creation (`fixer/pr.py`).** Branch `kintsua/<vendor>-<short-hash>`, one commit, PR body from the template in section 8, linked to the issue with "Closes #N". When the customer's CI reports on the PR, store the result on the impact; a failing CI moves status to `failed` and adds a comment with the log excerpt and what Kintsua could not fix.

**Rate limits and safety.** Use the installation token per org, cache it until expiry, respect secondary rate limits with a per-org semaphore of 2 concurrent writes. Never push to a protected branch; never force-push. Everything Kintsua writes is prefixed and labelled so it can be found and reverted.

## Fixer

The fixer turns an `impact` into a PR or an honest failure, inside a sandbox, in under 15 minutes, for the three change categories that cover most real breaks: rename, required_param and deprecation (SDK major bump). Everything else stays an issue in v0.

**Sandbox (`fixer/sandbox.py`).** One Docker container per job from a base image with Python 3.12, Node 20, git and the common test runners. No network except to the package registries and the GitHub API via an egress allowlist, so a prompt-injected changelog cannot exfiltrate the customer's code. Clone with the installation token at the default-branch head, install dependencies with the lockfile if present, and detect the test command from `pyproject.toml`, `package.json` scripts, `Makefile` or a `.kintsua.yml` override. Hard limits: 15 minutes wall clock, 4 GB memory, 2 CPUs. The container is destroyed after the job; only the diff and logs leave it.

**Agent loop (`fixer/agent.py`).** A bounded tool-using loop, not an open-ended coding agent. Tools available to the model: `read_file`, `search`, `apply_patch`, `run_tests`, `done`. Maximum 12 steps. The system prompt contains the classification, the before/after diff, the migration hint, the affected call sites with 20 lines of context each, and the rules: change only the named call sites and their direct helpers, do not touch unrelated files, do not change tests except to update expectations that the vendor change makes necessary, and stop with `done(reason)` if the fix needs a decision a human should make (for example a new required parameter with no inferable value). Use the same `llm.py` adapter as the classifier; default to the strongest coding model available and log tokens per job so unit cost is known by week 5.

**Validation (`fixer/validate.py`).** Before any PR: the patch applies cleanly; the diff touches only files that contain affected call sites or their imports (configurable radius of 1 import hop); the test command exits 0, or exits with the same failures as the baseline run on the unpatched tree (the baseline is run first and cached per SHA, so a repo with pre-existing red tests still gets fixes); static checks pass where present (`ruff`, `tsc --noEmit`). For MCP tool changes, additionally re-validate the new `inputSchema` against the arguments the patched call site constructs, using a JSON Schema validator on a synthetic call extracted from the indexer's `args_shape`. Any failure moves the impact to `failed` with the reason and a comment on the issue.

**PR body template.**

```
## What changed upstream
{vendor} {source_kind} {from_version} → {to_version}, detected {detected_at}.
{summary}

## What this PR changes
{files_changed} files, {call_sites_changed} call sites.
{bulleted list of file:line and one-line description}

## How it was verified
- Test command: `{test_cmd}` → exit 0 ({tests_passed} passed)
- Baseline on unpatched tree: {baseline_result}
- Static checks: {static_result}

## Review notes
{migration_hint and anything the agent flagged for a human}

Closes #{issue_number}. Opened by Kintsua.
```

**What the fixer refuses in v0.** Changes that touch authentication or secrets handling, changes requiring new credentials or scopes, repos with no test command and no static checks (issue only, with a note that adding either unlocks PRs), and any job where the agent's patch exceeds 300 changed lines. These rules go in the README so the behaviour is predictable.

**Learning loop.** Every PR outcome (merged, closed, edited before merge) is recorded on the impact. Merged-as-is rate is the north-star quality metric for the fixer and goes in the weekly report; by the application you want a real number here even if it is small.

## Runtime SDK

The SDK is the signal nobody else has: it sees what tools an agent actually calls and whether they still answer the way they did yesterday. v0 ships Python only, as `kintsua` on PyPI, under 500 lines, with no required code changes beyond one wrapper call.

```python
import kintsua

# MCP: wrap any client session object from the official mcp package
session = kintsua.wrap_mcp(session, repo="acme/support-agent", key=KINTSUA_KEY)

# HTTP: wrap an httpx or requests client
client = kintsua.wrap_http(httpx.AsyncClient(), repo="acme/support-agent", key=KINTSUA_KEY)

# Frameworks: one-line hooks
kintsua.hook_langchain(); kintsua.hook_openai_agents(); kintsua.hook_google_adk()
```

**What it records (`sdk/wrap.py`).** For MCP: on `initialize` and `tools/list`, compute a `schema_fingerprint` per tool (SHA-256 of the canonicalised `inputSchema`) and compare with the last fingerprint stored locally; a difference becomes a `schema_mismatch` event with the diff. On every `tools/call`, record tool name, latency, outcome, and the error class and message on failure; arguments and results are never sent unless the customer sets `sample=True`, in which case keys only, no values. For HTTP: host, method, normalised path, status, latency, and on 4xx/5xx the error body's first 512 bytes with obvious secrets redacted by regex.

**Transport (`sdk/report.py`).** Events are buffered in memory, batched every 5 seconds or 100 events, and POSTed to `/v1/ingest` with the SDK key. On failure the batch is dropped after one retry; the SDK must never block or crash the agent, so every public function is wrapped in a broad except that logs once at debug level. Total overhead budget: under 1 ms per call.

**Server side.** `/v1/ingest` validates the key, writes `runtime_event` rows, and enqueues `match_runtime` which does three things: creates `dependency` and `call_site` rows for tools and hosts the indexer never saw (marked `discovered_at_runtime`), promotes clusters of `schema_mismatch` into synthetic change events as described in section 5, and attaches `error` spikes to any open impact for the same tool so the issue shows "12 failed calls in the last hour" as proof the break is live.

**Privacy defaults.** No payloads, no PII, keys hashed at rest, per-repo data deletion endpoint from day one. State this on the landing page; it is a buying objection you can pre-empt.

## CLI and open-source core

`kintsua watch` is the MCP watcher and the rules classifier packaged as a CLI under an Apache 2.0 licence, published in week 6. It is the top of the funnel: a developer adds one step to CI, gets value with no account, and sees the hosted product's name on every run.

```
pip install kintsua
kintsua watch snapshot --server https://mcp.vendor.com/mcp   # writes .kintsua/vendor.lock.json
kintsua watch diff --server https://mcp.vendor.com/mcp       # exit 1 on breaking changes
kintsua watch report --since 7d                               # Markdown report to stdout
```

The lockfile is the canonical `tools/list` with fingerprints, committed to the repo so a schema change shows up in a PR diff. `diff` prints a table of changed tools, severity and the rule that fired, and exits non-zero on `breaking` so it can gate a deploy. A `--github-action` flag emits annotations. The hosted product is one flag away: `kintsua watch --push` sends snapshots to the account so the repo gets issues and PRs without CI running.

Keep the boundary clean: the CLI contains `watchers/mcp.py`, `watchers/openapi.py` and `classify/rules.py` and nothing else; the LLM classifier, indexer, matcher and fixer stay closed. Publish the weekly drift report from the hosted watcher to a public page and an email list; it is generated by the same report command.

## API surface and minimal web

Eleven endpoints and two pages. Everything else happens in GitHub.

| Method and path | Auth | Purpose |
|---|---|---|
| POST /webhooks/github | GitHub signature | Installation, push, PR and issue events |
| POST /v1/ingest | SDK key | Runtime event batches |
| GET /v1/repos/{id}/dependencies | Session | Dependency map JSON for the settings page |
| GET /v1/repos/{id}/changes | Session | Change events affecting this repo, newest first |
| POST /v1/repos/{id}/sources | Session | Add a spec or changelog URL |
| POST /v1/repos/{id}/mute | Session | Mute a path or tool pattern |
| POST /v1/repos/{id}/fix-mode | Session | Switch pr / issue_only |
| POST /v1/keys | Session | Create or rotate an SDK key |
| DELETE /v1/repos/{id}/data | Session | Delete all runtime and index data for a repo |
| GET /report/weekly | Public | The weekly drift report page and RSS |
| POST /v1/billing/checkout | Session | Stripe Checkout session for the paid plan |

**Auth.** Sign in with GitHub via the App's OAuth flow; a session is scoped to the installations the user can see. SDK keys are per org, shown once, stored hashed.

**Pages.** `/settings/{repo}` renders the dependency map, the change list, mute rules and fix mode as a server-rendered HTMX page, no SPA. `/report/weekly` is the public page that doubles as marketing. That is the whole web surface for v0.

**Billing.** Stripe Checkout and a webhook that sets `org.plan`. Free: public repos and up to 5 watched integrations. Pro: $29 per watched integration per month, billed on the count of `dependency` rows with a watched source on the first of the month. Keep pricing on one line of the landing page and change it after the first five customers tell you it is wrong.

## Milestones, testing and risks

1. **M1, by 7 October: watcher and drift report.** `core/` models and queue, MCP and OpenAPI watchers, rules classifier, 40 seeded sources, daily run, Markdown report. Done when one week of real change events exists in Postgres and the report has been emailed to 30 engineering leads. Gate: five or more asked for alerts.
2. **M2, by 14 October: GitHub App and indexer.** App registered, webhooks, install flow, MCP config and Python extractors, dependency resolution, install issue. Done when a fresh install on a design partner's repo produces a correct install issue in under one hour.
3. **M3, by 21 October: matcher and issues.** Matching, issue creation, dedupe, `.kintsua.yml`, push re-indexing, TypeScript extractor. Done when a real upstream change opens a correct issue on a partner repo without manual steps.
4. **M4, by 28 October: fixer.** Sandbox, agent loop, validation, PR template, CI result capture, refusal rules. Done when Kintsua has opened three PRs on partner repos and at least one is merged.
5. **M5, by 4 November: runtime SDK, CLI, billing.** PyPI SDK with MCP and httpx wrappers, ingest endpoint, runtime matching, `kintsua watch` published, public weekly report page, Stripe Checkout. Done when two orgs are on Pro and the SDK has reported events from a production agent.
6. **M6, by 7 November: application.** Numbers pulled into the YC draft, 60-second demo recorded, landing page live.

**Testing.** Unit tests on every watcher's `diff` with fixture pairs; a golden set of labelled change events for the classifier with a CI check that rules stay at 100% and the model above 90%; indexer tests on five open-source agent repos (pick real ones using LangChain, ADK, OpenAI Agents SDK, CrewAI, and a raw httpx agent) with expected dependency counts; an end-to-end test that stands up a local MCP server, changes a tool schema, and asserts an issue payload is produced; fixer tests that replay recorded breaks against a fixture repo in the sandbox and assert the validator's verdict. Target under 10 minutes for the full suite so it runs on every push.

**Risks and what to do about them.** Rate limits and blocked scraping on vendor spec URLs: cache aggressively, honour robots.txt, and prefer registries and official spec endpoints. Noisy classifications destroying trust in the first week: start every org in `issue_only` with draft issues you review, and only flip to automatic after the golden set passes. Fixer producing wrong patches: the validator is the product, so never ship a PR whose tests did not run; a smaller merged-as-is rate is better than a larger wrong-PR rate. Sandbox cost: cap concurrent fix jobs at 3 and record token spend per job from day one. Prompt injection via vendor changelogs or tool descriptions: treat all fetched text as data, strip it from any instruction position in prompts, and keep the sandbox egress allowlist. A competitor shipping detection first: expected, since detection is commoditising; the moat is the fix PR grounded in call sites plus runtime evidence, so protect the pace on M3 to M5 above all.

## Architecture diagram

Every component talks through Postgres and a job queue, never to each other. Change events flow left to right from the watchers to a GitHub PR; the indexer and the runtime SDK feed the matcher from above and below, and GitHub webhooks trigger re-indexing. Every arrow is a job on the Postgres queue.

```
                 ┌──────────┐   install + push webhooks
                 │ Indexer  │◄─────────────────────────────┐
                 │maps call │                               │
                 │  sites   │                               │
                 └────┬─────┘                               │
 change events   sev. │ impact                 PR + issue   │
┌─────────┐  ┌────────▼──┐  ┌─────────┐  ┌────────────┐  ┌─┴────────┐
│Watchers ├─►│Classifier ├─►│ Matcher ├─►│   Fixer    ├─►│GitHub App│
│poll+diff│  │rules→LLM  │  │join call│  │sandbox + PR│  │issues/PRs│
└─────────┘  └───────────┘  │  sites  │  └────────────┘  └──────────┘
                            └────▲────┘
                            ┌────┴──────┐
                            │Runtime SDK│
                            │failed call│
                            └───────────┘
┌──────────────────────────────────────────────────────────────────────┐
│ Postgres: snapshots, change events, call sites, impacts, job queue   │
└──────────────────────────────────────────────────────────────────────┘
```
