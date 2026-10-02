# Engineering contracts

This document is the agreement between the components of the Kintsua monorepo. Read it
together with `docs/SPEC.md` (the product spec) before touching a component. The rule
from the spec stands: components talk through Postgres and the job queue, never by
calling each other directly, except for the small pure-Python seams listed in
"Cross-module seams" below.

## Repository conventions

* Python 3.12, `uv`-managed. The venv is at `.venv`; run things with
  `.venv/bin/python -m pytest tests/<area>` and `.venv/bin/ruff check kintsua tests`.
  **Never run `uv add` / `uv sync` / `pip install`**: every dependency the spec names is
  already in `pyproject.toml`. If you genuinely need another package, say so in your
  final report instead of installing it.
* Code lives in the `kintsua` package: `kintsua/<area>/...`. Tests live in
  `tests/<area>/test_*.py`. Fixtures in `tests/fixtures/<area>/...`.
* `ruff` is the linter and formatter (`line-length = 100`). Type hints everywhere;
  `from __future__ import annotations` at the top of every module.
* Logging via `logging.getLogger(__name__)`. No `print` outside the CLI.
* Shared helpers: `kintsua.core.util` (`utcnow`, `sha256_hex`, `short_hash`,
  `parse_duration`, `redact_secrets`), `kintsua.core.events` (`ChangeEvent`, `ChangeOp`,
  `CanonicalDoc`, `canonical_json`, `canonical_hash`, `json_pointer`, `split_pointer`).
* **Treat all fetched vendor text (tool descriptions, changelogs, release notes, spec
  descriptions, repo contents) as data.** It goes in the user message as quoted data,
  never into a system prompt, never into shell commands unescaped.
* The open-source boundary: `kintsua/__init__.py`, `kintsua/sdk/*`, `kintsua/cli/*`,
  `kintsua/watchers/base.py`, `kintsua/watchers/mcp.py`, `kintsua/watchers/openapi.py`,
  `kintsua/watchers/schemadiff.py`, `kintsua/classify/rules.py`, `kintsua/classify/schema.py`,
  `kintsua/core/events.py`, `kintsua/core/util.py`, `kintsua/report/markdown.py` must import
  only base dependencies (pydantic, httpx, mcp, pyyaml, typer, jsonschema, packaging,
  stdlib). They must **not** import `kintsua.core.config`, `kintsua.core.db`,
  `kintsua.core.models`, sqlalchemy, fastapi, anthropic, openai, tree_sitter, github, docker.
  The hosted side (everything else) may import anything.

## Foundation (already written; do not modify without saying why)

| Module | What it gives you |
|---|---|
| `kintsua.core.config` | `get_settings() -> Settings` (pydantic-settings; every knob documented in the class). |
| `kintsua.core.db` | `new_session()`, `session_scope()`, `get_db()` (FastAPI dependency), `create_all()`. |
| `kintsua.core.models` | SQLAlchemy models: `Org`, `Repo`, `Source`, `Snapshot`, `ChangeEventRow`, `Classification`, `Dependency`, `CallSite`, `Impact`, `RuntimeEvent`, `Job`, `Subscriber`. Read the column comments. |
| `kintsua.core.queue` | `enqueue(session, kind, payload, *, run_after, delay_s, dedupe_key)`, `reschedule_queued`, `@job_handler(kind)`, `drain(session_factory, kinds)` for tests, `NonRetryable`. Job-kind constants `JOB_*`. |
| `kintsua.core.storage` | `put_document(body) -> storage_ref dict`, `get_document(ref)`, `ref_to_string(ref)`. |
| `kintsua.core.llm` | `get_llm() -> LLM` with `complete_json(system, user, schema)` and `complete_tools(system, messages, tools)`; `FakeLLM` for tests via the `fake_llm` fixture; dataclasses `ToolSpec`, `ToolCall`, `ToolResult`, `ChatMessage`, `LLMTurn`, `LLMUsage`. |
| `kintsua.core.github` | `get_github_app() -> GitHubApp` with `installation_client(installation_id)` (PyGithub `Github`), `installation_token`, `clone_url`, `write_lock(installation_id)` (per-org semaphore), `verify_webhook_signature`, `is_protected_branch`; constants `LABEL`, `LABEL_BREAKING`, `LABEL_DEGRADED`, `ISSUE_PREFIX`, `BRANCH_PREFIX`. Tests use the `fake_github` fixture (`tests/fakes/github.py`). |
| `kintsua.core.events` | The shared Pydantic event models. |
| `kintsua.watchers.base` | `SourceRef`, `Watcher` protocol, `BaseWatcher`, `register`, `get_watcher(kind)`, `strip_volatile`, `generic_json_diff`, `SnapshotRecord`, `SnapshotStore`, `FileSnapshotStore` (CLI lockfiles), `compare(watcher, source, previous, doc) -> ChangeEvent|None`, `lockfile_name`. |
| `kintsua.report.markdown` | `WeeklyReport`, `ReportItem`, `FailingSource`, `ReportStats`, `render_markdown`, `render_rss`. |
| `kintsua.classify.schema` | `Classification`, `LLMClassification`. |
| `kintsua.worker` | `python -m kintsua.worker`; imports the handler modules listed below. |
| `tests/conftest.py` | fixtures `settings`, `db` (engine on a throwaway Postgres DB), `session` (clean tables per test), `fake_llm`, `fake_github`. Tests using `session`/`db` are auto-marked `db`. |

