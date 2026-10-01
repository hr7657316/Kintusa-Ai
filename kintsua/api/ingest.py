"""SDK and CLI ingest: ``POST /v1/ingest`` (runtime event batches) and ``POST /v1/watch/push``
(``kintsua watch --push`` snapshots). Both authenticate with an SDK key.

``/v1/ingest`` writes ``runtime_event`` rows (secrets in ``sample`` redacted again
server-side, ``observed_at`` clamped to now, the ingest ``kind`` kept inside ``sample`` for
the matcher) and queues ``match_runtime`` for the repo, debounced 30 s.

``/v1/watch/push`` stores the pushed canonical document as a snapshot of the source,
diffs it against the previous one (``ingest_canonical_doc`` emits the change event and
queues ``classify_event``) and makes sure the repo has a ``dependency`` row on that source
so the matcher lands impacts on it.
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
from kintsua.core.models import Dependency, Org, Repo, RuntimeEvent, Source
from kintsua.core.queue import JOB_MATCH_RUNTIME, enqueue
from kintsua.core.util import redact_secrets, utcnow
from kintsua.watchers.run import find_or_create_source, ingest_canonical_doc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["ingest"])

MATCH_RUNTIME_DELAY_S = 30
MAX_SAMPLE_BYTES = 16 * 1024
MAX_SAMPLE_KEYS = 200
MAX_SAMPLE_DEPTH = 6
HTTP_TOOL_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S+", re.IGNORECASE)


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


def build_sample(event: IngestEvent) -> dict[str, Any]:
    sample = redact_sample(dict(event.sample or {}))
    if not isinstance(sample, dict):
        sample = {}
    sample["kind"] = infer_kind(event)
    if len(canonical_json(sample).encode("utf-8")) > MAX_SAMPLE_BYTES:
        keep: dict[str, Any] = {"kind": sample["kind"], "truncated": True}
        for key in ("arg_keys", "server", "status", "previous_fingerprint"):
            if key in sample:
                keep[key] = sample[key]
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


def push_source_meta(src: WatchSource) -> dict[str, Any]:
    meta: dict[str, Any] = {"pushed_by": "cli"}
    if src.kind == "mcp":
        meta["transport"] = (
            "http" if src.locator.lower().startswith(("http://", "https://")) else "stdio"
        )
    return meta


def ensure_dependency(session: Session, repo: Repo, source: Source, src: WatchSource) -> Dependency:
    dep = session.scalar(
        select(Dependency).where(
            Dependency.repo_id == repo.id,
            Dependency.kind == source.kind,
            Dependency.name == source.locator,
        )
    )
    if dep is None:
        meta: dict[str, Any] = {"pushed_by": "cli"}
        if src.vendor or source.vendor:
            meta["vendor"] = src.vendor or source.vendor
        if src.display_name or source.display_name:
            meta["display_name"] = src.display_name or source.display_name
        meta.update({k: v for k, v in push_source_meta(src).items() if k != "pushed_by"})
        dep = Dependency(
            repo_id=repo.id,
            source_id=source.id,
            kind=source.kind,
            name=source.locator,
            discovered_at_runtime=True,
            meta=meta,
        )
        session.add(dep)
        session.flush()
        return dep
    if dep.source_id is None:
        dep.source_id = source.id
        session.flush()
    return dep


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
    source = find_or_create_source(
        session,
        src.kind,
        src.locator,
        display_name=src.display_name,
        vendor=src.vendor,
        meta=push_source_meta(src),
    )
    had_snapshot = source.last_polled_at is not None
    doc = CanonicalDoc(version=body.doc.version, body=body.doc.body)
    try:
        event = ingest_canonical_doc(session, source, doc)
    except Exception as e:  # noqa: BLE001 - a malformed document must not 500
        log.warning("watch/push: could not ingest %s %s: %s", src.kind, src.locator, e)
        raise HTTPException(
            status_code=422, detail=f"could not ingest the document: {type(e).__name__}: {e}"
        )
    dep = ensure_dependency(session, repo, source, src)
    session.commit()
    ops = change_event_ops(event.diff if event is not None else None)
    log.info(
        "watch/push: %s %s for %s -> %s (%d ops)",
        src.kind,
        src.locator,
        repo.full_name,
        f"change event {event.id}" if event is not None else "no change",
        ops,
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
    "ensure_dependency",
    "find_repo",
    "infer_kind",
    "ingest",
    "normalise_observed_at",
    "redact_sample",
    "router",
    "watch_push",
]
