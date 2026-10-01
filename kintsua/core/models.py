"""SQLAlchemy 2 models: the eleven tables from the build spec (plus ``subscriber``).

Every event table is immutable; state lives on ``impact`` and ``job``. All ids are UUIDs,
all timestamps are timestamptz, and every table carries ``created_at``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from kintsua.core.util import utcnow

# ---- enums kept as plain strings (checked in code, documented here) -----------------
SOURCE_KINDS = ("openapi", "mcp", "pypi", "npm", "changelog")
SEVERITIES = ("breaking", "degraded", "safe", "unknown")
IMPACT_STATUSES = ("detected", "issued", "fixing", "pr_open", "merged", "dismissed", "failed")
RUNTIME_OUTCOMES = ("ok", "error", "schema_mismatch")
JOB_STATUSES = ("queued", "running", "done", "failed")
DEPENDENCY_KINDS = ("openapi", "mcp", "pypi", "npm", "http")
PLANS = ("free", "pro")
FIX_MODES = ("pr", "issue_only")


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


def _ts(nullable: bool = True) -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


# ---- tenancy -------------------------------------------------------------------------


class Org(Base):
    __tablename__ = "org"

    id: Mapped[uuid.UUID] = _uuid_pk()
    github_installation_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plan: Mapped[str] = mapped_column(String(16), nullable=False, default="free")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255))
    # account type ("Organization" | "User") and the GitHub login, for the settings page
    account_type: Mapped[str | None] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # org-wide defaults: {"fix_mode": "issue_only", "auto_issue": false}
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # SDK keys are per org, shown once, stored hashed
    sdk_key_hash: Mapped[str | None] = mapped_column(String(64))
    sdk_key_prefix: Mapped[str | None] = mapped_column(String(16))
    sdk_key_created_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _created_at()

    repos: Mapped[list[Repo]] = relationship(back_populates="org")


class Repo(Base):
    __tablename__ = "repo"
    __table_args__ = (UniqueConstraint("github_repo_id", name="uq_repo_github_repo_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("org.id", ondelete="CASCADE"), nullable=False
    )
    github_repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    last_indexed_sha: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # parsed .kintsua.yml ({"sources": [...], "mute": [...], "fix": {"mode": "pr", "base": "main"}})
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    install_issue_number: Mapped[int | None] = mapped_column(Integer)
    indexed_at: Mapped[datetime | None] = _ts()
    last_index_error: Mapped[str | None] = mapped_column(Text)
    # last index run: {"files_parsed", "parse_errors", "head_sha", "reason", "phases", "duration_s"}
    index_stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()

    org: Mapped[Org] = relationship(back_populates="repos")
    dependencies: Mapped[list[Dependency]] = relationship(back_populates="repo")


# ---- what we watch -------------------------------------------------------------------


class Source(Base):
    __tablename__ = "source"
    __table_args__ = (UniqueConstraint("kind", "locator", name="uq_source_kind_locator"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # openapi | mcp | pypi | npm | changelog
    # spec URL | MCP server URL or stdio command | package name | page URL
    locator: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(128))  # "stripe", "slack" ... for grouping
    poll_interval_s: Mapped[int] = mapped_column(Integer, nullable=False, default=6 * 3600)
    last_polled_at: Mapped[datetime | None] = _ts()
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backoff_until: Mapped[datetime | None] = _ts()
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # adapter hints: {"format": "openapi"|"discovery", "headers": {...}, "auth_env": "X",
    #                 "transport": "http"|"stdio", "args": [...], "env": {...}}
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()

    snapshots: Mapped[list[Snapshot]] = relationship(back_populates="source")


class Snapshot(Base):
    __tablename__ = "snapshot"
    __table_args__ = (Index("ix_snapshot_source_captured", "source_id", text("captured_at DESC")),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source.id", ondelete="CASCADE"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str | None] = mapped_column(String(255))
    # {"kind": "inline", "body": {...}} if < 256 KB else {"kind": "s3", "key": "..."}
    storage_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    created_at: Mapped[datetime] = _created_at()

    source: Mapped[Source] = relationship(back_populates="snapshots")


class ChangeEventRow(Base):
    __tablename__ = "change_event"
    __table_args__ = (
        UniqueConstraint("from_snapshot_id", "to_snapshot_id", name="uq_change_event_pair"),
        Index("ix_change_event_source_detected", "source_id", text("detected_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source.id", ondelete="CASCADE"), nullable=False
    )
    from_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("snapshot.id", ondelete="SET NULL")
    )
    to_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("snapshot.id", ondelete="SET NULL")
    )
    # the ChangeEvent pydantic model, dumped: {source_kind, from_version, to_version, ops, raw_diff_ref, text}
    diff: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    created_at: Mapped[datetime] = _created_at()

    classification: Mapped[Classification | None] = relationship(
        back_populates="change_event", uselist=False
    )


class Classification(Base):
    __tablename__ = "classification"

    id: Mapped[uuid.UUID] = _uuid_pk()
    change_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_event.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # breaking | degraded | safe | unknown
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    affected_paths: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    migration_hint: Mapped[str | None] = mapped_column(Text)
    rule_hits: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    llm_model: Mapped[str | None] = mapped_column(String(128))
    llm_confidence: Mapped[float | None] = mapped_column(Float)
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    needs_human: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created_at()

    change_event: Mapped[ChangeEventRow] = relationship(back_populates="classification")


# ---- what customers depend on --------------------------------------------------------


class Dependency(Base):
    __tablename__ = "dependency"
    __table_args__ = (
        UniqueConstraint("repo_id", "kind", "name", name="uq_dependency_repo_kind_name"),
        Index("ix_dependency_source", "source_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repo.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # openapi | mcp | pypi | npm | http
    name: Mapped[str] = mapped_column(
        String(512), nullable=False
    )  # package name, host, or MCP locator
    version_constraint: Mapped[str | None] = mapped_column(String(255))
    first_seen_sha: Mapped[str | None] = mapped_column(String(64))
    discovered_at_runtime: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # {"vendor": "stripe", "host": "api.stripe.com", "config_file": ".mcp.json", "language": "python"}
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()

    repo: Mapped[Repo] = relationship(back_populates="dependencies")
    source: Mapped[Source | None] = relationship()
    call_sites: Mapped[list[CallSite]] = relationship(back_populates="dependency")


class CallSite(Base):
    __tablename__ = "call_site"
    __table_args__ = (Index("ix_call_site_dependency_operation", "dependency_id", "operation"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    dependency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dependency.id", ondelete="CASCADE"), nullable=False
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    line: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    symbol: Mapped[str | None] = mapped_column(
        Text
    )  # stripe.Charge.create, slack_client.chat_postMessage
    # HTTP method+path ("POST /v1/charges"), tool name, or SDK method
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    # keyword-argument names / JSON keys the call passes: {"keys": ["amount", "currency"]}
    args_shape: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    discovered_at_runtime: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_seen_sha: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()

    dependency: Mapped[Dependency] = relationship(back_populates="call_sites")


# ---- what we do about it -------------------------------------------------------------


class Impact(Base):
    __tablename__ = "impact"
    __table_args__ = (
        UniqueConstraint("repo_id", "change_event_id", name="uq_impact_repo_change_event"),
        Index("ix_impact_repo_status", "repo_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repo.id", ondelete="CASCADE"), nullable=False
    )
    change_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("change_event.id", ondelete="CASCADE"), nullable=False
    )
    classification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("classification.id", ondelete="SET NULL")
    )
    # detected | issued | fixing | pr_open | merged | dismissed | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="detected")
    github_issue_number: Mapped[int | None] = mapped_column(Integer)
    github_pr_number: Mapped[int | None] = mapped_column(Integer)
    fix_job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    fix_reason: Mapped[str | None] = mapped_column(Text)  # why the fixer refused / failed
    pr_outcome: Mapped[str | None] = mapped_column(
        String(32)
    )  # merged_as_is | merged_edited | closed
    pr_branch: Mapped[str | None] = mapped_column(String(255))
    ci_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    runtime_error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # which call sites matched: [{"call_site_id": ..., "file_path": ..., "line": ..., "operation": ...}]
    matched_call_sites: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _created_at()
    resolved_at: Mapped[datetime | None] = _ts()

    repo: Mapped[Repo] = relationship()
    change_event: Mapped[ChangeEventRow] = relationship()
    classification: Mapped[Classification | None] = relationship()


class RuntimeEvent(Base):
    __tablename__ = "runtime_event"
    __table_args__ = (
        Index(
            "ix_runtime_event_repo_tool_observed", "repo_id", "tool_name", text("observed_at DESC")
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repo.id", ondelete="CASCADE"), nullable=False
    )
    sdk_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # MCP tool, or "GET api.host/path" for HTTP
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | error | schema_mismatch
    error_class: Mapped[str | None] = mapped_column(String(255))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # keys only, never values; or the schema diff for a mismatch
    sample: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    created_at: Mapped[datetime] = _created_at()


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        Index("ix_job_queued_run_after", "run_after", postgresql_where=text("status = 'queued'")),
        Index(
            "uq_job_dedupe_key_queued",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'queued' AND dedupe_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    locked_at: Mapped[datetime | None] = _ts()
    finished_at: Mapped[datetime | None] = _ts()
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created_at()


class Subscriber(Base):
    """Email subscribers to the public weekly drift report."""

    __tablename__ = "subscriber"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()


ALL_TABLES = [
    Org,
    Repo,
    Source,
    Snapshot,
    ChangeEventRow,
    Classification,
    Dependency,
    CallSite,
    Impact,
    RuntimeEvent,
    Job,
    Subscriber,
]
