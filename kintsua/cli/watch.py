"""``kintsua watch``: snapshot and diff MCP servers (and OpenAPI specs) in CI.

The open-source core: the MCP / OpenAPI watchers, the rules classifier and the Markdown
report, wired to a lockfile directory (``.kintsua/``). Exit codes: 0 nothing at or above
``--fail-on``, 1 the gate fired, 2 usage error or missing lockfile, 3 the source could not be
fetched (connection refused, authentication required, bad spec).
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from kintsua import __version__
from kintsua.classify.rules import HTTP_METHODS, SEVERITY_RANK, affected_operation, classify_event
from kintsua.classify.schema import Classification
from kintsua.core.events import CanonicalDoc, ChangeEvent, ChangeOp, canonical_hash
from kintsua.core.util import parse_duration
from kintsua.report.markdown import (
    FailingSource,
    ReportItem,
    ReportStats,
    WeeklyReport,
    render_markdown,
    to_dict,
)
from kintsua.watchers.base import (
    BaseWatcher,
    FileSnapshotStore,
    SourceRef,
    compare,
    get_watcher,
    lockfile_name,
)

HOSTED = "https://github.com/apps/kintsua"
DEFAULT_URL = "https://api.kintsua.dev"
TAGLINE = f"get issues and fix PRs at {HOSTED}"
EXIT_GATE, EXIT_USAGE, EXIT_FETCH = 1, 2, 3
MAX_ROWS = 200
SEV_STYLE = {"breaking": "bold red", "degraded": "yellow", "safe": "green", "unknown": "dim"}
SEV_MARK = {"breaking": "🔴", "degraded": "🟠", "safe": "🟢", "unknown": "⚪"}
GH_LEVEL = {"breaking": "error", "degraded": "warning"}

app = typer.Typer(
    help="Snapshot and diff MCP servers / OpenAPI specs; gate CI on breaking changes.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


class FailOn(StrEnum):
    breaking = "breaking"
    degraded = "degraded"


ServerOpt = Annotated[
    str | None,
    typer.Option(
        "--server",
        "-s",
        help="MCP server: a streamable-HTTP URL or a stdio command ('npx -y @vendor/mcp').",
    ),
]
SpecOpt = Annotated[
    str | None, typer.Option("--spec", help="OpenAPI spec URL (instead of --server).")
]
DirOpt = Annotated[Path, typer.Option("--dir", help="Directory for lockfiles and history.jsonl.")]
NameOpt = Annotated[
    str | None, typer.Option("--name", help="Lockfile base name (default: from the server).")
]
HeaderOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--header", "-H", help="Extra HTTP header K=V (repeatable; values may use ${VAR})."
    ),
]
EnvOpt = Annotated[
    list[str] | None,
    typer.Option("--env", help="Environment variable K=V for a stdio server (repeatable)."),
]
AuthEnvOpt = Annotated[
    str | None, typer.Option("--auth-env", help="Env var holding a bearer token for the server.")
]
TimeoutOpt = Annotated[float, typer.Option("--timeout", help="Connect timeout in seconds.")]
PushOpt = Annotated[
    bool, typer.Option("--push", help="Also send the snapshot to the hosted service.")
]
RepoOpt = Annotated[
    str | None, typer.Option("--repo", envvar="KINTSUA_REPO", help="owner/name for --push.")
]
KeyOpt = Annotated[
    str | None, typer.Option("--key", envvar="KINTSUA_KEY", help="SDK key for --push.")
]
UrlOpt = Annotated[str, typer.Option("--url", envvar="KINTSUA_URL", help="Hosted API base URL.")]


# --------------------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------------------


def info(msg: str) -> None:
    typer.echo(msg)


def warn(msg: str) -> None:
    typer.echo(f"kintsua watch: warning: {msg}", err=True)


def fail(msg: str, code: int) -> None:
    typer.echo(f"kintsua watch: error: {msg}", err=True)
    raise typer.Exit(code)


def _now() -> datetime:
    return datetime.now(UTC)


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _console() -> Console:
    tty = sys.stdout.isatty()
    return Console(file=sys.stdout, width=None if tty else 160, markup=False, highlight=False)


# --------------------------------------------------------------------------------------
# Targets: flags -> (watcher, SourceRef, lockfile name)
# --------------------------------------------------------------------------------------


@dataclass
class Target:
    watcher: BaseWatcher
    source: SourceRef
    name: str

    @property
    def display(self) -> str:
        return self.source.display_name or self.name

    @property
    def unit(self) -> str:
        return "tools" if self.source.kind == "mcp" else "operations"


def _kv(items: list[str] | None, flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            fail(f"{flag} expects K=V, got {item!r}", EXIT_USAGE)
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


def _host(locator: str) -> str:
    return locator.split("://", 1)[1].split("/", 1)[0] if "://" in locator else locator


def _short_cmd(cmd: str) -> str:
    """``/usr/bin/python /repo/tests/mcp_server.py`` -> ``python mcp_server.py`` (60 chars max)."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    paths = ("/", "./", "../", "~")
    short = " ".join(Path(t).name if t.startswith(paths) else t for t in tokens)
    return short[:60] or cmd[:60]


