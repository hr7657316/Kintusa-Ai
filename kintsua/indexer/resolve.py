"""Resolution: attach a ``Source`` row to every extracted dependency.

* SDK package (curated map) -> package source (``pypi`` / ``npm``) **and** the vendor's
  OpenAPI source (``dep.meta["openapi_source_id"]``), or its changelog source when no spec is
  public (``dep.meta["changelog_source_id"]``). The primary source stays the package.
* Raw HTTP host -> the OpenAPI source for that host when the map knows one; else a source
  from ``.kintsua.yml`` whose ``hosts`` (or spec URL host) cover it; else the vendor's
  changelog; else unwatched (``source_id`` None, grey in the map).
* MCP server -> an ``mcp`` source keyed by URL or command line (transport in meta; only
  env / header *names*). Disabled servers stay unwatched.
* ``.kintsua.yml`` ``sources[]`` -> created and attached to the repo as dependencies of
  kind ``openapi`` / ``changelog`` / ``mcp`` (name = locator) so they show in the map.

Sources are created through ``kintsua.watchers.run.find_or_create_source`` when that module
exists, with a local upsert of identical semantics as the fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kintsua.core.models import Source
from kintsua.indexer import sdk_map
from kintsua.indexer.config import ConfigSource, config_sources
from kintsua.indexer.mcp_config import vendor_for_locator
from kintsua.indexer.schema import DependencyInfo

log = logging.getLogger(__name__)

DEFAULT_POLL_S = {
    "mcp": 6 * 3600,
    "openapi": 24 * 3600,
    "pypi": 6 * 3600,
    "npm": 6 * 3600,
    "changelog": 24 * 3600,
}
PACKAGE_DISPLAY = {"pypi": "PyPI", "npm": "npm"}


@dataclass
class ResolveResult:
    dependencies: list[DependencyInfo]
    new_source_ids: list[UUID] = field(default_factory=list)
    sources: dict[UUID, Source] = field(default_factory=dict)

    @property
    def watched(self) -> int:
        return sum(1 for d in self.dependencies if d.source_id is not None)


def _local_find_or_create_source(
    session: Session,
    kind: str,
    locator: str,
    *,
    display_name: str | None = None,
    vendor: str | None = None,
    meta: dict[str, Any] | None = None,
    poll_interval_s: int | None = None,
) -> Source:
    """Same semantics as ``kintsua.watchers.run.find_or_create_source`` (fallback)."""
    locator = locator.strip()
    row = session.scalar(select(Source).where(Source.kind == kind, Source.locator == locator))
    if row is not None:
        if display_name and (not row.display_name or row.display_name == row.locator):
            row.display_name = display_name
        if vendor and not row.vendor:
            row.vendor = vendor
        if meta:
            merged = {**meta, **(row.meta or {})}
            if merged != (row.meta or {}):
                row.meta = merged
        return row
    row = Source(
        kind=kind,
        locator=locator,
        display_name=display_name or locator,
        vendor=vendor,
        meta=dict(meta or {}),
        poll_interval_s=poll_interval_s or DEFAULT_POLL_S.get(kind, 6 * 3600),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(Source).where(Source.kind == kind, Source.locator == locator)
        )
        if existing is None:  # pragma: no cover
            raise
        return existing
    return row


def _find_or_create_source_fn():  # type: ignore[no-untyped-def]
    try:
        from kintsua.watchers.run import find_or_create_source

        return find_or_create_source
    except ImportError:  # watchers.run not written yet
        return _local_find_or_create_source


class SourceCache:
    """Find-or-create with a per-run cache and a record of which rows are new."""

    def __init__(self, session: Session):
        self.session = session
        self._fn = _find_or_create_source_fn()
        self.cache: dict[tuple[str, str], Source] = {}
        self.new_ids: list[UUID] = []
        self.sources: dict[UUID, Source] = {}

    def get(
        self,
        kind: str,
        locator: str,
        *,
        display_name: str | None = None,
        vendor: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Source:
        locator = locator.strip()
        key = (kind, locator)
        if key in self.cache:
            return self.cache[key]
        existed = (
            self.session.scalar(
                select(Source.id).where(Source.kind == kind, Source.locator == locator)
            )
            is not None
        )
        row = self._fn(
            self.session, kind, locator, display_name=display_name, vendor=vendor, meta=meta
        )
        if not existed:
            self.new_ids.append(row.id)
        self.cache[key] = row
        self.sources[row.id] = row
        return row


def _host_of_url(url: str) -> str | None:
    try:
        return (urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def _config_source_rows(
    cache: SourceCache, cfg_sources: list[ConfigSource]
) -> tuple[list[DependencyInfo], dict[str, Source]]:
    """Create the sources a repo configured and the host -> source map they provide."""
    deps: list[DependencyInfo] = []
    by_host: dict[str, Source] = {}
    for cs in cfg_sources:
        locator = cs.locator
        if not locator:
            continue
        meta: dict[str, Any] = {"configured_by": ".kintsua.yml"}
        if cs.kind == "mcp":
            meta["transport"] = "http" if cs.url else "stdio"
            if not cs.url:
                meta["quiet"] = True
        host_vendor = sdk_map.vendor_for_host(_host_of_url(locator) or "") if cs.url else None
        vendor = host_vendor.vendor if host_vendor else None
        for h in cs.hosts:
            e = sdk_map.vendor_for_host(h)
            if e is not None:
                vendor = vendor or e.vendor
        row = cache.get(cs.kind, locator, display_name=cs.name or locator, vendor=vendor, meta=meta)
        dep = DependencyInfo(
            kind=cs.kind,
            name=locator,
            vendor=row.vendor,
            display_name=cs.name or locator,
            meta={"config_file": ".kintsua.yml", "configured": True, "hosts": list(cs.hosts)},
            source_id=row.id,
        )
        if cs.kind == "mcp":
            dep.meta["transport"] = meta["transport"]
        deps.append(dep)
        if cs.kind in ("openapi", "changelog"):
            for h in cs.hosts:
                by_host.setdefault(h, row)
            url_host = _host_of_url(locator)
            if url_host and not cs.hosts:
                by_host.setdefault(url_host, row)
    return deps, by_host


def _resolve_package(dep: DependencyInfo, cache: SourceCache) -> None:
    lookup = sdk_map.lookup_pypi if dep.kind == "pypi" else sdk_map.lookup_npm
    entry = lookup(dep.name)
    label = PACKAGE_DISPLAY[dep.kind]
    if entry is not None:
        if not entry.watchable:
            return
        dep.vendor = dep.vendor or entry.vendor
        dep.display_name = dep.display_name or entry.display_name
        dep.meta.setdefault("sdk_kind", entry.kind)
        row = cache.get(
            dep.kind, dep.name, display_name=f"{dep.name} ({label})", vendor=entry.vendor
        )
        dep.source_id = row.id
        if entry.openapi_url:
            meta = {"format": entry.openapi_format} if entry.openapi_format else {}
            spec = cache.get(
                "openapi",
                entry.openapi_url,
                display_name=f"{entry.display_name} API",
                vendor=entry.vendor,
                meta=meta,
            )
            dep.meta["openapi_source_id"] = str(spec.id)
        elif entry.changelog_url:
            cl = cache.get(
                "changelog",
                entry.changelog_url,
                display_name=f"{entry.display_name} changelog",
                vendor=entry.vendor,
            )
            dep.meta["changelog_source_id"] = str(cl.id)
        return
    if dep.meta.get("mcp_server_package") and not dep.meta.get("inferred"):
        # an MCP server distributed as a package: a new version is a new tool surface
        row = cache.get(dep.kind, dep.name, display_name=f"{dep.name} ({label})")
        dep.source_id = row.id
        dep.meta.setdefault("sdk_kind", "mcp-server")


def _resolve_http(dep: DependencyInfo, cache: SourceCache, by_host: dict[str, Source]) -> None:
    host = dep.name.lower()
    bare = host.split(":")[0]
    entry = sdk_map.vendor_for_host(bare)
    if entry is not None:
        dep.vendor = dep.vendor or entry.vendor
        dep.display_name = dep.display_name or entry.display_name
    spec_entry = sdk_map.spec_for_host(bare)
    if spec_entry is not None and spec_entry.openapi_url:
        meta = {"format": spec_entry.openapi_format} if spec_entry.openapi_format else {}
        row = cache.get(
            "openapi",
            spec_entry.openapi_url,
            display_name=f"{spec_entry.display_name} API",
            vendor=spec_entry.vendor,
            meta=meta,
        )
        dep.source_id = row.id
        dep.meta["resolved_via"] = "sdk_map"
        return
    cfg = by_host.get(host) or by_host.get(bare)
    if cfg is None:
        for h, row in by_host.items():
            if bare.endswith("." + h):
                cfg = row
                break
    if cfg is not None:
        dep.source_id = cfg.id
        dep.meta["resolved_via"] = ".kintsua.yml"
        return
    if entry is not None and entry.changelog_url:
        row = cache.get(
            "changelog",
            entry.changelog_url,
            display_name=f"{entry.display_name} changelog",
            vendor=entry.vendor,
        )
        dep.source_id = row.id
        dep.meta["resolved_via"] = "changelog"
        return
    dep.meta["resolved_via"] = None


def _resolve_mcp(dep: DependencyInfo, cache: SourceCache) -> None:
    if dep.meta.get("disabled"):
        return
    transport = str(
        dep.meta.get("transport") or ("http" if dep.name.startswith("http") else "stdio")
    )
    meta: dict[str, Any] = {"transport": transport}
    if transport == "stdio":
        meta["quiet"] = True
    for k in ("env_keys", "header_keys"):
        if dep.meta.get(k):
            meta[k] = list(dep.meta[k])
    display = dep.display_name or dep.meta.get("server_name") or dep.name
    dep.vendor = dep.vendor or vendor_for_locator(dep.name)
    row = cache.get("mcp", dep.name, display_name=str(display), vendor=dep.vendor, meta=meta)
    dep.source_id = row.id
    dep.vendor = dep.vendor or row.vendor


def resolve_dependencies(
    session: Session,
    deps: list[DependencyInfo],
    *,
    repo_config: dict[str, Any] | None = None,
) -> ResolveResult:
    """Attach sources (creating them when needed) and append the repo's configured sources."""
    cache = SourceCache(session)
    cfg_deps, by_host = _config_source_rows(cache, config_sources(repo_config))
    out: list[DependencyInfo] = []
    seen = {d.key for d in deps}
    for dep in deps:
        if dep.kind in ("pypi", "npm"):
            _resolve_package(dep, cache)
        elif dep.kind == "http":
            _resolve_http(dep, cache, by_host)
        elif dep.kind == "mcp":
            _resolve_mcp(dep, cache)
        out.append(dep)
    for dep in cfg_deps:
        if dep.key in seen:
            for existing in out:
                if existing.key == dep.key:
                    existing.source_id = existing.source_id or dep.source_id
                    existing.merge_from(dep)
            continue
        out.append(dep)
    session.flush()
    return ResolveResult(
        dependencies=out, new_source_ids=list(cache.new_ids), sources=cache.sources
    )


__all__ = ["ResolveResult", "SourceCache", "resolve_dependencies"]