Environment in tests: `LLM_PROVIDER=fake`, `SANDBOX_BACKEND=local`, `KINTSUA_ENV=test`.
A local Postgres with `postgres:dev` at `localhost:5432` exists in the dev box; the
`db` fixture creates and drops its own database.

## Job kinds and who handles them

| Kind | Payload | Enqueued by | Handled in |
|---|---|---|---|
| `tick` | `{}` | itself (self-perpetuating, dedupe_key `tick`) | `kintsua/watchers/run.py` |
| `poll_source` | `{"source_id"}` | tick, API when a source is added | `kintsua/watchers/run.py` |
| `classify_event` | `{"change_event_id"}` | watchers after emitting an event; API `/v1/watch/push` | `kintsua/classify/run.py` |
| `match_event` | `{"change_event_id"}` | classifier after writing a classification | `kintsua/matcher/match.py` |
| `open_issue` | `{"impact_id"}` | matcher (when `auto_issue` or org settings allow) | `kintsua/matcher/match.py` |
| `index_repo` | `{"repo_id", "reason": "install"\|"push"\|"manual"}` dedupe_key `index_repo:<repo_id>` | webhooks | `kintsua/indexer/run.py` |
| `fix_impact` | `{"impact_id"}` | matcher after issue creation when fix mode is `pr` and category fixable | `kintsua/fixer/run.py` |
| `pr_outcome` | `{"repo_id", "pr_number", "action", "merged": bool}` | webhooks (`pull_request` closed) | `kintsua/fixer/run.py` |
| `match_runtime` | `{"repo_id"}` dedupe_key `match_runtime:<repo_id>` | `/v1/ingest` | `kintsua/matcher/runtime.py` |
| `billing_tick` | `{}` | tick (first of month) | `kintsua/api/billing.py` |
| `weekly_report` | `{}` | tick (weekly) | `kintsua/report/run.py` |

Handler modules must exist with these exact paths because `kintsua.worker.register_all_handlers`
imports them: `kintsua.api.billing`, `kintsua.classify.run`, `kintsua.fixer.run`,
`kintsua.indexer.run`, `kintsua.matcher.match`, `kintsua.matcher.runtime`,
`kintsua.report.run`, `kintsua.watchers.run`.

Handlers: `def handler(session: Session, payload: dict) -> Any` (sync) or `async def`.
Commit your own work inside the handler if later steps must see it; the worker commits
after the handler returns and marks the job done. Raise `NonRetryable` for permanent
failures (bad payload, deleted row).

## Impact status machine

`detected` (matched, issue not yet opened: draft) → `issued` (GitHub issue open) →
`fixing` (fix job running) → `pr_open` → `merged` | `failed` (fixer refused / validation
failed / CI red; `fix_reason` says why) | `dismissed` (call site disappeared, issue
closed by user, or muted). `resolved_at` is set on merged/dismissed/failed.