def _refine_display(t: Target, doc: CanonicalDoc) -> None:
    """Prefer the name the source gives itself: MCP ``serverInfo.name`` / OpenAPI ``info.title``."""
    section, key = ("server", "name") if t.source.kind == "mcp" else ("info", "title")
    name = (doc.body.get(section) or {}).get(key)
    if isinstance(name, str) and name.strip():
        t.source.display_name = name.strip()[:80]


def resolve_target(
    server: str | None,
    spec: str | None,
    headers: list[str] | None = None,
    env: list[str] | None = None,
    auth_env: str | None = None,
    name: str | None = None,
    timeout: float = 60.0,
) -> Target:
    if bool(server) == bool(spec):
        fail("pass exactly one of --server URL|CMD or --spec URL", EXIT_USAGE)
    meta: dict[str, Any] = {"headers": _kv(headers, "--header"), "timeout_s": timeout}
    if spec:
        src = SourceRef.stable("openapi", spec, display_name=_host(spec), meta=meta)
    else:
        meta.update(quiet=True, env=_kv(env, "--env"))
        if auth_env:
            meta["auth_env"] = auth_env
        display = _host(server) if "://" in str(server) else _short_cmd(str(server))
        src = SourceRef.stable("mcp", str(server), display_name=display, meta=meta)
    return Target(get_watcher(src.kind), src, name or lockfile_name(src))


# --------------------------------------------------------------------------------------
# Fetch / lockfile / history / push
# --------------------------------------------------------------------------------------


def _describe(e: BaseException) -> str:
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(dict.fromkeys(_describe(x) for x in e.exceptions))
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from {e.request.url}"
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _auth_required(msg: str) -> bool:
    needles = ("401", "403", "authentication required", "www-authenticate", "unauthorized")
    return any(n in msg.lower() for n in needles)


