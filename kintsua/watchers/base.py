"""Watcher interface and the generic pieces every adapter shares.

A watcher fetches the current state of a source and returns a ``CanonicalDoc``; the
base layer handles hashing, snapshot storage, diffing and event emission so each
adapter only has to implement ``fetch`` and (optionally) a smarter ``diff``.

This module is dependency-light (pydantic only) so the open-source CLI can use it. The
Postgres-backed snapshot store and the job plumbing live in ``kintsua.watchers.run``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, Field

from kintsua.core.events import (
    CanonicalDoc,
    ChangeEvent,
    ChangeOp,
    SourceKind,
    canonical_hash,
    json_pointer,
)

NAMESPACE_SOURCE = UUID("6f1b4a7e-2c1d-4e7c-9a5b-7d9e0c1f2a3b")

# Keys that change on every fetch and must never influence the content hash.
VOLATILE_KEYS = frozenset(
    {
        "timestamp",
        "timestamps",
        "generated_at",
        "generatedAt",
        "requestId",
        "request_id",
        "x-request-id",
        "etag",
        "ETag",
        "lastModified",
        "last_modified",
        "nonce",
        "sessionId",
        "session_id",
        "_meta",
        "date",
        "fetched_at",
    }
)


class SourceRef(BaseModel):
    """A DB-free description of a source. The server builds it from a ``Source`` row; the
    CLI builds it from command-line flags."""

    id: UUID = Field(default_factory=uuid4)
    kind: SourceKind
    locator: str
    display_name: str = ""
    vendor: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def stable(cls, kind: SourceKind, locator: str, **kw: Any) -> SourceRef:
        """Deterministic id from (kind, locator) so the CLI lockfile is reproducible."""
        return cls(
            id=uuid5(NAMESPACE_SOURCE, f"{kind}:{locator}"), kind=kind, locator=locator, **kw
        )


@runtime_checkable
class Watcher(Protocol):
    kind: str

    async def fetch(self, source: SourceRef) -> CanonicalDoc: ...

    def diff(self, before: CanonicalDoc, after: CanonicalDoc) -> list[ChangeOp]: ...


class BaseWatcher:
    """Default ``diff`` is a generic key-wise JSON diff; adapters override for semantics."""

    kind: str = ""

    async def fetch(self, source: SourceRef) -> CanonicalDoc:  # pragma: no cover - abstract
        raise NotImplementedError

    def diff(self, before: CanonicalDoc, after: CanonicalDoc) -> list[ChangeOp]:
        return generic_json_diff(before.body, after.body)


REGISTRY: dict[str, type[BaseWatcher]] = {}


def register(cls: type[BaseWatcher]) -> type[BaseWatcher]:
    REGISTRY[cls.kind] = cls
    return cls


def get_watcher(kind: str) -> BaseWatcher:
    """Instantiate the adapter for a source kind, importing adapters lazily."""
    if kind not in REGISTRY:
        import importlib

        mod = {
            "openapi": "openapi",
            "mcp": "mcp",
            "pypi": "packages",
            "npm": "packages",
            "changelog": "changelog",
        }.get(kind)
        if mod:
            importlib.import_module(f"kintsua.watchers.{mod}")
    if kind not in REGISTRY:
        raise KeyError(f"no watcher for source kind {kind!r}")
    return REGISTRY[kind]()


# --------------------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------------------


def strip_volatile(obj: Any, extra: set[str] | frozenset[str] | None = None) -> Any:
    """Recursively drop volatile keys and sort dict keys (lists keep order)."""
    vol = VOLATILE_KEYS | (frozenset(extra) if extra else frozenset())
    if isinstance(obj, dict):
        return {k: strip_volatile(v, vol) for k, v in sorted(obj.items()) if k not in vol}
    if isinstance(obj, list):
        return [strip_volatile(v, vol) for v in obj]
    return obj


def sort_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: sort_keys(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [sort_keys(v) for v in obj]
    return obj


# --------------------------------------------------------------------------------------
# Generic diff
# --------------------------------------------------------------------------------------


def _is_scalar(x: Any) -> bool:
    return not isinstance(x, dict | list)


def generic_json_diff(before: Any, after: Any, path: tuple[str | int, ...] = ()) -> list[ChangeOp]:
    """Key-wise structural diff producing ``added`` / ``removed`` / ``type_changed`` /
    ``text_changed`` ops with JSON-pointer paths. Lists are compared element-wise by index
    (adapters with ids should normalise lists to dicts before calling this)."""
    ops: list[ChangeOp] = []
    ptr = json_pointer(*path) if path else ""
    if isinstance(before, dict) and isinstance(after, dict):
        for k in sorted(set(before) | set(after)):
            if k not in before:
                ops.append(
                    ChangeOp(op="added", path=json_pointer(*path, k), before=None, after=after[k])
                )
            elif k not in after:
                ops.append(
                    ChangeOp(
                        op="removed", path=json_pointer(*path, k), before=before[k], after=None
                    )
                )
            else:
                ops.extend(generic_json_diff(before[k], after[k], (*path, k)))
        return ops
    if isinstance(before, list) and isinstance(after, list):
        n = max(len(before), len(after))
        for i in range(n):
            if i >= len(before):
                ops.append(
                    ChangeOp(op="added", path=json_pointer(*path, i), before=None, after=after[i])
                )
            elif i >= len(after):
                ops.append(
                    ChangeOp(
                        op="removed", path=json_pointer(*path, i), before=before[i], after=None
                    )
                )
            else:
                ops.extend(generic_json_diff(before[i], after[i], (*path, i)))
        return ops
    if before == after:
        return ops
    if type(before) is not type(after) and not (
        isinstance(before, int | float) and isinstance(after, int | float)
    ):
        ops.append(ChangeOp(op="type_changed", path=ptr or "/", before=before, after=after))
    elif isinstance(before, str) and isinstance(after, str):
        ops.append(ChangeOp(op="text_changed", path=ptr or "/", before=before, after=after))
    else:
        ops.append(ChangeOp(op="type_changed", path=ptr or "/", before=before, after=after))
    return ops


# --------------------------------------------------------------------------------------
# Snapshot stores
# --------------------------------------------------------------------------------------


class SnapshotRecord(BaseModel):
    source_id: UUID
    content_hash: str
    version: str | None = None
    captured_at: datetime
    body: dict[str, Any] | None = None  # loaded lazily by DB stores
    snapshot_id: UUID | None = None

    def as_doc(self) -> CanonicalDoc:
        return CanonicalDoc(version=self.version, body=self.body or {})


class SnapshotStore(Protocol):
    def latest(self, source_id: UUID) -> SnapshotRecord | None: ...

    def save(self, source: SourceRef, doc: CanonicalDoc) -> SnapshotRecord: ...


class FileSnapshotStore:
    """Lockfile-backed store used by the CLI: ``<dir>/<name>.lock.json`` holds the latest
    canonical document and ``<dir>/history.jsonl`` the change events seen."""

    def __init__(self, directory: str | Path = ".kintsua"):
        self.dir = Path(directory)

    def path_for(self, source: SourceRef | UUID, name: str | None = None) -> Path:
        if isinstance(source, SourceRef):
            name = name or lockfile_name(source)
        else:
            name = name or str(source)
        return self.dir / f"{name}.lock.json"

    def latest(self, source_id: UUID, name: str | None = None) -> SnapshotRecord | None:
        p = self.dir / f"{name}.lock.json" if name else None
        if p is None:
            for cand in self.dir.glob("*.lock.json"):
                data = json.loads(cand.read_text("utf-8"))
                if data.get("source_id") == str(source_id):
                    p = cand
                    break
        if p is None or not p.exists():
            return None
        data = json.loads(p.read_text("utf-8"))
        return SnapshotRecord(
            source_id=UUID(data["source_id"]),
            content_hash=data["content_hash"],
            version=data.get("version"),
            captured_at=datetime.fromisoformat(data["captured_at"]),
            body=data.get("body"),
        )

    def save(self, source: SourceRef, doc: CanonicalDoc, name: str | None = None) -> SnapshotRecord:
        from kintsua.core.util import utcnow

        self.dir.mkdir(parents=True, exist_ok=True)
        rec = SnapshotRecord(
            source_id=source.id,
            content_hash=doc.content_hash(),
            version=doc.version,
            captured_at=utcnow(),
            body=doc.body,
        )
        payload = {
            "kintsua_lockfile": 1,
            "source_id": str(source.id),
            "kind": source.kind,
            "locator": source.locator,
            "display_name": source.display_name,
            "content_hash": rec.content_hash,
            "version": rec.version,
            "captured_at": rec.captured_at.isoformat(),
            "body": doc.body,
        }
        self.path_for(source, name).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        return rec

    def append_history(self, event: ChangeEvent, extra: dict[str, Any] | None = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        row = {**event.model_dump(mode="json"), **(extra or {})}
        with (self.dir / "history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    def history(self) -> list[dict[str, Any]]:
        p = self.dir / "history.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text("utf-8").splitlines() if line.strip()]


def lockfile_name(source: SourceRef) -> str:
    """``https://mcp.vendor.com/mcp`` -> ``mcp.vendor.com``; ``npx -y foo`` -> ``npx-y-foo``."""
    import re

    loc = source.locator
    if "://" in loc:
        host = loc.split("://", 1)[1].split("/", 1)[0]
        return re.sub(r"[^A-Za-z0-9.-]+", "-", host) or "server"
    return re.sub(r"[^A-Za-z0-9.-]+", "-", loc).strip("-")[:80] or "server"


# --------------------------------------------------------------------------------------
# The shared flow: compare a fresh document against the previous snapshot
# --------------------------------------------------------------------------------------


def compare(
    watcher: BaseWatcher | Watcher,
    source: SourceRef,
    previous: SnapshotRecord | None,
    doc: CanonicalDoc,
    *,
    raw_diff_ref: str = "inline",
    text: str | None = None,
) -> ChangeEvent | None:
    """Return a ``ChangeEvent`` if ``doc`` differs from ``previous`` (None on first sight
    or when the content hash is unchanged)."""
    new_hash = doc.content_hash()
    if previous is None:
        return None
    if previous.content_hash == new_hash:
        return None
    ops = watcher.diff(previous.as_doc(), doc)
    if not ops and (previous.version == doc.version):
        return None
    return ChangeEvent(
        source_id=source.id,
        source_kind=source.kind,
        from_version=previous.version or previous.content_hash[:12],
        to_version=doc.version or new_hash[:12],
        ops=ops,
        raw_diff_ref=raw_diff_ref,
        text=text,
    )


def hash_doc(doc: CanonicalDoc) -> str:
    return canonical_hash(doc.body)