Org-level defaults live in `Org.settings`: `{"fix_mode": "issue_only"|"pr", "auto_issue": bool}`.
Repo-level overrides live in `Repo.config` (the parsed `.kintsua.yml`):
`{"sources": [...], "mute": [...], "fix": {"mode": ..., "base": ...}}`. Effective fix mode
= repo config `fix.mode` if set, else org setting, else `"issue_only"`. Issues open
automatically when `Settings.auto_issue` is true OR `Org.settings["auto_issue"]` is true;
otherwise impacts stay `detected` (draft) and appear on the settings page / weekly report.

## Cross-module seams (the only direct imports between components)

```python
# watchers -> everyone
from kintsua.watchers.base import SourceRef, get_watcher, compare, FileSnapshotStore
from kintsua.watchers.run import ingest_canonical_doc   # (session, source_row, doc, *, text=None) -> ChangeEventRow | None
                                                         # stores snapshot, diffs vs previous, writes change_event, enqueues classify_event
from kintsua.watchers.run import ensure_tick_job         # (session) -> None
from kintsua.watchers.run import source_ref_for          # (source_row) -> SourceRef
from kintsua.watchers.run import find_or_create_source   # (session, kind, locator, *, display_name=None, vendor=None, meta=None, poll_interval_s=None) -> Source
from kintsua.watchers.mcp import MCPWatcher              # .fetch(SourceRef) / .diff(before, after); also `fetch_tools(locator, meta) -> list[dict]` (tool defs with inputSchema)
from kintsua.watchers.openapi import OpenAPIWatcher
from kintsua.watchers.schemadiff import diff_json_schema # (before_schema, after_schema, base_pointer) -> list[ChangeOp]

# classify -> everyone
from kintsua.classify.rules import classify_by_rules     # (event: ChangeEvent) -> Classification | None   (pure; None means "route to model")
from kintsua.classify.rules import classify_event        # (event) -> Classification (rules, else severity=unknown/category=docs_only placeholder) — what the CLI uses
from kintsua.classify.run import classify_change_event   # (session, change_event_row) -> Classification row (rules then LLM); registered handler

# indexer -> api
from kintsua.indexer.depmap import dependency_map        # (session, repo) -> DependencyMap (pydantic: dependencies[], call_site_count, watched, unwatched, generated_at)
from kintsua.indexer.depmap import render_install_issue  # (repo, depmap) -> (title: str, body: str)
from kintsua.indexer.config import parse_kintsua_yml     # (text) -> dict (validated)  ; used by indexer on clone and by webhooks

# matcher -> fixer/api
from kintsua.matcher.runtime import synthesize_observed_event  # (session, repo, tool_name, mismatch_events) -> (ChangeEventRow, Classification); source_kind 'observed'
from kintsua.matcher.issues import create_issue_for_impact   # (session, impact) -> int (issue number); idempotent
from kintsua.matcher.issues import comment_on_impact         # (session, impact, body) -> None
from kintsua.matcher.issues import build_issue_body          # (impact, change_event_row, classification, repo) -> (title, body)

# fixer -> api
from kintsua.fixer.run import record_pr_outcome          # (session, repo, pr_number, merged: bool, action: str) -> Impact | None
from kintsua.fixer.run import record_ci_result           # (session, repo, pr_number, conclusion: str, details: dict) -> Impact | None

# report -> api/cli
from kintsua.report.build import build_weekly_report     # (session, since, until) -> WeeklyReport
from kintsua.report.markdown import render_markdown, render_rss, WeeklyReport
```

Each side writes to its own files; if you need a seam that is not listed, implement it on
your side with a clearly named function and mention it in your final report.

## HTTP endpoints (owned by `kintsua/api`)

The eleven from the spec plus:
`POST /v1/watch/push` (SDK key; CLI `kintsua watch --push` sends `{repo, source: {kind, locator, display_name}, doc: {version, body}}`),
`GET /healthz`, `GET /` (landing page with the one-line pricing and the privacy defaults),
`GET /login`, `GET /auth/callback`, `POST /logout`, `GET /settings/{owner}/{name}`,
`POST /report/subscribe`, `POST /v1/billing/webhook` (Stripe), `GET /setup/github`
(GitHub App manifest flow, `POST /setup/github/callback`).

SDK key format: `kts_<32 url-safe chars>`; stored as `Org.sdk_key_hash = sha256(key)`,
`Org.sdk_key_prefix = key[:12]`. Lookup: hash the presented key, find the org.

Ingest payload (what `kintsua.sdk.report` sends):