def _append_error(store: FileSnapshotStore, t: Target, msg: str) -> None:
    store.dir.mkdir(parents=True, exist_ok=True)
    row = {
        "error": msg,
        "source_id": str(t.source.id),
        "source_kind": t.source.kind,
        "locator": t.source.locator,
        "display_name": t.display,
        "detected_at": _now().isoformat(),
    }
    with (store.dir / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def fetch_doc(t: Target, store: FileSnapshotStore | None = None) -> CanonicalDoc:
    """Fetch the canonical document; a failure is explained on stderr (recorded in
    ``history.jsonl`` when a store is given) and exits with code 3."""
    try:
        return asyncio.run(t.watcher.fetch(t.source))
    except Exception as e:  # noqa: BLE001 - MCPConnectError, httpx errors, bad spec, OSError
        msg = _describe(e)
        if store is not None:
            _append_error(store, t, msg)
        typer.echo(f"kintsua watch: error: could not fetch {t.display}: {msg}", err=True)
        if _auth_required(msg):
            typer.echo(
                "hint: the server requires authentication; pass --auth-env TOKEN_VAR (sent as "
                "'Authorization: Bearer <token>') or --header 'Authorization=Bearer ...'",
                err=True,
            )
        raise typer.Exit(EXIT_FETCH)


def fingerprints_of(doc: CanonicalDoc, kind: str) -> dict[str, str]:
    """MCP: ``{tool: sha256(inputSchema)}``; OpenAPI: ``{"GET /path": sha256(operation)}``."""
    if kind == "mcp":
        from kintsua.watchers.mcp import tool_fingerprint

        tools = doc.body.get("tools") or {}
        return {n: tool_fingerprint(t.get("inputSchema")) for n, t in sorted(tools.items())}
    out: dict[str, str] = {}
    for path, item in sorted((doc.body.get("paths") or {}).items()):
        if isinstance(item, dict):
            for method, op in sorted(item.items()):
                if method.lower() in HTTP_METHODS and isinstance(op, dict):
                    out[f"{method.upper()} {path}"] = canonical_hash(op)
    return out


def write_lockfile(store: FileSnapshotStore, t: Target, doc: CanonicalDoc) -> Path:
    """``<dir>/<name>.lock.json``: the canonical body plus a top-level ``fingerprints`` map."""
    store.save(t.source, doc, t.name)
    path = store.path_for(t.source, t.name)
    data = json.loads(path.read_text("utf-8"))
    data["fingerprints"] = fingerprints_of(doc, t.source.kind)
    data["kintsua_version"] = __version__
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8")
    return path


def push_snapshot(
    t: Target, doc: CanonicalDoc, repo: str | None, key: str | None, url: str
) -> bool:
    """``POST {url}/v1/watch/push`` with the CONTRACTS payload; failures are warnings."""
    if not key:
        warn("--push skipped: pass --key or set KINTSUA_KEY")
        return False
    if not repo:
        warn("--push skipped: pass --repo owner/name or set KINTSUA_REPO")
        return False
    body = {
        "repo": repo,
        "source": {"kind": t.source.kind, "locator": t.source.locator, "display_name": t.display},
        "doc": {"version": doc.version, "body": doc.body},
    }
    headers = {"Authorization": f"Bearer {key}", "User-Agent": f"kintsua-cli/{__version__}"}
    endpoint = f"{url.rstrip('/')}/v1/watch/push"
    try:
        r = httpx.post(endpoint, json=body, headers=headers, timeout=60.0)
    except httpx.HTTPError as e:
        warn(f"push to {endpoint} failed: {_describe(e)}")
        return False
    if r.status_code >= 400:
        warn(f"push to {endpoint} failed: HTTP {r.status_code}: {r.text[:200]}")
        return False
    try:
        data = r.json()
    except ValueError:
        data = {}
    parts = (
        [f"{k}={v}" for k, v in data.items() if isinstance(v, str | int | bool)]
        if isinstance(data, dict)
        else []
    )
    info(
        f"kintsua watch: pushed {t.display} to {url} · {', '.join(parts) or f'HTTP {r.status_code}'}"
    )
    return True


# --------------------------------------------------------------------------------------
# Diff presentation
# --------------------------------------------------------------------------------------


@dataclass
class Row:
    operation: str
    op: str
    path: str
    severity: str
    rule: str
    detail: str


def _short(v: Any, n: int = 48) -> str:
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def _detail(op: ChangeOp) -> str:
    if op.op in ("added", "removed"):
        return ""
    if op.before is None and op.after is None:
        return ""
    return f"{_short(op.before)} -> {_short(op.after)}"


def rows_for(ev: ChangeEvent) -> list[Row]:
    """One row per op, classified on its own so the table shows the rule that fired for it."""
    rows: list[Row] = []
    for op in ev.ops:
        one = ChangeEvent(
            source_id=ev.source_id,
            source_kind=ev.source_kind,
            from_version=ev.from_version,
            to_version=ev.to_version,
            ops=[op],
        )
        c = classify_event(one)
        rule = c.rule_hits[0] if c.rule_hits else ""
        rows.append(
            Row(affected_operation(op.path) or "-", op.op, op.path, c.severity, rule, _detail(op))
        )
    rows.sort(key=lambda r: (SEVERITY_RANK.get(r.severity, 9), r.path))
    return rows


def print_table(
    rows: list[Row], cls: Classification, t: Target, ev: ChangeEvent, store: FileSnapshotStore
) -> None:
    console = _console()
    title = f"kintsua watch: {t.display} {ev.from_version or '?'} -> {ev.to_version or '?'}"
    table = Table(title=title, title_justify="left")
    for col in ("Operation", "Change", "Path", "Severity", "Rule", "Detail"):
        table.add_column(col, overflow="fold")
    for r in rows[:MAX_ROWS]:
        sev = Text(r.severity, style=SEV_STYLE.get(r.severity, ""))
        table.add_row(r.operation, r.op, r.path, sev, r.rule, r.detail)
    console.print(table)
    if len(rows) > MAX_ROWS:
        console.print(f"... {len(rows) - MAX_ROWS} more changes")
    console.print(
        f"{SEV_MARK.get(cls.severity, '')} {cls.severity.upper()} ({cls.category}): {cls.summary}"
    )
    if cls.migration_hint:
        console.print(f"fix: {cls.migration_hint}")
    hist = _rel(store.dir / "history.jsonl")
    console.print(
        f"kintsua watch: {len(ev.ops)} change(s) in {t.display}, recorded in {hist} · {TAGLINE}"
    )


def _gh(s: str, prop: bool = False) -> str:
    s = s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return s.replace(":", "%3A").replace(",", "%2C") if prop else s


def github_annotations(rows: list[Row], cls: Classification, lock_path: Path, *, err: bool) -> None:
    """``::error|warning|notice file=<lockfile>::message`` lines for the Actions log."""
    file = _gh(_rel(lock_path), prop=True)
    title = _gh(f"kintsua watch: {cls.severity}", prop=True)
    typer.echo(
        f"::{GH_LEVEL.get(cls.severity, 'notice')} file={file},title={title}::{_gh(cls.summary)}",
        err=err,
    )
    for r in rows:
        msg = _gh(f"{r.operation}: {r.op} {r.path} [{r.rule or r.severity}]")
        typer.echo(f"::{GH_LEVEL.get(r.severity, 'notice')} file={file}::{msg}", err=err)


def step_summary(
    rows: list[Row], cls: Classification, t: Target, ev: ChangeEvent, lock_path: Path
) -> str:
    cell = lambda s: str(s).replace("|", "\\|")  # noqa: E731
    lines = [
        f"## kintsua watch: {t.display} ({ev.from_version or '?'} -> {ev.to_version or '?'})",
        "",
        f"**{SEV_MARK.get(cls.severity, '')} {cls.severity.upper()}** · {cls.category} · {cell(cls.summary)}",
        "",
    ]
    if cls.migration_hint:
        lines += [f"Fix: {cell(cls.migration_hint)}", ""]
    lines += [
        "| Operation | Change | Path | Severity | Rule | Detail |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows[:MAX_ROWS]:
        lines.append(
            f"| {cell(r.operation)} | {r.op} | `{cell(r.path)}` | {r.severity} | {r.rule} | {cell(r.detail)} |"
        )
    lines += ["", f"Lockfile: `{_rel(lock_path)}` · [Kintsua]({HOSTED})", ""]
    return "\n".join(lines)


def write_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


@app.command()
def snapshot(
    server: ServerOpt = None,
    spec: SpecOpt = None,
    dir: DirOpt = Path(".kintsua"),
    name: NameOpt = None,
    header: HeaderOpt = None,
    env: EnvOpt = None,
    auth_env: AuthEnvOpt = None,
    timeout: TimeoutOpt = 60.0,
    push: PushOpt = False,
    repo: RepoOpt = None,
    key: KeyOpt = None,
    url: UrlOpt = DEFAULT_URL,
) -> None:
    """Fetch the server's tools/list (or an OpenAPI spec) and write <dir>/<name>.lock.json."""
    t = resolve_target(server, spec, header, env, auth_env, name, timeout)
    store = FileSnapshotStore(dir)
    doc = fetch_doc(t, store)
    _refine_display(t, doc)
    path = write_lockfile(store, t, doc)
    n = len(fingerprints_of(doc, t.source.kind))
    info(f"kintsua watch: {n} {t.unit} snapshotted -> {_rel(path)} · {TAGLINE}")
    if push:
        push_snapshot(t, doc, repo, key, url)


@app.command()
def diff(
    server: ServerOpt = None,
    spec: SpecOpt = None,
    dir: DirOpt = Path(".kintsua"),
    name: NameOpt = None,
    header: HeaderOpt = None,
    env: EnvOpt = None,
    auth_env: AuthEnvOpt = None,
    timeout: TimeoutOpt = 60.0,
    fail_on: Annotated[
        FailOn, typer.Option("--fail-on", help="Exit 1 at this severity or worse.")
    ] = FailOn.breaking,
    github_action: Annotated[
        bool, typer.Option("--github-action", help="Emit ::error/::warning annotations.")
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", help="Print the event + classification as JSON.")
    ] = False,
    update: Annotated[
        bool, typer.Option("--update", help="Rewrite the lockfile after diffing.")
    ] = False,
    push: PushOpt = False,
    repo: RepoOpt = None,
    key: KeyOpt = None,
    url: UrlOpt = DEFAULT_URL,
) -> None:
    """Fetch, compare with the lockfile, classify with the rules; exit 1 on breaking changes."""
    t = resolve_target(server, spec, header, env, auth_env, name, timeout)
    store = FileSnapshotStore(dir)
    lock_path = store.path_for(t.source, t.name)
    prev = store.latest(t.source.id, name=t.name)
    if prev is None:
        what = f"--spec {spec}" if spec else f"--server {server!r}"
        fail(
            f"no lockfile at {_rel(lock_path)}; run: kintsua watch snapshot {what} --dir {dir}",
            EXIT_USAGE,
        )
    doc = fetch_doc(t, store)
    _refine_display(t, doc)
    event_text = getattr(t.watcher, "event_text", lambda b, a: None)
    ev = compare(t.watcher, t.source, prev, doc, text=event_text(prev.as_doc(), doc))
    source_json = {"kind": t.source.kind, "locator": t.source.locator, "display_name": t.display}
    if ev is None:
        n = len(fingerprints_of(doc, t.source.kind))
        if json_out:
            info(
                json.dumps(
                    {
                        "changed": False,
                        "source": source_json,
                        "lockfile": _rel(lock_path),
                        "exit_code": 0,
                    }
                )
            )
        else:
            info(f"kintsua watch: no changes in {t.display} ({n} {t.unit}) · {TAGLINE}")
        if github_action:
            typer.echo(
                f"::notice file={_gh(_rel(lock_path), True)}::kintsua watch: no changes in {_gh(t.display)}",
                err=json_out,
            )
        if push:
            push_snapshot(t, doc, repo, key, url)
        return
    cls = classify_event(ev, display_name=t.display)
    rows = rows_for(ev)
    code = EXIT_GATE if SEVERITY_RANK[cls.severity] <= SEVERITY_RANK[fail_on.value] else 0
    store.append_history(
        ev,
        extra={
            "classification": cls.model_dump(),
            "detected_at": _now().isoformat(),
            "display_name": t.display,
            "locator": t.source.locator,
            "lockfile": lock_path.name,
            "exit_code": code,
        },
    )
    if json_out:
        payload = {
            "changed": True,
            "source": source_json,
            "lockfile": _rel(lock_path),
            "event": ev.model_dump(mode="json"),
            "classification": cls.model_dump(),
            "rows": [asdict(r) for r in rows],
            "fail_on": fail_on.value,
            "exit_code": code,
        }
        info(json.dumps(payload, indent=2, default=str))
    else:
        print_table(rows, cls, t, ev, store)
    if github_action:
        github_annotations(rows, cls, lock_path, err=json_out)
        write_step_summary(step_summary(rows, cls, t, ev, lock_path))
    if update:
        write_lockfile(store, t, doc)
        if not json_out:
            info(f"kintsua watch: lockfile updated -> {_rel(lock_path)}")
    if push:
        push_snapshot(t, doc, repo, key, url)
    if code:
        raise typer.Exit(code)


def _parse_dt(value: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def build_report(
    store: FileSnapshotStore, window: timedelta, now: datetime | None = None
) -> WeeklyReport:
    """A ``WeeklyReport`` from ``history.jsonl``: one item per change entry in the window, failing
    sources from ``error`` entries (cleared by a later successful diff), stats counted locally."""
    until = now or _now()
    since = until - window
    items: list[ReportItem] = []
    failing: dict[str, FailingSource] = {}
    all_locators: set[str] = set()
    seen: set[str] = set()
    rows = sorted(store.history(), key=lambda r: _parse_dt(r.get("detected_at")) or until)
    for row in rows:
        locator = str(row.get("locator") or row.get("source_id") or "?")
        all_locators.add(locator)
        detected = _parse_dt(row.get("detected_at")) or until
        if detected < since or detected > until:
            continue
        seen.add(locator)
        name = row.get("display_name") or locator
        kind = row.get("source_kind") or "mcp"
        if "error" in row:
            f = failing.get(locator) or FailingSource(display_name=name, kind=kind, locator=locator)
            f.last_error, f.consecutive_failures = str(row["error"]), f.consecutive_failures + 1
            f.failing_since = min(f.failing_since or detected, detected)
            failing[locator] = f
            continue
        failing.pop(locator, None)
        c = row.get("classification") or {}
        ops = row.get("ops") or []
        sev = c.get("severity") or "unknown"
        items.append(
            ReportItem(
                source_name=name,
                source_kind=kind,
                severity=sev,
                category=c.get("category") or "behaviour",
                summary=c.get("summary") or f"{len(ops)} change(s)",
                detected_at=detected,
                affected_paths=list(c.get("affected_paths") or []),
                from_version=row.get("from_version"),
                to_version=row.get("to_version"),
                migration_hint=c.get("migration_hint"),
                needs_human=sev == "unknown",
                rule_hits=list(c.get("rule_hits") or []),
                op_count=len(ops),
            )
        )
    items.sort(key=lambda i: i.detected_at, reverse=True)
    counts = Counter(i.severity for i in items)
    stats = ReportStats(
        sources_watched=len(all_locators),
        sources_polled=len(seen),
        change_events=len(items),
        breaking=counts["breaking"],
        degraded=counts["degraded"],
        safe=counts["safe"],
        unknown=counts["unknown"],
        needs_human=sum(1 for i in items if i.needs_human),
    )
    note = (
        f"Built from {_rel(store.dir / 'history.jsonl')} by kintsua watch {__version__}; {TAGLINE}."
    )
    return WeeklyReport(
        title="Kintsua watch report",
        since=since,
        until=until,
        generated_at=until,
        items=items,
        failing_sources=list(failing.values()),
        stats=stats,
        notes=[note],
    )


@app.command()
def report(
    since: Annotated[str, typer.Option("--since", help="Window, e.g. 7d, 24h, 2w.")] = "7d",
    dir: DirOpt = Path(".kintsua"),
    json_out: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
    include_safe: Annotated[
        bool, typer.Option("--include-safe/--no-safe", help="List safe changes too.")
    ] = True,
) -> None:
    """Render the Markdown drift report from <dir>/history.jsonl for the last --since window."""
    try:
        window = parse_duration(since)
    except ValueError as e:
        fail(str(e), EXIT_USAGE)
        return
    rep = build_report(FileSnapshotStore(dir), window)
    info(
        json.dumps(to_dict(rep), indent=2)
        if json_out
        else render_markdown(rep, include_safe=include_safe)
    )
