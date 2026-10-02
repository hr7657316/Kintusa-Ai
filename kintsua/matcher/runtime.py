"""Runtime events -> dependencies, promoted change events and error-spike evidence.

``match_runtime`` (job payload ``{"repo_id"}``, deduped per repo by ``/v1/ingest``) looks at
the repo's ``runtime_event`` rows of the last ``Settings.runtime_promotion_window_s`` (24 h)
and does three idempotent things, so running it twice on the same events changes nothing:

(a) **Discovery.** Every tool or host the indexer never saw gets a ``dependency`` (kind
    ``mcp``, named after the server the sample carries or else the tool name; kind
    ``http`` named after the host for ``"GET api.stripe.com/v1/charges"`` tool names) and a
    ``call_site`` (``file_path="<runtime>"``, ``line=0``, ``symbol=tool_name``, normalised
    operation) flagged ``discovered_at_runtime``. "Never saw" is judged per dependency: an
    HTTP call counts as known only through a call site of a dependency for the same host
    (or an SDK package of the vendor that owns the host), an MCP tool only through a call
    site of an ``mcp`` dependency (of the same server when the sample names one).
(b) **Promotion.** A tool with at least ``runtime_promotion_threshold`` ``schema_mismatch``
    events in the window and no documented change event in the window for any source this
    repo's dependencies for the tool point to gets a synthetic change event (source kind
    ``observed``, severity ``breaking``, category ``behaviour``, rule ``runtime_promotion``)
    and an impact, then ``open_issue`` under the usual auto-issue gate. ``.kintsua.yml``
    mutes apply as in ``match.py``. The same ``(source, to_version)`` pair is never promoted
    twice.
(c) **Error spikes.** Open impacts (issued / fixing / pr_open) whose matched call sites
    cover the failing operation on the same host / server get ``runtime_error_count`` =
    failed calls in the last hour and one issue comment (``<!-- kintsua:runtime -->``) that
    is created once and edited after.

Trust (docs/CONTRACTS.md, "Source trust"): runtime samples are customer data. Discovery
creates a ``source`` only for an http(s) server URL that passes
``netguard.check_url(url, resolve=False)`` (no localhost, private, link-local or metadata
host literals, no credentials), with ``origin="runtime"``; a command line, an SDK label
such as ``mcp:<serverInfo.name>`` or a blocked URL is recorded on the dependency and never
becomes a source. HTTP hosts map to the curated ``sdk_map`` sources (``origin="seed"``) or
to a source this org (or the seed) already uses for the host. Promoted events always hang
off the repo's own inactive ``observed:<repo>:<tool>`` source (``origin="observed"``), so
they are private to the repo and never shared between tenants.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kintsua.core.config import get_settings
from kintsua.core.events import ChangeEvent, ChangeOp, canonical_hash, canonical_json
from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Classification,
    Dependency,
    Impact,
    Org,
    Repo,
    RuntimeEvent,
    Source,
)
from kintsua.core.netguard import check_url, is_http_locator
from kintsua.core.queue import JOB_MATCH_RUNTIME, NonRetryable, job_handler
from kintsua.core.util import short_hash, utcnow
from kintsua.matcher.issues import (
    RUNTIME_COMMENT_MARKER,
    RUNTIME_FILE_PATH,
    plain,
    upsert_impact_comment,
)
from kintsua.matcher.match import (
    PACKAGE_KINDS,
    after_match,
    dependency_vendor,
    ensure_impact,
    site_record,
)
from kintsua.matcher.mute import clean_patterns, is_muted
from kintsua.matcher.operations import (
    HTTP_METHODS,
    normalize_operation,
    operations_match,
)

log = logging.getLogger(__name__)

RULE_RUNTIME_PROMOTION = "runtime_promotion"
ERROR_SPIKE_WINDOW_S = 3600
MAX_EVENTS_PER_RUN = 50_000
OPEN_ISSUE_STATUSES: tuple[str, ...] = ("issued", "fixing", "pr_open")
RUNTIME_ORIGIN = "runtime"  # sources discovered from runtime samples (http(s) only)
OBSERVED_ORIGIN = "observed"  # the synthetic per-repo source of promoted events
SEED_ORIGIN = "seed"  # curated sdk_map spec / changelog URLs
MAX_DEPENDENCY_NAME = 512  # Dependency.name is varchar(512)
MAX_SERVER_URL = 2048  # a longer runtime server URL is recorded, never watched
_SERVER_LABEL_PREFIXES = ("mcp:", "langchain:")  # SDK labels for servers without a URL
_HTTP_TOOL_RE = re.compile(r"^([A-Za-z]+)\s+(\S+)$")
_FINGERPRINT_KEYS = (
    "previous_fingerprint",
    "from_fingerprint",
    "before_fingerprint",
    "old_fingerprint",
)


@dataclass
class RuntimeMatchResult:
    repo_id: str
    events_seen: int = 0
    dependencies_created: int = 0
    call_sites_created: int = 0
    promoted: list[str] = field(default_factory=list)  # change_event ids
    impacts: list[str] = field(default_factory=list)
    error_spikes: dict[str, int] = field(default_factory=dict)  # impact id -> count

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "events_seen": self.events_seen,
            "dependencies_created": self.dependencies_created,
            "call_sites_created": self.call_sites_created,
            "promoted": list(self.promoted),
            "impacts": list(self.impacts),
            "error_spikes": dict(self.error_spikes),
        }


# --------------------------------------------------------------------------------------
# Event shapes
# --------------------------------------------------------------------------------------


def runtime_kind(event: RuntimeEvent) -> str:
    """``"http"`` or ``"mcp"``: from ``sample.kind`` when the ingest stored it, else from
    the tool name shape (``"GET api.host/path"`` is HTTP)."""
    sample = event.sample if isinstance(event.sample, dict) else {}
    kind = sample.get("kind")
    if kind in ("http", "mcp"):
        return str(kind)
    m = _HTTP_TOOL_RE.match(event.tool_name or "")
    if m and m.group(1).upper() in HTTP_METHODS:
        return "http"
    return "mcp"


def http_host(tool_name: str) -> str | None:
    m = _HTTP_TOOL_RE.match(tool_name or "")
    if not m:
        return None
    target = m.group(2)
    if "://" in target:
        target = target.split("://", 1)[1]
    host = target.split("/", 1)[0].split("?", 1)[0]
    return host.lower() or None


def runtime_operation(event: RuntimeEvent) -> str:
    """The call-site operation for a runtime event: canonical ``"GET /v1/charges/{id}"``
    for HTTP, the bare tool name for MCP (what the indexer records too)."""
    if runtime_kind(event) == "http":
        return normalize_operation(event.tool_name)
    return (event.tool_name or "").strip()


def sample_server(event: RuntimeEvent) -> str | None:
    sample = event.sample if isinstance(event.sample, dict) else {}
    server = sample.get("server") or sample.get("server_locator")
    return str(server).strip() if isinstance(server, str) and server.strip() else None


def _arg_keys(event: RuntimeEvent) -> list[str]:
    sample = event.sample if isinstance(event.sample, dict) else {}
    keys = sample.get("arg_keys")
    if isinstance(keys, list):
        return sorted({str(k) for k in keys if isinstance(k, str | int)})[:50]
    return []


def recent_events(session: Session, repo: Repo, since: datetime) -> list[RuntimeEvent]:
    return list(
        session.scalars(
            select(RuntimeEvent)
            .where(RuntimeEvent.repo_id == repo.id, RuntimeEvent.observed_at >= since)
            .order_by(RuntimeEvent.observed_at.desc())
            .limit(MAX_EVENTS_PER_RUN)
        )
    )


# --------------------------------------------------------------------------------------
# (a) discovery
# --------------------------------------------------------------------------------------


def _repo_sites(session: Session, repo: Repo) -> list[tuple[CallSite, Dependency]]:
    rows = session.execute(
        select(CallSite, Dependency)
        .join(Dependency, Dependency.id == CallSite.dependency_id)
        .where(Dependency.repo_id == repo.id)
    ).all()
    return [(cs, dep) for cs, dep in rows]


# ---- which dependency a runtime event belongs to ---------------------------------------


@dataclass(frozen=True)
class RuntimeScope:
    """Where a runtime call went: ``kind`` ``http`` with the ``host`` of the tool name, or
    ``mcp`` with the ``server`` the sample names (a URL, an SDK label, or None)."""

    kind: str
    host: str | None = None
    server: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.host or server_key(self.server))


def event_scope(event: RuntimeEvent) -> RuntimeScope:
    kind = runtime_kind(event)
    if kind == "http":
        return RuntimeScope("http", host=http_host(event.tool_name or ""))
    return RuntimeScope("mcp", server=sample_server(event))


def server_key(value: str | None) -> str:
    """Comparable identity of an MCP server: an http(s) URL with scheme and host lowercased
    and no trailing slash, or a label without its SDK prefix (``mcp:weather`` ->
    ``weather``), lowercased."""
    v = (value or "").strip()
    if is_http_locator(v):
        scheme, _, rest = v.rstrip("/").partition("://")
        host, sep, path = rest.partition("/")
        return f"{scheme.lower()}://{host.lower()}{sep}{path}"
    low = v.lower()
    for prefix in _SERVER_LABEL_PREFIXES:
        if low.startswith(prefix):
            return low[len(prefix) :].strip()
    return low


def _dep_server_keys(dep: Dependency) -> set[str]:
    meta = dep.meta or {}
    values = (dep.name, meta.get("server_name"), meta.get("pushed_locator"), meta.get("server"))
    return {server_key(v) for v in values if isinstance(v, str) and v.strip()}


@lru_cache(maxsize=4096)
def _host_vendor(host: str) -> str | None:
    """The curated vendor slug owning ``host`` (cached: called per call site per event)."""
    try:
        from kintsua.indexer import sdk_map  # optional seam: pure data, no DB
    except ImportError:  # pragma: no cover - components missing in a partial checkout
        return None
    entry = sdk_map.vendor_for_host(host)
    return entry.vendor if entry is not None else None


def _dep_serves_host(dep: Dependency, host: str | None) -> bool:
    """Does ``dep`` stand for calls to ``host``: an ``http`` dependency on that host, a
    configured spec / changelog that lists it, or an SDK package of the vendor owning it."""
    if not host:
        return False
    meta = dep.meta or {}
    if dep.kind == "http":
        return (dep.name or "").lower() == host or str(meta.get("host") or "").lower() == host
    if dep.kind in ("openapi", "changelog"):
        hosts = meta.get("hosts")
        return isinstance(hosts, list) and host in {str(h).lower() for h in hosts}
    if dep.kind in PACKAGE_KINDS:
        vendor = _host_vendor(host)
        return vendor is not None and dependency_vendor(dep, None) == vendor
    return False


def scoped_pairs(pairs: Iterable[tuple[Any, Dependency]], scope: RuntimeScope) -> list[Any]:
    """The ``(site, dependency)`` pairs a runtime event of ``scope`` may refer to: for HTTP
    the dependencies serving its host; for MCP the ``mcp`` dependencies, narrowed to the
    sample's server when one of them is that server. A server URL that no dependency has
    matches nothing (it is a server the indexer never saw); an SDK label that no dependency
    carries (``mcp:<serverInfo.name>`` vs a config entry name) falls back to every ``mcp``
    dependency, matched by tool name."""
    if scope.kind == "http":
        return [(s, dep) for s, dep in pairs if _dep_serves_host(dep, scope.host)]
    mcp = [(s, dep) for s, dep in pairs if dep.kind == "mcp"]
    if not scope.server:
        return mcp
    key = server_key(scope.server)
    same = [(s, dep) for s, dep in mcp if key in _dep_server_keys(dep)]
    if same or is_http_locator(scope.server):
        return same
    return mcp


def _covered(
    operation: str, sites: Iterable[tuple[CallSite, Dependency]], scope: RuntimeScope
) -> bool:
    canonical = normalize_operation(operation)
    for cs, _dep in scoped_pairs(sites, scope):
        if normalize_operation(cs.operation) == canonical or operations_match(
            cs.operation, operation
        ):
            return True
    return False


# ---- sources and rows ------------------------------------------------------------------


def watchable_server_url(server: str | None) -> str | None:
    """``server`` when the hosted watcher may watch it: an http(s) URL of at most
    ``MAX_SERVER_URL`` characters that passes ``netguard.check_url(..., resolve=False)``
    (public host literal, no credentials). None for everything else: command lines, SDK
    labels, ``localhost`` / private / link-local / metadata addresses."""
    s = (server or "").strip()
    if not s or len(s) > MAX_SERVER_URL or not is_http_locator(s):
        return None
    if any(ch.isspace() for ch in s):
        return None
    try:
        check_url(s, resolve=False)
    except ValueError:  # BlockedURL, or a URL urlsplit cannot parse
        return None
    return s


def _unwatched_reason(server: str) -> str:
    if not is_http_locator(server):
        return "not an http(s) URL; runtime data never becomes a command the worker runs"
    return "not a public http(s) URL"


def dependency_name(name: str) -> str:
    """``dependency.name`` for a runtime name: as is, or cut with a hash suffix when it does
    not fit the column (tool names and server URLs from the SDK can be long)."""
    if len(name) <= MAX_DEPENDENCY_NAME:
        return name
    suffix = "#" + short_hash(name, 16)
    return name[: MAX_DEPENDENCY_NAME - len(suffix)] + suffix


def _source_for_host(
    session: Session, repo: Repo, host: str
) -> tuple[Source | None, str | None, str | None]:
    """``(source, vendor, display_name)`` for a runtime-discovered HTTP host: the curated SDK
    map when it is importable (the vendor's spec, else its changelog; ``origin="seed"``),
    otherwise a source a dependency on the same host already has, if it is seeded or used
    by this org (never another org's private or pushed source)."""
    vendor: str | None = None
    display: str | None = None
    try:
        from kintsua.indexer import sdk_map  # optional seam: pure data, no DB
        from kintsua.watchers.run import find_or_create_source
    except ImportError:  # pragma: no cover - components missing in a partial checkout
        sdk_map = None  # type: ignore[assignment]
        find_or_create_source = None  # type: ignore[assignment]
    if sdk_map is not None and find_or_create_source is not None:
        entry = sdk_map.vendor_for_host(host)
        if entry is not None:
            vendor, display = entry.vendor, entry.display_name
        spec = sdk_map.spec_for_host(host)
        if spec is not None and spec.openapi_url:
            meta = {"format": spec.openapi_format} if spec.openapi_format else {}
            src = find_or_create_source(
                session,
                "openapi",
                spec.openapi_url,
                display_name=f"{spec.display_name} API",
                vendor=spec.vendor,
                meta=meta,
                origin=SEED_ORIGIN,
            )
            return src, vendor, display
        if entry is not None and entry.changelog_url:
            src = find_or_create_source(
                session,
                "changelog",
                entry.changelog_url,
                display_name=f"{entry.display_name} changelog",
                vendor=entry.vendor,
                origin=SEED_ORIGIN,
            )
            return src, vendor, display
    found = session.execute(
        select(Dependency, Source)
        .join(Source, Source.id == Dependency.source_id)
        .join(Repo, Repo.id == Dependency.repo_id)
        .where(
            Dependency.kind == "http",
            Dependency.name == host,
            Source.origin.not_in(("push", OBSERVED_ORIGIN)),
            or_(Source.origin == SEED_ORIGIN, Repo.org_id == repo.org_id),
        )
        .order_by(Dependency.created_at)
        .limit(1)
    ).first()
    if found is not None:
        other, src = found
        return src, vendor or (other.meta or {}).get("vendor"), display
    return None, vendor, display


def _source_for_server(session: Session, url: str) -> Source | None:
    """The ``mcp`` source for a runtime server URL that ``watchable_server_url`` accepted:
    created with ``origin="runtime"`` (an existing row, e.g. a seeded one, keeps its
    origin). Never called with anything but an http(s) URL."""
    try:
        from kintsua.watchers.run import find_or_create_source
    except ImportError:  # pragma: no cover
        return session.scalar(select(Source).where(Source.kind == "mcp", Source.locator == url))
    meta: dict[str, Any] = {"transport": "http", "discovered_at_runtime": True}
    return find_or_create_source(
        session, "mcp", url, display_name=url, meta=meta, origin=RUNTIME_ORIGIN
    )


def _get_or_create_dependency(
    session: Session,
    repo: Repo,
    kind: str,
    name: str,
    *,
    source: Source | None,
    meta: dict[str, Any],
) -> tuple[Dependency, bool]:
    name = dependency_name(name)
    existing = session.scalar(
        select(Dependency).where(
            Dependency.repo_id == repo.id, Dependency.kind == kind, Dependency.name == name
        )
    )
    if existing is not None:
        if existing.source_id is None and source is not None:
            existing.source_id = source.id
        return existing, False
    dep = Dependency(
        repo_id=repo.id,
        source_id=source.id if source is not None else None,
        kind=kind,
        name=name,
        discovered_at_runtime=True,
        meta=meta,
    )
    try:
        with session.begin_nested():
            session.add(dep)
            session.flush()
    except IntegrityError:
        winner = session.scalar(
            select(Dependency).where(
                Dependency.repo_id == repo.id, Dependency.kind == kind, Dependency.name == name
            )
        )
        if winner is None:  # pragma: no cover
            raise
        return winner, False
    return dep, True


def _ensure_runtime_site(
    session: Session, dep: Dependency, operation: str, symbol: str, keys: list[str]
) -> tuple[CallSite, bool]:
    canonical = normalize_operation(operation)
    for cs in session.scalars(select(CallSite).where(CallSite.dependency_id == dep.id)):
        if normalize_operation(cs.operation) == canonical:
            return cs, False
    cs = CallSite(
        dependency_id=dep.id,
        file_path=RUNTIME_FILE_PATH,
        line=0,
        symbol=symbol,
        operation=operation,
        args_shape={"keys": keys} if keys else {},
        discovered_at_runtime=True,
    )
    session.add(cs)
    session.flush()
    return cs, True


def _server_dependencies(session: Session, repo: Repo, server: str) -> list[Dependency]:
    """The repo's ``mcp`` dependencies that are ``server`` (by locator, config name or
    pushed locator), the indexer's before runtime-discovered ones."""
    key = server_key(server)
    deps = session.scalars(
        select(Dependency)
        .where(Dependency.repo_id == repo.id, Dependency.kind == "mcp")
        .order_by(Dependency.discovered_at_runtime, Dependency.created_at)
    )
    return [d for d in deps if key in _dep_server_keys(d)]


def _discover_one(
    session: Session,
    repo: Repo,
    ev: RuntimeEvent,
    scope: RuntimeScope,
    operation: str,
    *,
    now: datetime,
) -> tuple[Dependency, bool, CallSite, bool] | None:
    tool = (ev.tool_name or "").strip()
    meta: dict[str, Any] = {
        "discovered_at_runtime": True,
        "runtime_first_seen": now.isoformat(),
        "runtime_tool_kind": scope.kind,
    }
    if scope.kind == "http":
        if not scope.host:
            return None
        source, vendor, display = _source_for_host(session, repo, scope.host)
        meta["host"] = scope.host
        if vendor:
            meta["vendor"] = vendor
        if display:
            meta["display_name"] = display
        dep, created = _get_or_create_dependency(
            session, repo, "http", scope.host, source=source, meta=meta
        )
    else:
        server = scope.server
        known = _server_dependencies(session, repo, server) if server else []
        if known:
            dep, created = known[0], False  # a server this repo already knows: a new tool
        else:
            url = watchable_server_url(server)
            source = _source_for_server(session, url) if url else None
            if server:
                meta["server_name"] = server
                if url:
                    meta["transport"] = "http"
                else:
                    meta["server_unwatched"] = _unwatched_reason(server)
            dep, created = _get_or_create_dependency(
                session, repo, "mcp", server or tool, source=source, meta=meta
            )
    cs, cs_created = _ensure_runtime_site(session, dep, operation, tool, _arg_keys(ev))
    return dep, created, cs, cs_created


def discover_dependencies(
    session: Session, repo: Repo, events: list[RuntimeEvent], *, now: datetime
) -> tuple[int, int]:
    """(a): ``(dependencies_created, call_sites_created)``. Each event's rows are written in
    a savepoint: one malformed event is logged and skipped, it never stops the run."""
    sites = _repo_sites(session, repo)
    deps_created = sites_created = 0
    seen: set[tuple[str, str, str]] = set()
    for ev in events:
        if not (ev.tool_name or "").strip():
            continue
        try:
            operation = runtime_operation(ev)
            scope = event_scope(ev)
            key = (*scope.key, normalize_operation(operation))
            if key in seen:
                continue
            seen.add(key)
            if _covered(operation, sites, scope):
                continue
            with session.begin_nested():
                out = _discover_one(session, repo, ev, scope, operation, now=now)
        except Exception as e:  # noqa: BLE001 - one bad event must not disable the repo's run
            log.warning(
                "runtime: skipping event %s (%r) on %s: %s",
                ev.id,
                (ev.tool_name or "")[:120],
                repo.full_name,
                e,
            )
            continue
        if out is None:
            continue
        dep, created, cs, cs_created = out
        if created:
            deps_created += 1
            log.info(
                "runtime: %s depends on %s %s (discovered at runtime)",
                repo.full_name,
                dep.kind,
                dep.name,
            )
        if cs_created:
            sites_created += 1
            sites.append((cs, dep))
    session.flush()
    return deps_created, sites_created


# --------------------------------------------------------------------------------------
# (b) promotion
# --------------------------------------------------------------------------------------


def _sites_for_tool(
    sites: list[tuple[CallSite, Dependency]], operation: str, scope: RuntimeScope
) -> list[tuple[CallSite, Dependency]]:
    """The call sites a runtime operation refers to: same host / server (``scoped_pairs``)
    and matching operation."""
    out = [
        (cs, dep)
        for cs, dep in scoped_pairs(sites, scope)
        if operations_match(cs.operation, operation)
    ]
    out.sort(key=lambda p: (not p[0].discovered_at_runtime, p[0].file_path, p[0].line))
    return out


def _source_ids_of(deps: Iterable[Dependency]) -> set[UUID]:
    ids: set[UUID] = set()
    for dep in deps:
        if dep.source_id is not None:
            ids.add(dep.source_id)
        for key in ("openapi_source_id", "changelog_source_id"):
            raw = (dep.meta or {}).get(key)
            if raw:
                with contextlib.suppress(ValueError):
                    ids.add(UUID(str(raw)))
    return ids


def _documented_change_exists(session: Session, source_ids: set[UUID], since: datetime) -> bool:
    """A real (watcher / push) change event in the window on one of ``source_ids``; earlier
    runtime promotions do not count (they are not documentation of anything)."""
    if not source_ids:
        return False
    row = session.scalar(
        select(ChangeEventRow.id)
        .where(
            ChangeEventRow.source_id.in_(list(source_ids)),
            ChangeEventRow.detected_at >= since,
            ChangeEventRow.diff["source_kind"].astext.is_distinct_from("observed"),
        )
        .limit(1)
    )
    return row is not None


def observed_locator(repo: Repo, tool_name: str) -> str:
    return f"observed:{repo.full_name}:{tool_name}"


def observed_source(session: Session, repo: Repo, tool_name: str, server: str | None) -> Source:
    """The source a promoted event hangs off: always the repo's own synthetic inactive
    ``observed:<repo>:<tool>`` source (``origin="observed"``: never polled, never public).
    Runtime evidence is one customer's private data, so it never lands on a shared server
    source where other tenants' matching or the public report would see it. The server the
    samples named, if any, is kept in ``meta.server``."""
    locator = observed_locator(repo, tool_name)
    src = session.scalar(select(Source).where(Source.kind == "mcp", Source.locator == locator))
    if src is not None:
        if server and not (src.meta or {}).get("server"):
            src.meta = {**(src.meta or {}), "server": server}
        return src
    meta: dict[str, Any] = {"observed": True, "repo": repo.full_name, "tool": tool_name}
    if server:
        meta["server"] = server
    src = Source(
        kind="mcp",
        locator=locator,
        display_name=tool_name,
        active=False,
        origin=OBSERVED_ORIGIN,
        meta=meta,
    )
    try:
        with session.begin_nested():
            session.add(src)
            session.flush()
    except IntegrityError:
        found = session.scalar(
            select(Source).where(Source.kind == "mcp", Source.locator == locator)
        )
        if found is None:  # pragma: no cover
            raise
        return found
    return src


def _ops_from_samples(tool_name: str, events: list[RuntimeEvent]) -> list[ChangeOp]:
    prefix = f"/tools/{tool_name.replace('~', '~0').replace('/', '~1')}/inputSchema"
    ops: list[ChangeOp] = []
    seen: set[str] = set()
    for ev in events:
        sample = ev.sample if isinstance(ev.sample, dict) else {}
        diff = sample.get("diff")
        if not isinstance(diff, list):
            continue
        for raw in diff:
            if not isinstance(raw, dict):
                continue
            data = dict(raw)
            path = str(data.get("path") or "")
            if not path.startswith("/tools/"):
                data["path"] = prefix + (
                    path if path.startswith("/") else ("/" + path if path else "")
                )
            try:
                op = ChangeOp.model_validate(data)
            except Exception:  # noqa: BLE001 - a malformed op from the SDK is skipped
                continue
            key = canonical_json(op.model_dump(mode="json"))
            if key in seen:
                continue
            seen.add(key)
            ops.append(op)
    return ops


def _fingerprints(
    session: Session, repo: Repo, tool_name: str, events: list[RuntimeEvent]
) -> tuple[str | None, str]:
    """``(from, to)`` schema fingerprints for a mismatch cluster."""
    ordered = sorted(events, key=lambda e: e.observed_at)
    to_fp = next((e.schema_fingerprint for e in reversed(ordered) if e.schema_fingerprint), None)
    from_fp: str | None = None
    for ev in ordered:
        sample = ev.sample if isinstance(ev.sample, dict) else {}
        for key in _FINGERPRINT_KEYS:
            val = sample.get(key)
            if isinstance(val, str) and val and val != to_fp:
                from_fp = val
                break
        if from_fp:
            break
    if from_fp is None:
        prior = session.scalar(
            select(RuntimeEvent.schema_fingerprint)
            .where(
                RuntimeEvent.repo_id == repo.id,
                RuntimeEvent.tool_name == tool_name,
                RuntimeEvent.outcome == "ok",
                RuntimeEvent.schema_fingerprint.is_not(None),
                RuntimeEvent.observed_at < ordered[0].observed_at,
            )
            .order_by(RuntimeEvent.observed_at.desc())
            .limit(1)
        )
        if prior and prior != to_fp:
            from_fp = str(prior)
    if to_fp is None:
        ops = _ops_from_samples(tool_name, events)
        to_fp = canonical_hash([o.model_dump(mode="json") for o in ops] or [tool_name])[:16]
    return from_fp, str(to_fp)


def synthesize_observed_event(
    session: Session, repo: Repo, tool_name: str, mismatch_events: list[RuntimeEvent]
) -> tuple[ChangeEventRow, Classification]:
    """Write the synthetic change event and its classification for a cluster of
    ``schema_mismatch`` events (see the module docstring). Flushes, does not commit."""
    if not mismatch_events:
        raise ValueError("synthesize_observed_event needs at least one mismatch event")
    settings = get_settings()
    server = next((sample_server(e) for e in mismatch_events if sample_server(e)), None)
    source = observed_source(session, repo, tool_name, server)
    from_fp, to_fp = _fingerprints(session, repo, tool_name, mismatch_events)
    ops = _ops_from_samples(tool_name, mismatch_events)
    if not ops:
        ops = [
            ChangeOp(
                op="text_changed",
                path=f"/tools/{tool_name.replace('~', '~0').replace('/', '~1')}/inputSchema",
                before=from_fp,
                after=to_fp,
            )
        ]
    hours = max(1, int(settings.runtime_promotion_window_s // 3600))
    n = len(mismatch_events)
    event = ChangeEvent(
        source_id=source.id,
        source_kind="observed",
        from_version=from_fp,
        to_version=to_fp,
        ops=ops,
        raw_diff_ref="inline",
        text=f"{n} schema_mismatch runtime events for tool {tool_name} reported by the Kintsua SDK",
    )
    now = utcnow()
    diff = event.model_dump(mode="json")
    # private runtime evidence of one repo: never on the public report, never shared
    diff["private"] = True
    diff["repo_id"] = str(repo.id)
    if server:
        diff["server"] = server
    row = ChangeEventRow(source_id=source.id, diff=diff, detected_at=now)
    session.add(row)
    session.flush()
    classification = Classification(
        change_event_id=row.id,
        severity="breaking",
        category="behaviour",
        summary=(
            f"Tool {tool_name} answers with a different schema than yesterday "
            f"({n} mismatches in {hours}h, undocumented upstream change)"
        ),
        affected_paths=[f"tool:{tool_name}"],
        migration_hint=None,
        rule_hits=[RULE_RUNTIME_PROMOTION],
        needs_human=False,
    )
    session.add(classification)
    session.flush()
    log.info(
        "runtime: promoted %s mismatches for %s on %s into change_event %s",
        n,
        tool_name,
        repo.full_name,
        row.id,
    )
    return row, classification


def _existing_promotion(session: Session, source: Source, to_fp: str) -> ChangeEventRow | None:
    return session.scalar(
        select(ChangeEventRow)
        .where(
            ChangeEventRow.source_id == source.id,
            ChangeEventRow.diff["to_version"].astext == to_fp,
            ChangeEventRow.diff["source_kind"].astext == "observed",
        )
        .order_by(ChangeEventRow.detected_at.desc())
        .limit(1)
    )


def promote_mismatches(
    session: Session,
    repo: Repo,
    events: list[RuntimeEvent],
    *,
    now: datetime,
    threshold: int,
    window_s: int,
) -> tuple[list[ChangeEventRow], list[Impact]]:
    """(b): returns ``(promoted change events, impacts touched)``."""
    since = now - timedelta(seconds=window_s)
    clusters: dict[str, list[RuntimeEvent]] = defaultdict(list)
    for ev in events:
        if (
            ev.outcome == "schema_mismatch"
            and ev.observed_at >= since
            and (ev.tool_name or "").strip()
        ):
            clusters[ev.tool_name.strip()].append(ev)
    if not clusters:
        return [], []
    org = session.get(Org, repo.org_id)
    sites = _repo_sites(session, repo)
    mutes = clean_patterns((repo.config or {}).get("mute"))
    promoted: list[ChangeEventRow] = []
    impacts: list[Impact] = []
    for tool, cluster in sorted(clusters.items()):
        if len(cluster) < threshold:
            continue
        server = next((sample_server(e) for e in cluster if sample_server(e)), None)
        scope = event_scope(cluster[0])
        if scope.kind == "mcp":
            scope = RuntimeScope("mcp", server=server)
        operation = runtime_operation(cluster[0])
        all_sites = _sites_for_tool(sites, operation, scope)
        tool_sites = [
            (cs, dep)
            for cs, dep in all_sites
            if not is_muted(
                mutes, dependency_vendor(dep, None), cs.operation, tool=dep.kind == "mcp"
            )
        ]
        if is_muted(mutes, None, operation, tool=scope.kind == "mcp") or (
            all_sites and not tool_sites
        ):
            log.info("runtime: %s on %s is muted; not promoting", tool, repo.full_name)
            continue
        if _documented_change_exists(
            session, _source_ids_of(dep for _cs, dep in tool_sites), since
        ):
            log.info(
                "runtime: %s on %s has a documented change in the window; not promoting",
                tool,
                repo.full_name,
            )
            continue
        source = observed_source(session, repo, tool, server)
        _from_fp, to_fp = _fingerprints(session, repo, tool, cluster)
        row = _existing_promotion(session, source, to_fp)
        classification: Classification | None
        if row is not None:
            classification = session.scalar(
                select(Classification).where(Classification.change_event_id == row.id)
            )
            if classification is None:
                continue
        else:
            row, classification = synthesize_observed_event(session, repo, tool, cluster)
            promoted.append(row)
        for dep in {dep.id: dep for _cs, dep in tool_sites}.values():
            if dep.source_id is None and dep.kind == "mcp":
                dep.source_id = source.id
        matched = [site_record(cs, dep, (dep.meta or {}).get("vendor")) for cs, dep in tool_sites]
        impact, created = ensure_impact(session, repo, row, classification, matched)
        if created:
            impact.runtime_error_count = 0
            after_match(session, impact, org)
            impacts.append(impact)
    session.flush()
    return promoted, impacts


# --------------------------------------------------------------------------------------
# (c) error spikes
# --------------------------------------------------------------------------------------


def runtime_evidence_comment(count: int, error_class: str | None) -> str:
    return (
        f"{RUNTIME_COMMENT_MARKER}\n**Runtime evidence:** {int(count)} failed calls in the last hour "
        f"(latest error class: {plain(error_class or 'unknown')})"
    )


def attach_error_spikes(
    session: Session, repo: Repo, events: list[RuntimeEvent], *, now: datetime
) -> dict[str, int]:
    """(c): ``{impact_id: failed calls in the last hour}`` for impacts that were updated.

    A failed call counts for an impact when one of its matched call sites has the same
    operation **and** belongs to a dependency the call went to (``scoped_pairs``: the same
    host, or the same MCP server), so two vendors sharing a path shape never pool their
    failures on each other's issues."""
    since = now - timedelta(seconds=ERROR_SPIKE_WINDOW_S)
    errors = [e for e in events if e.outcome == "error" and e.observed_at >= since]
    impacts = list(
        session.scalars(
            select(Impact).where(
                Impact.repo_id == repo.id, Impact.status.in_(("detected", *OPEN_ISSUE_STATUSES))
            )
        )
    )
    out: dict[str, int] = {}
    if not impacts:
        return out
    repo_deps = [
        (None, dep)
        for dep in session.scalars(select(Dependency).where(Dependency.repo_id == repo.id))
    ]
    # (kind, host / server, operation) -> (ids of the dependencies it may hit, op, events)
    groups: dict[tuple[str, str, str], tuple[set[str], str, list[RuntimeEvent]]] = {}
    for ev in errors:
        scope = event_scope(ev)
        op = runtime_operation(ev)
        key = (*scope.key, normalize_operation(op))
        if key not in groups:
            ids = {str(dep.id) for _none, dep in scoped_pairs(repo_deps, scope)}
            groups[key] = (ids, op, [])
        groups[key][2].append(ev)
    for impact in impacts:
        sites = [
            (str(s.get("operation")), str(s.get("dependency_id") or ""))
            for s in (impact.matched_call_sites or [])
            if isinstance(s, dict) and s.get("operation")
        ]
        if not sites:
            continue
        hits: list[RuntimeEvent] = []
        for dep_ids, tool_op, evs in groups.values():
            if any(
                dep_id in dep_ids
                and (operations_match(op, tool_op) or operations_match(tool_op, op))
                for op, dep_id in sites
            ):
                hits.extend(evs)
        count = len(hits)
        if count == 0:
            if impact.runtime_error_count:
                impact.runtime_error_count = 0
            continue
        changed = count != int(impact.runtime_error_count or 0)
        impact.runtime_error_count = count
        out[str(impact.id)] = count
        if (
            impact.status not in OPEN_ISSUE_STATUSES
            or not impact.github_issue_number
            or not changed
        ):
            continue
        latest = max(hits, key=lambda e: e.observed_at)
        body = runtime_evidence_comment(count, latest.error_class)
        try:
            action = upsert_impact_comment(session, impact, RUNTIME_COMMENT_MARKER, body)
            log.info("runtime: impact %s evidence comment %s (%s errors)", impact.id, action, count)
        except Exception as e:  # noqa: BLE001 - GitHub trouble must not fail the run
            log.warning("could not update runtime evidence on impact %s: %s", impact.id, e)
    session.flush()
    return out


# --------------------------------------------------------------------------------------
# The run + job handler
# --------------------------------------------------------------------------------------


def match_runtime(
    session: Session, repo: Repo, *, now: datetime | None = None
) -> RuntimeMatchResult:
    settings = get_settings()
    now = now or utcnow()
    window_s = max(int(settings.runtime_promotion_window_s), ERROR_SPIKE_WINDOW_S)
    events = recent_events(session, repo, now - timedelta(seconds=window_s))
    result = RuntimeMatchResult(repo_id=str(repo.id), events_seen=len(events))
    if not events:
        return result
    result.dependencies_created, result.call_sites_created = discover_dependencies(
        session, repo, events, now=now
    )
    session.commit()
    promoted, impacts = promote_mismatches(
        session,
        repo,
        events,
        now=now,
        threshold=int(settings.runtime_promotion_threshold),
        window_s=int(settings.runtime_promotion_window_s),
    )
    result.promoted = [str(r.id) for r in promoted]
    result.impacts = [str(i.id) for i in impacts]
    session.commit()
    result.error_spikes = attach_error_spikes(session, repo, events, now=now)
    session.commit()
    log.info(
        "runtime match for %s: %s events, %s new deps, %s new call sites, %s promoted, %s spikes",
        repo.full_name,
        result.events_seen,
        result.dependencies_created,
        result.call_sites_created,
        len(result.promoted),
        len(result.error_spikes),
    )
    return result


@job_handler(JOB_MATCH_RUNTIME)
def handle_match_runtime(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("repo_id")
    if not raw:
        raise NonRetryable("match_runtime: payload has no repo_id")
    try:
        repo_id = UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        raise NonRetryable(f"match_runtime: bad repo_id {raw!r}")
    repo = session.get(Repo, repo_id)
    if repo is None:
        raise NonRetryable(f"match_runtime: repo {repo_id} does not exist")
    if not repo.active:
        return {"repo_id": str(repo_id), "skipped": "inactive"}
    return match_runtime(session, repo).as_dict()


__all__ = [
    "RULE_RUNTIME_PROMOTION",
    "RuntimeMatchResult",
    "RuntimeScope",
    "attach_error_spikes",
    "dependency_name",
    "discover_dependencies",
    "event_scope",
    "handle_match_runtime",
    "http_host",
    "match_runtime",
    "observed_locator",
    "observed_source",
    "promote_mismatches",
    "runtime_evidence_comment",
    "runtime_kind",
    "runtime_operation",
    "sample_server",
    "scoped_pairs",
    "server_key",
    "synthesize_observed_event",
    "watchable_server_url",
]
