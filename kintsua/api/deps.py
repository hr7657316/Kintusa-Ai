"""FastAPI dependencies and helpers shared by the routers.

* ``get_db`` (re-exported from ``kintsua.core.db``): one session per request.
* Signed session cookies (``itsdangerous`` ``URLSafeTimedSerializer`` with
  ``settings.session_secret``): ``current_session_user`` / ``require_user``.
* ``org_for_sdk_key``: ``Authorization: Bearer kts_...`` -> ``Org`` (hash lookup), 401 otherwise.
* ``repo_access``: a repo id the signed-in user may touch (404 when it is outside the
  installations the session is scoped to).
* ``render`` / ``read_body`` / ``is_htmx``: Jinja2 rendering and JSON-or-form request bodies
  so the same endpoints serve the HTMX settings page and plain JSON clients.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kintsua.api.schemas import SessionUser
from kintsua.core.config import get_settings
from kintsua.core.db import get_db
from kintsua.core.models import Org, Repo
from kintsua.core.util import sha256_hex

log = logging.getLogger(__name__)

SESSION_COOKIE = "kintsua_session"
OAUTH_STATE_COOKIE = "kintsua_oauth_state"
SESSION_SALT = "kintsua.session"
OAUTH_SALT = "kintsua.oauth-state"
MANIFEST_SALT = "kintsua.github-manifest"
SDK_KEY_PREFIX = "kts_"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------------------
# Session cookies
# --------------------------------------------------------------------------------------


def serializer(salt: str = SESSION_SALT) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt=salt)


def sign_session(user: SessionUser) -> str:
    return serializer().dumps(user.model_dump())


def load_session(token: str | None, *, max_age_s: int | None = None) -> SessionUser | None:
    if not token:
        return None
    try:
        data = serializer().loads(token, max_age=max_age_s or get_settings().session_max_age_s)
    except (BadSignature, SignatureExpired):
        return None
    try:
        return SessionUser.model_validate(data)
    except ValidationError:
        return None


def cookie_secure() -> bool:
    return get_settings().base_url.lower().startswith("https://")


def set_session_cookie(response: Response, user: SessionUser) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(user),
        max_age=get_settings().session_max_age_s,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_session_user(request: Request) -> SessionUser | None:
    """The signed-in user, or None. Never raises."""
    return load_session(request.cookies.get(SESSION_COOKIE))


def _same_origin_or_absent(request: Request) -> None:
    """Cookie-authenticated writes must come from our own origin when the browser says
    where they came from (SameSite=Lax covers the rest)."""
    if request.method not in MUTATING_METHODS:
        return
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return
    origin_host = urlsplit(origin).netloc.lower()
    request_host = (request.headers.get("host") or "").lower()
    base_host = urlsplit(get_settings().base_url).netloc.lower()
    if origin_host and origin_host not in (request_host, base_host):
        raise HTTPException(status_code=403, detail="cross-origin request refused")


def require_user(
    request: Request, user: SessionUser | None = Depends(current_session_user)
) -> SessionUser:
    if user is None:
        raise HTTPException(status_code=401, detail="sign in with GitHub first")
    _same_origin_or_absent(request)
    return user


# --------------------------------------------------------------------------------------
# SDK keys
# --------------------------------------------------------------------------------------


def org_for_sdk_key(request: Request, session: Session = Depends(get_db)) -> Org:
    auth = request.headers.get("authorization") or ""
    scheme, _, key = auth.partition(" ")
    key = key.strip()
    if scheme.lower() != "bearer" or not key.startswith(SDK_KEY_PREFIX) or len(key) < 16:
        raise HTTPException(
            status_code=401,
            detail="an SDK key is required: Authorization: Bearer kts_...",
            headers={"WWW-Authenticate": "Bearer"},
        )
    org = session.scalar(select(Org).where(Org.sdk_key_hash == sha256_hex(key)))
    if org is None or not org.active:
        raise HTTPException(
            status_code=401, detail="invalid SDK key", headers={"WWW-Authenticate": "Bearer"}
        )
    return org


# --------------------------------------------------------------------------------------
# Orgs and repos the session may see
# --------------------------------------------------------------------------------------


def user_orgs(session: Session, user: SessionUser) -> list[Org]:
    if not user.installation_ids:
        return []
    return list(
        session.scalars(
            select(Org)
            .where(Org.github_installation_id.in_(list(user.installation_ids)))
            .order_by(Org.name)
        )
    )


def user_repos(session: Session, user: SessionUser, *, active_only: bool = False) -> list[Repo]:
    if not user.installation_ids:
        return []
    stmt = (
        select(Repo)
        .join(Org, Org.id == Repo.org_id)
        .where(Org.github_installation_id.in_(list(user.installation_ids)))
        .order_by(Repo.full_name)
    )
    if active_only:
        stmt = stmt.where(Repo.active.is_(True))
    return list(session.scalars(stmt))


def org_access(session: Session, user: SessionUser, org_id: str | UUID) -> Org:
    try:
        oid = UUID(str(org_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="org not found")
    org = session.get(Org, oid)
    if org is None or org.github_installation_id not in user.installation_ids:
        raise HTTPException(status_code=404, detail="org not found")
    return org


def pick_org(session: Session, user: SessionUser, org_id: str | UUID | None) -> Org:
    """The org a session-level action applies to: the given one (must be visible to the
    user), or the user's only org when none is given."""
    if org_id:
        return org_access(session, user, org_id)
    orgs = user_orgs(session, user)
    if not orgs:
        raise HTTPException(status_code=404, detail="no installation is visible to this session")
    if len(orgs) > 1:
        raise HTTPException(
            status_code=400, detail="several orgs are visible to this session; pass org_id"
        )
    return orgs[0]


