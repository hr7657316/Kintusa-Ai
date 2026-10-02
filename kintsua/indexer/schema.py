"""Shared in-memory shapes produced by the three extractors and consumed by
``resolve`` / ``run``. Pure Pydantic, no database imports.

A ``DependencyInfo`` is keyed by ``(kind, name)`` exactly like the ``dependency`` table;
a ``CallSiteInfo`` carries the key of the dependency it belongs to so results from
different extractors can be merged before anything is written.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

DependencyKind = Literal["pypi", "npm", "http", "mcp", "openapi", "changelog"]


class CallSiteInfo(BaseModel):
    dependency_kind: DependencyKind
    dependency_name: str
    file_path: str  # repo-relative, forward slashes
    line: int  # 1-based
    symbol: str | None = None  # stripe.Charge.create, httpx.AsyncClient.get, call_tool
    operation: str  # "POST /v1/charges" | tool name | SDK method chain
    args_shape: dict[str, Any] = Field(default_factory=dict)  # {"keys": [...]}
    in_tests: bool = False

    @property
    def dependency_key(self) -> tuple[str, str]:
        return (self.dependency_kind, self.dependency_name)


class DependencyInfo(BaseModel):
    kind: DependencyKind
    name: str  # package name, host, MCP locator, or spec/changelog URL
    vendor: str | None = None
    display_name: str | None = None
    version_constraint: str | None = None
    # config_file, transport, env_keys, language, host, dev, declared, server_name ...
    meta: dict[str, Any] = Field(default_factory=dict)
    source_id: UUID | None = None  # filled by resolve

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.name)

    def merge_from(self, other: DependencyInfo) -> None:
        """Fold another sighting of the same (kind, name) into this one."""
        if other.vendor and not self.vendor:
            self.vendor = other.vendor
        if other.display_name and not self.display_name:
            self.display_name = other.display_name
        if other.version_constraint and not self.version_constraint:
            self.version_constraint = other.version_constraint
        for k, v in other.meta.items():
            if k not in self.meta:
                self.meta[k] = v
            elif k == "config_files" and isinstance(v, list):
                merged = list(self.meta[k])
                merged.extend(x for x in v if x not in merged)
                self.meta[k] = merged
            elif k == "dev" and self.meta[k] and not v:
                # a package that is both a dev and a runtime dependency is a runtime one
                self.meta[k] = False


class ExtractStats(BaseModel):
    files_seen: int = 0
    files_parsed: int = 0
    parse_errors: int = 0
    skipped_large: int = 0
    dynamic_mcp_registrations: int = 0
    unattributed_tool_calls: int = 0
    errors: list[str] = Field(default_factory=list)  # "<path>: <reason>", capped
    # repo-relative paths that exist but were skipped or only partially extracted (too
    # large, binary, syntax errors, extractor exception): their call sites may be missing,
    # so re-indexing must not treat impacts on them as vanished. Not capped.
    partial_files: list[str] = Field(default_factory=list)

    def add(self, other: ExtractStats) -> ExtractStats:
        return ExtractStats(
            files_seen=self.files_seen + other.files_seen,
            files_parsed=self.files_parsed + other.files_parsed,
            parse_errors=self.parse_errors + other.parse_errors,
            skipped_large=self.skipped_large + other.skipped_large,
            dynamic_mcp_registrations=self.dynamic_mcp_registrations
            + other.dynamic_mcp_registrations,
            unattributed_tool_calls=self.unattributed_tool_calls + other.unattributed_tool_calls,
            errors=(self.errors + other.errors)[:50],
            partial_files=list(dict.fromkeys(self.partial_files + other.partial_files)),
        )

    def _mark_partial(self, path: str) -> None:
        if path and path not in self.partial_files:
            self.partial_files.append(path)

    def record_error(self, path: str, reason: str) -> None:
        self.parse_errors += 1
        self._mark_partial(path)
        if len(self.errors) < 50:
            self.errors.append(f"{path}: {reason}")

    def record_skipped(self, path: str) -> None:
        """A file over the size limit: counted and remembered, not parsed."""
        self.skipped_large += 1
        self._mark_partial(path)


class ExtractResult(BaseModel):
    dependencies: list[DependencyInfo] = Field(default_factory=list)
    call_sites: list[CallSiteInfo] = Field(default_factory=list)
    stats: ExtractStats = Field(default_factory=ExtractStats)

    def add_dependency(self, dep: DependencyInfo) -> DependencyInfo:
        for existing in self.dependencies:
            if existing.key == dep.key:
                existing.merge_from(dep)
                return existing
        self.dependencies.append(dep)
        return dep

    def merge(self, other: ExtractResult) -> ExtractResult:
        out = ExtractResult(stats=self.stats.add(other.stats))
        for dep in self.dependencies + other.dependencies:
            out.add_dependency(dep.model_copy(deep=True))
        out.call_sites = list(self.call_sites) + list(other.call_sites)
        return out

    def dependency(self, kind: str, name: str) -> DependencyInfo | None:
        for d in self.dependencies:
            if d.kind == kind and d.name == name:
                return d
        return None


class IndexBudgetExceeded(RuntimeError):
    """Raised when the 10-minute index budget runs out between (or inside) phases."""
