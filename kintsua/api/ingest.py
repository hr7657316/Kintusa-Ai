"""SDK and CLI ingest: ``POST /v1/ingest`` (runtime event batches) and ``POST /v1/watch/push``
(``kintsua watch --push`` snapshots). Both authenticate with an SDK key.

``/v1/ingest`` writes ``runtime_event`` rows (secrets in ``sample`` redacted again
server-side, ``observed_at`` clamped to now, the ingest ``kind`` kept inside ``sample`` for
the matcher) and queues ``match_runtime`` for the repo, debounced 30 s.

``/v1/watch/push`` stores the pushed canonical document as a snapshot of an org-scoped
source (docs/CONTRACTS.md, "Source trust", rule 4): ``origin="push"``,
``owner_org_id=<org>``, locator ``push:<org_id>:<kind>:<locator>`` and
``meta.pushed_locator=<locator>``. It never writes onto a seeded, customer or another org's
source, so a push cannot fabricate a change for repos it does not own, and pushed sources
are never polled (a stdio command line is accepted because the worker never runs it).
``ingest_canonical_doc`` diffs against the previous push, emits the change event and queues
``classify_event``; the pushing repo gets a ``dependency`` on the source with one
runtime-style ``call_site`` per tool / operation of the pushed document, so the matcher has
something to land impacts on.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kintsua.api.deps import org_for_sdk_key
from kintsua.api.schemas import IngestBatch, IngestEvent, WatchPushBody, WatchSource
from kintsua.core.config import get_settings
from kintsua.core.db import get_db
from kintsua.core.events import CanonicalDoc, canonical_json
from kintsua.core.models import CallSite, Dependency, Org, Repo, RuntimeEvent, Snapshot, Source
from kintsua.core.netguard import is_http_locator
from kintsua.core.queue import JOB_MATCH_RUNTIME, enqueue
from kintsua.core.storage import get_document
from kintsua.core.util import redact_secrets, short_hash, utcnow
from kintsua.matcher.issues import RUNTIME_FILE_PATH
from kintsua.watchers.run import find_or_create_source, ingest_canonical_doc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["ingest"])

MATCH_RUNTIME_DELAY_S = 30
MAX_SAMPLE_BYTES = 16 * 1024
MAX_SAMPLE_KEYS = 200
MAX_SAMPLE_DEPTH = 6
MAX_OP_VALUE_CHARS = 512
HTTP_TOOL_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S+", re.IGNORECASE)
PUSH_ORIGIN = "push"
PUSH_SITE_SYMBOL = "kintsua watch --push"
MAX_PUSH_CALL_SITES = 1000
MAX_DEPENDENCY_NAME = 512  # dependency.name is varchar(512)
OPENAPI_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def find_repo(session: Session, org: Org, full_name: str) -> Repo | None:
    """The org's repo by ``owner/name`` (case-insensitive); inactive repos still count so an
    SDK keeps reporting while a repo is suspended."""
    return session.scalar(
        select(Repo)
        .where(Repo.org_id == org.id, func.lower(Repo.full_name) == full_name.strip().lower())
        .order_by(Repo.active.desc(), Repo.created_at.desc())
        .limit(1)
    )


def _require_repo(session: Session, org: Org, full_name: str) -> Repo:
    repo = find_repo(session, org, full_name)
    if repo is None:
        raise HTTPException(
            status_code=404,
            detail=f"repo {full_name!r} is not installed for this org; install the GitHub App on it first",
        )
    return repo


def redact_sample(value: Any, depth: int = 0) -> Any:
    """Redact secrets in every string, bound the size, keep the structure (keys only is the
    SDK's job; the server redacts again so a misconfigured SDK cannot leak a token)."""
    if depth > MAX_SAMPLE_DEPTH:
        return None
    if isinstance(value, str):
        return redact_secrets(value)[:2048]
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= MAX_SAMPLE_KEYS:
                out["_truncated"] = True
                break
            out[redact_secrets(str(k))[:128]] = redact_sample(v, depth + 1)
        return out
    if isinstance(value, list | tuple):
        items = [redact_sample(v, depth + 1) for v in list(value)[:MAX_SAMPLE_KEYS]]
        if len(value) > MAX_SAMPLE_KEYS:
            items.append("_truncated")
        return items
    return redact_secrets(str(value))[:512]


def infer_kind(event: IngestEvent) -> str:
    if event.kind in ("mcp", "http"):
        return event.kind
    return "http" if HTTP_TOOL_RE.match(event.tool_name or "") else "mcp"


def normalise_observed_at(value: datetime | None, now: datetime) -> datetime:
    if value is None:
        return now
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return now if value > now else value


def _json_size(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _clip_op_value(value: Any) -> Any:
    """An op's ``before`` / ``after`` as-is when small, else a clipped JSON excerpt."""
    text = value if isinstance(value, str) else canonical_json(value)
    if len(text) <= MAX_OP_VALUE_CHARS:
        return value
    return text[:MAX_OP_VALUE_CHARS] + "..."


def compact_diff(diff: list[Any], budget: int) -> tuple[list[dict[str, Any]], int]:
    """As many ops of a schema diff as fit in ``budget`` bytes, each keeping ``op`` and
    ``path`` with ``before`` / ``after`` clipped; returns ``(ops, ops_dropped)``."""
    ops = [raw for raw in diff if isinstance(raw, dict)]
    out: list[dict[str, Any]] = []
    used = 2
    for raw in ops:
        op: dict[str, Any] = {k: raw[k] for k in ("op", "path") if k in raw}
        for key in ("before", "after"):
            if key in raw:
                op[key] = _clip_op_value(raw[key])
        size = _json_size(op) + 1
        if used + size > budget:
            break
        out.append(op)
        used += size
    return out, len(ops) - len(out)


def build_sample(event: IngestEvent) -> dict[str, Any]:
    sample = redact_sample(dict(event.sample or {}))
    if not isinstance(sample, dict):
        sample = {}
    sample["kind"] = infer_kind(event)
    if _json_size(sample) > MAX_SAMPLE_BYTES:
        keep: dict[str, Any] = {"kind": sample["kind"], "truncated": True}
        for key in ("arg_keys", "server", "status", "previous_fingerprint"):
            if key in sample:
                keep[key] = sample[key]
        diff = sample.get("diff")
        if isinstance(diff, list) and diff:
            # a schema_mismatch keeps which properties changed: the matcher promotes it
            # into a change event built from these ops
            ops, dropped = compact_diff(diff, MAX_SAMPLE_BYTES - _json_size(keep) - 64)
            keep["diff"] = ops
            if dropped:
                keep["ops_dropped"] = dropped
        sample = keep
    return sample


def runtime_event_row(repo: Repo, key_hash: str, event: IngestEvent, now: datetime) -> RuntimeEvent:
    return RuntimeEvent(
        repo_id=repo.id,
        sdk_key_hash=key_hash,
        tool_name=event.tool_name,
        schema_fingerprint=event.schema_fingerprint,
        outcome=event.outcome,
        error_class=redact_secrets(event.error_class)[:255] if event.error_class else None,
        latency_ms=event.latency_ms,
        sample=build_sample(event),
        observed_at=normalise_observed_at(event.observed_at, now),
    )


# --------------------------------------------------------------------------------------
# POST /v1/ingest
# --------------------------------------------------------------------------------------


@router.post("/ingest")
def ingest(
    body: IngestBatch,
    org: Org = Depends(org_for_sdk_key),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = get_settings()
    if len(body.events) > settings.ingest_max_batch:
        raise HTTPException(
            status_code=413,
            detail=f"batch of {len(body.events)} events exceeds the limit of {settings.ingest_max_batch}",
        )
    repo = _require_repo(session, org, body.repo)
    now = utcnow()
    key_hash = org.sdk_key_hash or ""
    rows = [runtime_event_row(repo, key_hash, ev, now) for ev in body.events]
    if rows:
        session.add_all(rows)
        session.flush()
        enqueue(
            session,
            JOB_MATCH_RUNTIME,
            {"repo_id": str(repo.id)},
            dedupe_key=f"match_runtime:{repo.id}",
            delay_s=MATCH_RUNTIME_DELAY_S,
        )
    session.commit()
    log.info(
        "ingest: %d runtime event(s) for %s (sdk %s)", len(rows), repo.full_name, body.sdk_version
    )
    return {"accepted": len(rows), "repo_id": str(repo.id)}


# --------------------------------------------------------------------------------------
# POST /v1/watch/push
# --------------------------------------------------------------------------------------


def push_locator(org: Org, src: WatchSource) -> str:
    """The org-scoped locator a pushed snapshot is stored under (CONTRACTS rule 4)."""
    return f"push:{org.id}:{src.kind}:{src.locator}"


def push_source_meta(src: WatchSource) -> dict[str, Any]:
    meta: dict[str, Any] = {"pushed_by": "cli", "pushed_locator": src.locator}
    if src.kind == "mcp":
        meta["transport"] = "http" if is_http_locator(src.locator) else "stdio"
    return meta


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


def push_source(session: Session, org: Org, src: WatchSource) -> Source:
    """The org's own ``origin="push"`` source for ``src`` (created on first push).

    Refuses (409) a row under that locator that is not this org's push source, and a push
    source some other org's repo depends on: a push must never write a snapshot or change
    event that reaches another org."""
    locator = push_locator(org, src)
    existing = session.scalar(
        select(Source).where(Source.kind == src.kind, Source.locator == locator)
    )
    if existing is not None and (existing.origin != PUSH_ORIGIN or existing.owner_org_id != org.id):
        log.warning(
            "watch/push: %s %s is a %s source, not this org's push source; refused",
            src.kind,
            locator,
            existing.origin,
        )
        raise _conflict("this locator is not one of your pushed sources; the push was refused")
    source = existing or find_or_create_source(
        session,
        src.kind,
        locator,
        display_name=src.display_name or src.locator,
        vendor=src.vendor,
        meta=push_source_meta(src),
        origin=PUSH_ORIGIN,
        owner_org_id=org.id,
    )
    if source.origin != PUSH_ORIGIN or source.owner_org_id != org.id:  # lost a create race
        raise _conflict("this locator is not one of your pushed sources; the push was refused")
    foreign = session.scalar(
        select(Dependency.id)
        .join(Repo, Repo.id == Dependency.repo_id)
        .where(Dependency.source_id == source.id, Repo.org_id != org.id)
        .limit(1)
    )
    if foreign is not None:
        log.warning("watch/push: %s is depended on by another org; refused", locator)
        raise _conflict("another organisation depends on this source; the push was refused")
    return source


def dependency_name(locator: str) -> str:
    """``dependency.name`` for a pushed source: its locator, shortened with a hash suffix
    when it does not fit the column."""
    if len(locator) <= MAX_DEPENDENCY_NAME:
        return locator
    suffix = "#" + short_hash(locator, 16)
    return locator[: MAX_DEPENDENCY_NAME - len(suffix)] + suffix


def ensure_dependency(session: Session, repo: Repo, source: Source, src: WatchSource) -> Dependency:
    name = dependency_name(source.locator)
    dep = session.scalar(
        select(Dependency).where(
            Dependency.repo_id == repo.id,
            Dependency.kind == source.kind,
            Dependency.name == name,
        )
    )
    if dep is None:
        meta: dict[str, Any] = {"pushed_by": "cli", "pushed_locator": src.locator}
        if src.vendor or source.vendor:
            meta["vendor"] = src.vendor or source.vendor
        display = src.display_name or source.display_name or src.locator
        meta["display_name"] = display
        if source.kind == "mcp":
            meta["server_name"] = display
        meta.update({k: v for k, v in push_source_meta(src).items() if k != "pushed_by"})
        dep = Dependency(
            repo_id=repo.id,
            source_id=source.id,
            kind=source.kind,
            name=name,
            discovered_at_runtime=True,
            meta=meta,
        )
        session.add(dep)
        session.flush()
        return dep
    if dep.source_id != source.id:
        dep.source_id = source.id
        session.flush()
    return dep


def doc_operations(kind: str, body: dict[str, Any] | None) -> list[str]:
    """The call-site operations a pushed document offers: MCP tool names, or
    ``"METHOD /path"`` for every OpenAPI operation."""
    body = body if isinstance(body, dict) else {}
    out: list[str] = []
    if kind == "mcp":
        tools = body.get("tools")
        if isinstance(tools, dict):
            out = [str(name) for name in tools]
        elif isinstance(tools, list):
            out = [str(t["name"]) for t in tools if isinstance(t, dict) and t.get("name")]
    else:
        paths = body.get("paths")
        for path, item in paths.items() if isinstance(paths, dict) else []:
            if not isinstance(item, dict):
                continue
            for method in item:
                if str(method).lower() in OPENAPI_METHODS:
                    out.append(f"{str(method).upper()} {path}")
    return [op for op in dict.fromkeys(o.strip() for o in out) if op]


def latest_pushed_body(session: Session, source: Source) -> dict[str, Any] | None:
    snap = session.scalar(
        select(Snapshot)
        .where(Snapshot.source_id == source.id)
        .order_by(Snapshot.captured_at.desc(), Snapshot.created_at.desc())
        .limit(1)
    )
    if snap is None:
        return None
    try:
        body = get_document(snap.storage_ref)
    except Exception as e:  # noqa: BLE001 - a missing blob only costs call sites
        log.warning("watch/push: previous snapshot of %s unreadable: %s", source.locator, e)
        return None
    return body if isinstance(body, dict) else None


def ensure_push_call_sites(session: Session, dep: Dependency, operations: list[str]) -> int:
    """One runtime-style call site per tool / operation the pushed documents offered.

    The CLI user named this server for this repo but no code location is known, so every
    tool counts as used (as for tools the runtime SDK observes). Sites are only ever added,
    never removed, so a tool that disappears still matches its removal event."""
    known = set(session.scalars(select(CallSite.operation).where(CallSite.dependency_id == dep.id)))
    added = 0
    for op in operations:
        if op in known:
            continue
        if len(known) >= MAX_PUSH_CALL_SITES:
            log.info("watch/push: %s has %d call sites; not adding more", dep.name, len(known))
            break
        session.add(
            CallSite(
                dependency_id=dep.id,
                file_path=RUNTIME_FILE_PATH,
                line=0,
                symbol=PUSH_SITE_SYMBOL,
                operation=op,
                args_shape={},
                discovered_at_runtime=True,
            )
        )
        known.add(op)
        added += 1
    if added:
        session.flush()
    return added


def change_event_ops(diff: dict[str, Any] | None) -> int:
    if not diff:
        return 0
    total = diff.get("ops_total")
    if isinstance(total, int):
        return total
    ops = diff.get("ops")
    return len(ops) if isinstance(ops, list) else 0


@router.post("/watch/push")
def watch_push(
    body: WatchPushBody,
    org: Org = Depends(org_for_sdk_key),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    repo = _require_repo(session, org, body.repo)
    src = body.source
    source = push_source(session, org, src)
    had_snapshot = source.last_polled_at is not None
    previous_body = latest_pushed_body(session, source) if had_snapshot else None
    doc = CanonicalDoc(version=body.doc.version, body=body.doc.body)
    try:
        event = ingest_canonical_doc(session, source, doc)
    except Exception as e:  # noqa: BLE001 - a malformed document must not 500
        log.warning("watch/push: could not ingest %s %s: %s", src.kind, src.locator, e)
        raise HTTPException(
            status_code=422, detail=f"could not ingest the document: {type(e).__name__}: {e}"
        )
    dep = ensure_dependency(session, repo, source, src)
    operations = doc_operations(src.kind, previous_body) + doc_operations(src.kind, doc.body)
    sites_added = ensure_push_call_sites(session, dep, list(dict.fromkeys(operations)))
    session.commit()
    ops = change_event_ops(event.diff if event is not None else None)
    log.info(
        "watch/push: %s %s for %s -> %s (%d ops, %d new call sites)",
        src.kind,
        src.locator,
        repo.full_name,
        f"change event {event.id}" if event is not None else "no change",
        ops,
        sites_added,
    )
    return {
        "source_id": str(source.id),
        "dependency_id": str(dep.id),
        "change_event_id": str(event.id) if event is not None else None,
        "ops": ops,
        "first_snapshot": not had_snapshot,
        "version": doc.version,
    }


__all__ = [
    "build_sample",
    "change_event_ops",
    "compact_diff",
    "dependency_name",
    "doc_operations",
    "ensure_dependency",
    "ensure_push_call_sites",
    "find_repo",
    "infer_kind",
    "ingest",
    "normalise_observed_at",
    "push_locator",
    "push_source",
    "push_source_meta",
    "redact_sample",
    "router",
    "watch_push",
]
