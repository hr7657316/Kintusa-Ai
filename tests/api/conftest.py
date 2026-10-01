"""Fixtures for the API tests: a ``TestClient`` on ``create_app()``, committing row
factories (the app talks to Postgres through its own sessions, so rows must be committed),
a session-cookie minter and a webhook signer.

The payload fixtures under ``tests/api/fixtures`` are modelled on GitHub's real webhook
payloads; the factory defaults (installation 12345678, repo 186853002 ``acme/support-agent``)
line up with them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

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

FIXTURES = Path(__file__).parent / "fixtures"

INSTALLATION_ID = 12345678
REPO_GITHUB_ID = 186853002
REPO_FULL_NAME = "acme/support-agent"
STRIPE_SPEC = "https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json"
CHARGES_BODY = (
    "/paths/~1v1~1charges/post/requestBody/content/application~1x-www-form-urlencoded/schema"
)
REQUIRED_ADDED_OPS = [
    {
        "op": "required_added",
        "path": f"{CHARGES_BODY}/required",
        "before": ["amount"],
        "after": ["amount", "currency"],
    }
]


def load_payload(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))


# --------------------------------------------------------------------------------------
# App / client
# --------------------------------------------------------------------------------------


@pytest.fixture
def app(session, fake_github, fake_llm):
    from kintsua.api.app import create_app
    from kintsua.api.report import invalidate_cache
    from kintsua.api.webhooks import deliveries

    deliveries.clear()
    invalidate_cache()
    return create_app()


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# --------------------------------------------------------------------------------------
# Row factory
# --------------------------------------------------------------------------------------


class Factory:
    def __init__(self, session: Session):
        self.session = session
        self._n = 0

    def _next(self) -> int:
        self._n += 1
        return self._n

    def commit(self) -> None:
        self.session.commit()

    def refresh(self, *rows: Any) -> None:
        self.session.expire_all()
        for r in rows:
            self.session.refresh(r)

    # ---- tenancy ---------------------------------------------------------------
    def org(
        self,
        name: str = "acme",
        *,
        installation_id: int | None = None,
        plan: str = "free",
        **settings: Any,
    ) -> Org:
        org = Org(
            github_installation_id=installation_id
            if installation_id is not None
            else (INSTALLATION_ID if self._n == 0 else uuid.uuid4().int % 10**9),
            name=name,
            plan=plan,
            settings={"fix_mode": "issue_only", "auto_issue": False, **settings},
        )
        self._n += 1
        self.session.add(org)
        self.session.commit()
        return org

    def repo(
        self,
        org: Org,
        full_name: str = REPO_FULL_NAME,
        *,
        github_repo_id: int | None = None,
        config: dict[str, Any] | None = None,
        active: bool = True,
        default_branch: str = "main",
        private: bool = True,
    ) -> Repo:
        repo = Repo(
            org_id=org.id,
            github_repo_id=github_repo_id
            if github_repo_id is not None
            else (REPO_GITHUB_ID if full_name == REPO_FULL_NAME else uuid.uuid4().int % 10**9),
            full_name=full_name,
            default_branch=default_branch,
            config=dict(config or {}),
            active=active,
            private=private,
        )
        self.session.add(repo)
        self.session.commit()
        return repo

    # ---- sources / dependencies ------------------------------------------------
    def source(
        self,
        kind: str = "openapi",
        locator: str = STRIPE_SPEC,
        *,
        vendor: str | None = "stripe",
        display_name: str | None = None,
        active: bool = True,
        meta: dict[str, Any] | None = None,
    ) -> Source:
        src = Source(
            kind=kind,
            locator=locator,
            display_name=display_name or f"{vendor or kind} {kind}",
            vendor=vendor,
            active=active,
            meta=dict(meta or {}),
        )
        self.session.add(src)
        self.session.commit()
        return src

    def dependency(
        self,
        repo: Repo,
        source: Source | None = None,
        *,
        kind: str = "pypi",
        name: str = "stripe",
        meta: dict[str, Any] | None = None,
        discovered_at_runtime: bool = False,
    ) -> Dependency:
        m = {"vendor": source.vendor if source else "stripe", **(meta or {})}
        dep = Dependency(
            repo_id=repo.id,
            source_id=source.id if source else None,
            kind=kind,
            name=name,
            meta=m,
            discovered_at_runtime=discovered_at_runtime,
        )
        self.session.add(dep)
        self.session.commit()
        return dep

    def call_site(
        self,
        dep: Dependency,
        *,
        file_path: str = "app/billing.py",
        line: int = 42,
        symbol: str = "stripe.Charge.create",
        operation: str = "POST /v1/charges",
        args_shape: dict[str, Any] | None = None,
    ) -> CallSite:
        cs = CallSite(
            dependency_id=dep.id,
            file_path=file_path,
            line=line,
            symbol=symbol,
            operation=operation,
            args_shape=args_shape or {"keys": ["amount"]},
        )
        self.session.add(cs)
        self.session.commit()
        return cs

    # ---- events ----------------------------------------------------------------
    def change_event(
        self,
        source: Source,
        *,
        ops: list[dict[str, Any]] | None = None,
        from_version: str = "2026-08-27",
        to_version: str = "2026-09-30",
        text: str | None = None,
    ) -> ChangeEventRow:
        row = ChangeEventRow(
            source_id=source.id,
            diff={
                "source_id": str(source.id),
                "source_kind": source.kind,
                "from_version": from_version,
                "to_version": to_version,
                "ops": list(ops if ops is not None else REQUIRED_ADDED_OPS),
                "raw_diff_ref": "inline",
                "text": text,
            },
            detected_at=utcnow(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def classification(
        self,
        event: ChangeEventRow,
        *,
        severity: str = "breaking",
        category: str = "required_param",
        summary: str = "Stripe POST /v1/charges now requires `currency`",
        affected_paths: list[str] | None = None,
        migration_hint: str | None = "Pass currency='usd' to stripe.Charge.create",
        rule_hits: list[str] | None = None,
    ) -> Classification:
        cls = Classification(
            change_event_id=event.id,
            severity=severity,
            category=category,
            summary=summary,
            affected_paths=affected_paths if affected_paths is not None else ["POST /v1/charges"],
            migration_hint=migration_hint,
            rule_hits=rule_hits or ["required_added"],
        )
        self.session.add(cls)
        self.session.commit()
        return cls

    def impact(
        self,
        repo: Repo,
        event: ChangeEventRow,
        cls: Classification | None = None,
        *,
        status: str = "detected",
        matched_call_sites: list[dict[str, Any]] | None = None,
        **kw: Any,
    ) -> Impact:
        sites = (
            matched_call_sites
            if matched_call_sites is not None
            else [
                {
                    "call_site_id": str(uuid.uuid4()),
                    "file_path": "app/billing.py",
                    "line": 42,
                    "symbol": "stripe.Charge.create",
                    "operation": "POST /v1/charges",
                    "vendor": "stripe",
                }
            ]
        )
        impact = Impact(
            repo_id=repo.id,
            change_event_id=event.id,
            classification_id=cls.id if cls is not None else None,
            status=status,
            matched_call_sites=sites,
            **kw,
        )
        self.session.add(impact)
        self.session.commit()
        return impact

    def breaking_impact(
        self, repo: Repo, *, status: str = "detected", source: Source | None = None, **kw: Any
    ) -> tuple[Impact, ChangeEventRow, Classification]:
        src = source or self.source()
        event = self.change_event(src)
        cls = self.classification(event)
        return self.impact(repo, event, cls, status=status, **kw), event, cls

    def runtime_event(self, repo: Repo, **kw: Any) -> RuntimeEvent:
        ev = RuntimeEvent(
            repo_id=repo.id,
            sdk_key_hash=kw.pop("sdk_key_hash", "0" * 64),
            tool_name=kw.pop("tool_name", "get_weather"),
            outcome=kw.pop("outcome", "error"),
            sample=kw.pop("sample", {"kind": "mcp"}),
            **kw,
        )
        self.session.add(ev)
        self.session.commit()
        return ev

    # ---- keys ------------------------------------------------------------------
    def sdk_key(self, org: Org) -> str:
        from kintsua.api.keys import rotate_sdk_key

        key = rotate_sdk_key(self.session, org)
        self.session.commit()
        return key

    # ---- queries ---------------------------------------------------------------
    def jobs(self, kind: str | None = None, status: str | None = "queued") -> list[Job]:
        from sqlalchemy import select

        self.session.expire_all()
        stmt = select(Job).order_by(Job.created_at)
        if kind:
            stmt = stmt.where(Job.kind == kind)
        if status:
            stmt = stmt.where(Job.status == status)
        return list(self.session.scalars(stmt))


@pytest.fixture
def factory(session) -> Factory:
    return Factory(session)


# --------------------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------------------


def mint_session(
    client: TestClient,
    installation_ids: list[int] | tuple[int, ...] = (INSTALLATION_ID,),
    *,
    login: str = "octocat",
    user_id: int = 1,
) -> str:
    """Set a signed session cookie on the client and return the token."""
    from kintsua.api.deps import SESSION_COOKIE, sign_session
    from kintsua.api.schemas import SessionUser

    token = sign_session(
        SessionUser(login=login, id=user_id, installation_ids=list(installation_ids))
    )
    client.cookies.set(SESSION_COOKIE, token)
    return token


def webhook_secret() -> str:
    from kintsua.core.config import get_settings

    return get_settings().github_webhook_secret or ""


def sign(body: bytes, secret: str | None = None) -> str:
    return (
        "sha256="
        + hmac.new((secret or webhook_secret()).encode(), body, hashlib.sha256).hexdigest()
    )


def post_webhook(
    client: TestClient,
    event: str,
    payload: dict[str, Any],
    *,
    delivery: str | None = None,
    signature: str | None = None,
    secret: str | None = None,
):
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery or uuid.uuid4().hex,
        "X-Hub-Signature-256": signature or sign(body, secret),
        "User-Agent": "GitHub-Hookshot/abc123",
    }
    return client.post("/webhooks/github", content=body, headers=headers)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}