def repo_access(
    repo_id: str,
    user: SessionUser = Depends(require_user),
    session: Session = Depends(get_db),
) -> Repo:
    """404 unless the repo's org installation is one the session can see."""
    try:
        rid = UUID(str(repo_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="repo not found")
    repo = session.get(Repo, rid)
    if repo is None:
        raise HTTPException(status_code=404, detail="repo not found")
    org = session.get(Org, repo.org_id)
    if org is None or org.github_installation_id not in user.installation_ids:
        raise HTTPException(status_code=404, detail="repo not found")
    return repo


def repo_by_full_name(
    session: Session, user: SessionUser, owner: str, name: str
) -> tuple[Repo, Org]:
    full = f"{owner}/{name}".lower()
    row = session.execute(
        select(Repo, Org)
        .join(Org, Org.id == Repo.org_id)
        .where(func.lower(Repo.full_name) == full)
        .order_by(Repo.active.desc(), Repo.created_at.desc())
        .limit(1)
    ).first()
    if row is None or row[1].github_installation_id not in user.installation_ids:
        raise HTTPException(status_code=404, detail="repo not found")
    return row[0], row[1]


# --------------------------------------------------------------------------------------
# Templates and request bodies
# --------------------------------------------------------------------------------------

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _fmt_dt(value: Any, fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    if value is None:
        return ""
    try:
        return value.strftime(fmt)
    except AttributeError:
        return str(value)


_env.filters["dt"] = _fmt_dt


def template_env() -> Environment:
    return _env


def render(request: Request, name: str, *, status_code: int = 200, **context: Any) -> HTMLResponse:
    from kintsua.api.billing import PRICING_LINE

    settings = get_settings()
    ctx: dict[str, Any] = {
        "request": request,
        "settings": settings,
        "base_url": settings.base_url.rstrip("/"),
        "user": current_session_user(request),
        "pricing_line": PRICING_LINE,
        "app_slug": settings.github_app_slug,
    }
    ctx.update(context)
    html = _env.get_template(name).render(**ctx)
    return HTMLResponse(html, status_code=status_code)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def wants_html(request: Request) -> bool:
    """HTMX or a browser form post (no JSON accept) gets an HTML fragment back."""
    if is_htmx(request):
        return True
    accept = request.headers.get("accept", "")
    ctype = request.headers.get("content-type", "")
    return "text/html" in accept and "application/json" not in accept and "json" not in ctype


async def read_body[T: BaseModel](request: Request, model: type[T]) -> T:
    """Validate a JSON or form-encoded body against ``model``; 400/422 on failure."""
    ctype = request.headers.get("content-type", "").lower()
    data: Any
    if "form" in ctype:
        form = await request.form()
        data = {}
        for key in form:
            values = form.getlist(key)
            data[key] = values if len(values) > 1 else values[0]
    else:
        raw = await request.body()
        if not raw.strip():
            data = {}
        else:
            try:
                data = json.loads(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="body must be JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise RequestValidationError(e.errors(include_url=False, include_input=False))


__all__ = [
    "MANIFEST_SALT",
    "OAUTH_SALT",
    "OAUTH_STATE_COOKIE",
    "SDK_KEY_PREFIX",
    "SESSION_COOKIE",
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "clear_session_cookie",
    "cookie_secure",
    "current_session_user",
    "get_db",
    "is_htmx",
    "load_session",
    "org_access",
    "org_for_sdk_key",
    "pick_org",
    "read_body",
    "render",
    "repo_access",
    "repo_by_full_name",
    "require_user",
    "serializer",
    "set_session_cookie",
    "sign_session",
    "template_env",
    "user_orgs",
    "user_repos",
    "wants_html",
]
