"""The dependency map: a Pydantic view over ``dependency`` / ``call_site`` rows, and the
install-issue renderer ("Kintsua is watching 11 of your 14 external dependencies").

The same model backs ``GET /v1/repos/{id}/dependencies`` and the settings page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kintsua.core.models import CallSite, Dependency, Repo, Source
from kintsua.core.util import utcnow
from kintsua.indexer.config import effective_fix_mode
from kintsua.indexer.schema import ExtractStats

KIND_LABEL = {
    "pypi": "Python package",
    "npm": "npm package",
    "http": "HTTP API",
    "mcp": "MCP server",
    "openapi": "OpenAPI spec",
    "changelog": "Changelog",
}


class DependencyEntry(BaseModel):
    id: str | None = None
    kind: str
    name: str
    vendor: str | None = None
    display_name: str | None = None
    watched: bool = False
    source_kind: str | None = None
    source_id: str | None = None
    openapi_source_id: str | None = None
    changelog_source_id: str | None = None
    call_site_count: int = 0
    discovered_at_runtime: bool = False
    config_file: str | None = None
    dev: bool = False
    external: bool = True
    language: str | None = None
    version_constraint: str | None = None
    transport: str | None = None
    server_name: str | None = None
    first_seen_sha: str | None = None
    reason: str | None = None  # why it is unwatched, when the indexer refused its source


class DependencyTotals(BaseModel):
    dependencies: int = 0  # every row
    external: int = 0  # rows that are an external API / server / vendor SDK
    library_packages: int = 0  # declared packages that are not external (shown collapsed)
    call_sites: int = 0
    watched: int = 0
    unwatched: int = 0  # external and not watched
    runtime_discovered: int = 0
    files_parsed: int | None = None
    parse_errors: int | None = None


class DependencyMap(BaseModel):
    repo: str
    repo_id: str | None = None
    default_branch: str = "main"
    generated_at: datetime = Field(default_factory=utcnow)
    head_sha: str | None = None
    indexed_at: datetime | None = None
    dependencies: list[DependencyEntry] = Field(default_factory=list)
    totals: DependencyTotals = Field(default_factory=DependencyTotals)
    fix_mode: str = "issue_only"
    config_error: str | None = None
    index_error: str | None = None
    extract_errors: list[str] = Field(default_factory=list)

    @property
    def external(self) -> list[DependencyEntry]:
        return [d for d in self.dependencies if d.external]

    @property
    def library(self) -> list[DependencyEntry]:
        return [d for d in self.dependencies if not d.external]


def is_external(kind: str, vendor: str | None, meta: dict[str, Any] | None) -> bool:
    """Vendor SDKs, frameworks we watch, MCP servers, HTTP hosts and configured sources are
    external; a declared package nobody maps to a vendor is library code."""
    if kind in ("http", "mcp", "openapi", "changelog"):
        return True
    meta = meta or {}
    return bool(vendor) or bool(meta.get("mcp_server_package")) or bool(meta.get("sdk_kind"))


def _entry_sort_key(e: DependencyEntry) -> tuple[Any, ...]:
    order = {"mcp": 0, "http": 1, "pypi": 2, "npm": 2, "openapi": 3, "changelog": 3}
    return (not e.external, not e.watched, order.get(e.kind, 9), -e.call_site_count, e.name.lower())


def dependency_map(
    session: Session,
    repo: Repo,
    *,
    stats: ExtractStats | None = None,
    config_error: str | None = None,
    org_settings: dict[str, Any] | None = None,
) -> DependencyMap:
    """Build the map from the tables (``stats`` adds files_parsed / parse_errors when the
    caller has just indexed)."""
    deps = list(
        session.scalars(
            select(Dependency).where(Dependency.repo_id == repo.id).order_by(Dependency.name)
        )
    )
    counts = dict(
        session.execute(
            select(CallSite.dependency_id, func.count(CallSite.id))
            .join(Dependency, Dependency.id == CallSite.dependency_id)
            .where(Dependency.repo_id == repo.id)
            .group_by(CallSite.dependency_id)
        ).all()
    )
    source_ids = {d.source_id for d in deps if d.source_id is not None}
    sources: dict[Any, Source] = {}
    if source_ids:
        for s in session.scalars(select(Source).where(Source.id.in_(source_ids))):
            sources[s.id] = s
    entries: list[DependencyEntry] = []
    for d in deps:
        meta = d.meta or {}
        src = sources.get(d.source_id) if d.source_id is not None else None
        entries.append(
            DependencyEntry(
                id=str(d.id),
                kind=d.kind,
                name=d.name,
                vendor=meta.get("vendor") or (src.vendor if src else None),
                display_name=meta.get("display_name"),
                watched=src is not None and bool(src.active),
                source_kind=src.kind if src else None,
                source_id=str(src.id) if src else None,
                openapi_source_id=meta.get("openapi_source_id"),
                changelog_source_id=meta.get("changelog_source_id"),
                call_site_count=int(counts.get(d.id, 0)),
                discovered_at_runtime=bool(d.discovered_at_runtime),
                config_file=meta.get("config_file"),
                dev=bool(meta.get("dev")),
                external=is_external(
                    d.kind, meta.get("vendor") or (src.vendor if src else None), meta
                ),
                language=meta.get("language"),
                version_constraint=d.version_constraint,
                transport=meta.get("transport"),
                server_name=meta.get("server_name"),
                first_seen_sha=d.first_seen_sha,
                reason=meta.get("reason") if src is None else None,
            )
        )
    entries.sort(key=_entry_sort_key)
    external = [e for e in entries if e.external]
    totals = DependencyTotals(
        dependencies=len(entries),
        external=len(external),
        library_packages=len(entries) - len(external),
        call_sites=sum(e.call_site_count for e in entries),
        watched=sum(1 for e in external if e.watched),
        unwatched=sum(1 for e in external if not e.watched),
        runtime_discovered=sum(1 for e in entries if e.discovered_at_runtime),
        files_parsed=stats.files_parsed if stats else (repo.index_stats or {}).get("files_parsed"),
        parse_errors=stats.parse_errors if stats else (repo.index_stats or {}).get("parse_errors"),
    )
    if org_settings is None:
        try:
            org_settings = dict(repo.org.settings or {}) if repo.org is not None else {}
        except Exception:  # noqa: BLE001 - detached instance
            org_settings = {}
    return DependencyMap(
        repo=repo.full_name,
        repo_id=str(repo.id),
        default_branch=repo.default_branch,
        head_sha=repo.last_indexed_sha,
        indexed_at=repo.indexed_at,
        dependencies=entries,
        totals=totals,
        fix_mode=effective_fix_mode(repo.config, org_settings),
        config_error=config_error,
        index_error=repo.last_index_error,
        extract_errors=list(stats.errors) if stats else [],
    )


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def install_issue_title(depmap: DependencyMap) -> str:
    t = depmap.totals
    return f"Kintsua is watching {t.watched} of your {t.external} external dependencies"


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _dependency_cell(e: DependencyEntry) -> str:
    if e.kind == "mcp":
        label = f"`{_md(e.name)}`"
        if e.server_name and e.server_name != e.name:
            label = f"**{_md(e.server_name)}** {label}"
        return label
    if e.kind in ("openapi", "changelog"):
        return f"<{e.name}>"
    name = f"`{_md(e.name)}`"
    if e.display_name and e.vendor and e.display_name != e.name:
        name = f"{name} ({_md(e.display_name)})"
    if e.dev:
        name += " _(dev)_"
    return name


def _status_cell(e: DependencyEntry) -> str:
    if e.watched:
        via = e.source_kind or "source"
        extra = []
        if e.openapi_source_id:
            extra.append("openapi")
        if e.changelog_source_id:
            extra.append("changelog")
        if e.transport and e.kind == "mcp":
            extra.append(e.transport)
        text = f"watched ({via}" + (" + " + " + ".join(extra) if extra else "") + ")"
        prefix = "🟢"
    else:
        text = "unwatched"
        prefix = "⚪"
        if e.reason:
            text += f" — {_md(e.reason)}"
        elif e.kind == "http":
            text += " — no spec or changelog known for this host"
        elif e.kind == "mcp" and e.transport == "stdio" and e.source_id:
            text += (
                " — Kintsua does not run commands from a repo; use the server's http(s) URL "
                "or `kintsua watch --push` in CI"
            )
        elif e.kind == "mcp":
            text += " — disabled in config" if e.transport is None else ""
    if e.discovered_at_runtime:
        text += ", seen at runtime"
    return f"{prefix} {text}"


def render_dependency_table(depmap: DependencyMap) -> str:
    rows = ["| Dependency | Kind | Watch status | Call sites |", "|---|---|---|---:|"]
    for e in depmap.external:
        rows.append(
            f"| {_dependency_cell(e)} | {KIND_LABEL.get(e.kind, e.kind)} | {_status_cell(e)} | {e.call_site_count} |"
        )
    if len(rows) == 2:
        rows.append("| _nothing found yet_ | | | |")
    return "\n".join(rows)


def render_install_issue(repo: Repo, depmap: DependencyMap) -> tuple[str, str]:
    """(title, body) for the install issue; the body is refreshed on every re-index."""
    t = depmap.totals
    sha = (depmap.head_sha or "")[:7]
    when = (depmap.indexed_at or depmap.generated_at).strftime("%Y-%m-%d %H:%M UTC")
    files = f" across {t.files_parsed} files" if t.files_parsed is not None else ""
    lines = [
        f"Kintsua indexed `{repo.full_name}` at `{sha or 'HEAD'}` ({when}) and found "
        f"**{t.external} external dependencies** and **{t.call_sites} call sites**{files}: "
        f"**{t.watched} watched**, **{t.unwatched} unwatched**.",
        "",
        "Kintsua watches every mapped dependency daily and opens an issue here within an hour of a "
        "breaking change upstream, with the files and lines in this repo that call it.",
        "",
        "## Dependency map",
        "",
        render_dependency_table(depmap),
    ]
    if t.parse_errors:
        lines += [
            "",
            f"_{t.parse_errors} file(s) could not be fully parsed and were indexed partially._",
        ]
    if t.runtime_discovered:
        lines += [
            "",
            f"_{t.runtime_discovered} dependenc{'y was' if t.runtime_discovered == 1 else 'ies were'} first seen by the runtime SDK rather than in the code._",
        ]
    library = depmap.library
    if library:
        names = ", ".join(f"`{e.name}`" for e in library[:60])
        more = f" and {len(library) - 60} more" if len(library) > 60 else ""
        lines += [
            "",
            "<details>",
            f"<summary>{len(library)} other declared package(s) that are not external APIs</summary>",
            "",
            names + more,
            "",
            "</details>",
        ]
    lines += [
        "",
        "## Unwatched dependencies",
        "",
        "Unwatched rows are hosts and packages Kintsua has no public spec or changelog for. Point "
        "it at one by adding a `.kintsua.yml` at the root of this repo (optional `hosts` ties a "
        "spec to the API host your code calls):",
        "",
        "```yaml",
        "sources:",
        "  - kind: openapi",
        "    url: https://api.vendor.com/openapi.json",
        "    hosts: [api.vendor.com]",
        "  - kind: changelog",
        "    url: https://vendor.com/changelog",
        "mute:",
        "  - stripe:/v1/legacy/*",
        "fix:",
        "  mode: pr          # pr | issue_only",
        "  base: " + (depmap.default_branch or "main"),
        "```",
        "",
        "## Fix mode",
        "",
        f"Fix mode for this repo is **`{depmap.fix_mode}`**"
        + (
            ": Kintsua opens issues only. Set `fix.mode: pr` in `.kintsua.yml` and it will also "
            "open a tested fix PR for renamed fields, new required parameters and SDK major bumps."
            if depmap.fix_mode == "issue_only"
            else ": Kintsua opens a tested fix PR after each issue. Set `fix.mode: issue_only` in "
            "`.kintsua.yml` to get issues only."
        ),
    ]
    if depmap.config_error:
        lines += [
            "",
            "## Configuration error",
            "",
            f"`.kintsua.yml` could not be parsed and the previous configuration was kept: {_md(depmap.config_error)}",
        ]
    lines += [
        "",
        "## Privacy",
        "",
        "Kintsua clones this repo into an ephemeral sandbox on install and on every push to "
        f"`{depmap.default_branch}`, keeps only file paths, line numbers, symbol names and argument "
        "*names* (never values, never source text), and deletes the clone when indexing finishes. "
        "Delete everything with `DELETE /v1/repos/{id}/data`.",
        "",
        f"_This issue is updated on every push to `{depmap.default_branch}`. Opened by Kintsua._",
    ]
    return install_issue_title(depmap), "\n".join(lines)


__all__ = [
    "DependencyEntry",
    "DependencyMap",
    "DependencyTotals",
    "dependency_map",
    "install_issue_title",
    "is_external",
    "render_dependency_table",
    "render_install_issue",
]
