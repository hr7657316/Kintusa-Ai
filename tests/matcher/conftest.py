"""Row factories for the matcher tests (Postgres via the shared ``session`` fixture)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Classification,
    Dependency,
    Impact,
    Job,
    Org,
    Repo,
    RuntimeEvent,
    Source,
)
from kintsua.core.util import utcnow

CHARGES_BODY = (
    "/paths/~1v1~1charges/post/requestBody/content/application~1x-www-form-urlencoded/schema"
)
STRIPE_SPEC = "https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json"

REQUIRED_ADDED_OPS = [
    {
        "op": "required_added",
        "path": f"{CHARGES_BODY}/required",
        "before": ["amount"],
        "after": ["amount", "currency"],
    }
]


class Factory:
    def __init__(self, session: Session):
        self.session = session
        self._n = 0

    def _next(self) -> int:
        self._n += 1
        return self._n

    # ---- tenancy ---------------------------------------------------------------
    def org(self, name: str = "acme", **settings: Any) -> Org:
        org = Org(
            github_installation_id=uuid.uuid4().int % 10**9,
            name=name,
            settings=dict(settings),
        )
        self.session.add(org)
        self.session.flush()
        return org

    def repo(
        self,
        org: Org,
        full_name: str = "acme/support-agent",
        *,
        config: dict[str, Any] | None = None,
        active: bool = True,
        default_branch: str = "main",
    ) -> Repo:
        repo = Repo(
            org_id=org.id,
            github_repo_id=uuid.uuid4().int % 10**9,
            full_name=full_name,
            default_branch=default_branch,
            config=dict(config or {}),
            active=active,
        )
        self.session.add(repo)
        self.session.flush()
        return repo

    # ---- sources / deps ------------------------------------------------------------
    def source(
        self,
        kind: str = "openapi",
        locator: str = STRIPE_SPEC,
        *,
        display_name: str = "Stripe API",
        vendor: str | None = "stripe",
        meta: dict[str, Any] | None = None,
    ) -> Source:
        row = self.session.scalar(
            select(Source).where(Source.kind == kind, Source.locator == locator)
        )
        if row is not None:
            return row
        src = Source(
            kind=kind,
            locator=locator,
            display_name=display_name,
            vendor=vendor,
            meta=dict(meta or {}),
        )
        self.session.add(src)
        self.session.flush()
        return src

    def dependency(
        self,
        repo: Repo,
        *,
        source: Source | None = None,
        kind: str = "http",
        name: str = "api.stripe.com",
        meta: dict[str, Any] | None = None,
        runtime: bool = False,
    ) -> Dependency:
        dep = Dependency(
            repo_id=repo.id,
            source_id=source.id if source is not None else None,
            kind=kind,
            name=name,
            meta=dict(meta or {}),
            discovered_at_runtime=runtime,
        )
        self.session.add(dep)
        self.session.flush()
        return dep

    def call_site(
        self,
        dep: Dependency,
        operation: str,
        *,
        file_path: str = "agent.py",
        line: int | None = None,
        symbol: str | None = None,
        args_shape: dict[str, Any] | None = None,
        runtime: bool = False,
    ) -> CallSite:
        cs = CallSite(
            dependency_id=dep.id,
            file_path=file_path,
            line=line if line is not None else 10 + self._next(),
            symbol=symbol or operation,
            operation=operation,
            args_shape=dict(args_shape or {}),
            discovered_at_runtime=runtime,
        )
        self.session.add(cs)
        self.session.flush()
        return cs

    # ---- events -------------------------------------------------------------------
    def event(
        self,
        source: Source,
        ops: list[dict[str, Any]] | None = None,
        *,
        from_version: str | None = "2024-06-20",
        to_version: str | None = "2024-09-30",
        text: str | None = None,
        source_kind: str | None = None,
        detected_at: datetime | None = None,
    ) -> ChangeEventRow:
        row = ChangeEventRow(
            source_id=source.id,
            diff={
                "source_kind": source_kind or source.kind,
                "from_version": from_version,
                "to_version": to_version,
                "ops": list(ops if ops is not None else REQUIRED_ADDED_OPS),
                "raw_diff_ref": "inline",
                "text": text,
            },
            detected_at=detected_at or utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def classification(
        self,
        event: ChangeEventRow,
        *,
        severity: str = "breaking",
        category: str = "required_param",
        summary: str = "POST /v1/charges now requires currency",
        affected_paths: list[str] | None = None,
        rule_hits: list[str] | None = None,
        migration_hint: str | None = "Pass currency='usd' to stripe.Charge.create",
    ) -> Classification:
        cls = Classification(
            change_event_id=event.id,
            severity=severity,
            category=category,
            summary=summary,
            affected_paths=list(
                affected_paths if affected_paths is not None else ["POST /v1/charges"]
            ),
            rule_hits=list(rule_hits if rule_hits is not None else ["required_added"]),
            migration_hint=migration_hint,
        )
        self.session.add(cls)
        self.session.flush()
        return cls

    def runtime_event(
        self,
        repo: Repo,
        tool_name: str,
        *,
        outcome: str = "ok",
        observed_at: datetime | None = None,
        fingerprint: str | None = None,
        sample: dict[str, Any] | None = None,
        error_class: str | None = None,
        latency_ms: int = 12,
    ) -> RuntimeEvent:
        ev = RuntimeEvent(
            repo_id=repo.id,
            sdk_key_hash="k" * 64,
            tool_name=tool_name,
            schema_fingerprint=fingerprint,
            outcome=outcome,
            error_class=error_class,
            latency_ms=latency_ms,
            sample=sample,
            observed_at=observed_at or utcnow(),
        )
        self.session.add(ev)
        self.session.flush()
        return ev

    # ---- lookups ------------------------------------------------------------------
    def impacts(self, repo: Repo | None = None) -> list[Impact]:
        stmt = select(Impact).order_by(Impact.created_at)
        if repo is not None:
            stmt = stmt.where(Impact.repo_id == repo.id)
        return list(self.session.scalars(stmt))

    def jobs(self, kind: str | None = None) -> list[Job]:
        stmt = select(Job).order_by(Job.created_at)
        if kind:
            stmt = stmt.where(Job.kind == kind)
        return list(self.session.scalars(stmt))


@pytest.fixture
def f(session: Session) -> Factory:
    return Factory(session)


@pytest.fixture
def stripe_http_world(f: Factory):
    """An org + repo with a raw-HTTP Stripe dependency and two call sites, and a breaking
    change event on the Stripe spec that affects ``POST /v1/charges/{charge}``."""
    org = f.org()
    repo = f.repo(org)
    spec = f.source()
    dep = f.dependency(
        repo, source=spec, kind="http", name="api.stripe.com", meta={"vendor": "stripe"}
    )
    hit = f.call_site(
        dep,
        "POST /v1/charges/{id}",
        file_path="billing/charge.py",
        line=42,
        symbol="httpx.Client.post",
    )
    miss = f.call_site(
        dep, "GET /v1/customers", file_path="crm/sync.py", line=7, symbol="httpx.Client.get"
    )
    event = f.event(spec)
    cls = f.classification(event, affected_paths=["POST /v1/charges/{charge}"])
    f.session.commit()
    return {
        "org": org,
        "repo": repo,
        "spec": spec,
        "dep": dep,
        "hit": hit,
        "miss": miss,
        "event": event,
        "cls": cls,
    }