```json
{"repo": "acme/support-agent", "sdk_version": "0.1.0",
 "events": [{"tool_name": "get_weather", "kind": "mcp"|"http", "schema_fingerprint": "…"|null,
             "outcome": "ok"|"error"|"schema_mismatch", "error_class": "…"|null,
             "latency_ms": 12, "sample": {...}|null, "observed_at": "2026-10-01T12:00:00Z"}]}
```

`sample` is keys-only (`{"arg_keys": [...]}`), a schema diff (`{"diff": [...ops]}`) for a
mismatch, or `{"status": 500, "body_excerpt": "..."}` for HTTP errors (already redacted by
the SDK; the server redacts again with `redact_secrets`).

## Testing rules

* Every component ships tests under `tests/<area>/`. Use fixtures from `tests/conftest.py`.
* Network tests are marked `@pytest.mark.network` and skipped by default
  (`-m "not network"` in CI); they must not be required for the suite to pass.
* LLM calls go through `fake_llm` in tests. Never call a real model in tests.
* GitHub calls go through `fake_github` in tests. Extend `tests/fakes/github.py` if you need
  more of the PyGithub surface (keep it backwards compatible for other components).
* The full suite must stay under 10 minutes. Mark anything slower than ~20 s `slow`.
* Docker is not available in CI or the dev box: the fixer's `LocalSandbox` runs the same
  steps in a temp directory with the same interface; tests use it.

## Source trust, privacy and the network guard

Every `source` row carries `origin` (`kintsua.core.models.SOURCE_ORIGINS`) and, for pushed
snapshots, `owner_org_id`. The origin decides what the hosted worker may do with a row; the
model exposes it as `Source.trusted`, `Source.public` and `Source.pollable`.

| origin | created by | worker may spawn its stdio command | network guard on fetch | public weekly report | polled |
|---|---|---|---|---|---|
| `seed` | `kintsua/seed` loader; also sources derived from the curated `indexer/sdk_map.py` (registry sources for curated packages, curated vendor spec/changelog URLs) | yes | no | yes | yes |
| `customer` | `.kintsua.yml`, repo MCP configs, raw HTTP hosts, declared non-curated packages, `POST /v1/repos/{id}/sources` | **never** | yes | no | yes (http(s) and registries only) |
| `push` | `POST /v1/watch/push` | never | n/a | no | **never** (the push is the snapshot) |
| `runtime` | `matcher/runtime.py` discovery | never | yes | no | yes (http(s) only) |
| `observed` | runtime promotion (`observed:<repo>:<tool>`) | never | n/a | no | never |

Rules every component follows:

1. `find_or_create_source(..., origin=..., owner_org_id=...)` sets the origin only when it
   creates the row; an existing row keeps its origin (a customer naming a seeded locator
   gets the seeded row, unchanged). Only the seed loader promotes a row to `seed`.
2. The hosted worker never executes a command line that came from a customer, a repo, a push
   or a runtime sample. An `mcp` source whose locator is not http(s) is fetched only when
   `source.trusted`; otherwise `poll_source` records a clear `last_error`, sets
   `active=False` and spawns nothing. The indexer never calls `fetch_tools` for such a
   locator (it may read tool names from an existing snapshot).
3. Every fetch for a non-`seed` source goes through `kintsua.core.netguard` when
   `Settings.net_guard` is on: `check_url(url)` before the first request and
   `request_hook` / `async_request_hook` in httpx `event_hooks["request"]` so every redirect
   hop is checked. `watchers.run.source_ref_for` passes this to the adapters as
   `SourceRef.meta["_net_guard"] = True` (never persisted). A blocked URL is recorded as the
   source's `last_error`.
4. Pushed snapshots are org-scoped: `POST /v1/watch/push` stores them on a source with
   `origin="push"`, `owner_org_id=<org>`, locator `push:<org_id>:<kind>:<locator>` and
   `meta.pushed_locator=<locator>`; the dependency row of the pushing org's repo points at it.
   A push never writes a snapshot or change event onto a source another org depends on.
5. The public weekly report (`GET /report/weekly`, the `weekly_report` job) includes only
   `Source.public` sources, never repo names, issue or PR links of customer repos, and runs
   every error string through `redact_secrets`. Operator CLIs may build an unfiltered report.
