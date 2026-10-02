"""Seed the ``source`` table from ``kintsua/seed/sources.yml``.

The loader upserts by ``(kind, locator)``: new entries are inserted, existing rows get their
display_name / vendor / poll_interval_s refreshed and their ``meta`` merged (seed keys win,
other keys are preserved). Every seeded row gets ``origin = "seed"``: the operator's sources
are the only ones the worker may run as stdio commands and the only ones on the public
weekly report. A row it promotes to ``seed`` is reactivated with its failure state cleared
(``active``, ``last_error``, ``consecutive_failures``, ``backoff_until``), so a customer stdio
locator the worker refused comes back once the operator seeds it. It never deletes or
deactivates anything. ``--dry-run`` reports what would change without writing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from kintsua.core.models import Source
from kintsua.watchers.run import default_poll_interval

log = logging.getLogger(__name__)

SEED_FILE = Path(__file__).with_name("sources.yml")


class SeedEntry(BaseModel):
    kind: Literal["openapi", "mcp", "pypi", "npm", "changelog"]
    locator: str
    display_name: str
    vendor: str | None = None
    poll_interval_s: int | None = Field(default=None, ge=60)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("locator", "display_name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @property
    def effective_interval(self) -> int:
        return self.poll_interval_s or default_poll_interval(self.kind, self.meta)


def load_seed_sources(path: str | Path | None = None) -> list[SeedEntry]:
    """Parse and validate the seed file; duplicate (kind, locator) pairs are an error."""
    p = Path(path) if path else SEED_FILE
    data = yaml.safe_load(p.read_text("utf-8")) or {}
    raw = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ValueError(f"{p}: expected a top-level 'sources' list")
    entries: list[SeedEntry] = []
    seen: set[tuple[str, str]] = set()
    errors: list[str] = []
    for i, item in enumerate(raw):
        try:
            entry = SeedEntry.model_validate(item)
        except ValidationError as e:
            errors.append(f"entry #{i}: {e.errors()[0]['msg']} ({item!r})"[:300])
            continue
        key = (entry.kind, entry.locator)
        if key in seen:
            errors.append(f"entry #{i}: duplicate {key}")
            continue
        seen.add(key)
        entries.append(entry)
    if errors:
        raise ValueError(f"{p}: invalid seed entries:\n  " + "\n  ".join(errors))
    return entries


@dataclass
class SeedResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated) + len(self.unchanged)

    def summary(self) -> str:
        prefix = "[dry-run] " if self.dry_run else ""
        return (
            f"{prefix}{self.total} seed sources: {len(self.created)} created, "
            f"{len(self.updated)} updated, {len(self.unchanged)} unchanged"
        )


def upsert_sources(
    session: Session, entries: list[SeedEntry], *, dry_run: bool = False
) -> SeedResult:
    """Insert or refresh every entry. Flushes but does not commit (the caller decides; a dry
    run performs no writes at all)."""
    result = SeedResult(dry_run=dry_run)
    for e in entries:
        label = f"{e.kind}:{e.locator}"
        row = session.scalar(
            select(Source).where(Source.kind == e.kind, Source.locator == e.locator)
        )
        if row is None:
            result.created.append(label)
            if not dry_run:
                session.add(
                    Source(
                        kind=e.kind,
                        locator=e.locator,
                        display_name=e.display_name,
                        vendor=e.vendor,
                        poll_interval_s=e.effective_interval,
                        meta=dict(e.meta),
                        origin="seed",
                        owner_org_id=None,
                    )
                )
            continue
        changes: dict[str, Any] = {}
        if row.display_name != e.display_name:
            changes["display_name"] = e.display_name
        if e.vendor is not None and row.vendor != e.vendor:
            changes["vendor"] = e.vendor
        if e.poll_interval_s is not None and row.poll_interval_s != e.poll_interval_s:
            changes["poll_interval_s"] = e.poll_interval_s
        merged_meta = {**(row.meta or {}), **e.meta}
        if merged_meta != (row.meta or {}):
            changes["meta"] = merged_meta
        # the seed file is the operator's list: its rows are trusted and public, even if a
        # customer happened to add the same locator first. A promoted row starts afresh: a
        # customer stdio locator the worker refused and deactivated (or a failing customer
        # row in backoff) is polled again as the operator's source on the next tick.
        if row.origin != "seed":
            changes["origin"] = "seed"
            changes["owner_org_id"] = None
            changes["active"] = True
            changes["last_error"] = None
            changes["consecutive_failures"] = 0
            changes["backoff_until"] = None
        if not changes:
            result.unchanged.append(label)
            continue
        result.updated.append(label)
        if not dry_run:
            for k, v in changes.items():
                setattr(row, k, v)
    if not dry_run:
        session.flush()
    return result


def seed(session: Session, path: str | Path | None = None, *, dry_run: bool = False) -> SeedResult:
    return upsert_sources(session, load_seed_sources(path), dry_run=dry_run)
