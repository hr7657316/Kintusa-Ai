"""SDK keys: ``POST /v1/keys`` creates or rotates the org's key (shown once, stored as a
sha256 hash plus a 12-character prefix); ``GET /v1/keys`` lists prefixes and creation times.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from kintsua.api.deps import (
    SDK_KEY_PREFIX,
    get_db,
    pick_org,
    read_body,
    render,
    require_user,
    user_orgs,
    wants_html,
)
from kintsua.api.schemas import CreateKeyBody, SessionUser
from kintsua.core.models import Org
from kintsua.core.util import new_token, sha256_hex, utcnow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["keys"])

KEY_PREFIX_CHARS = 12


def generate_key() -> str:
    """``kts_`` + 32 url-safe characters."""
    return f"{SDK_KEY_PREFIX}{new_token(24)}"


def rotate_sdk_key(session: Session, org: Org) -> str:
    """Store a fresh key on the org and return the plaintext (the only time it exists)."""
    key = generate_key()
    org.sdk_key_hash = sha256_hex(key)
    org.sdk_key_prefix = key[:KEY_PREFIX_CHARS]
    org.sdk_key_created_at = utcnow()
    session.flush()
    log.info("SDK key rotated for org %s (prefix %s)", org.name, org.sdk_key_prefix)
    return key


def key_info(org: Org) -> dict[str, Any]:
    return {
        "org_id": str(org.id),
        "org": org.name,
        "prefix": org.sdk_key_prefix,
        "created_at": org.sdk_key_created_at.isoformat() if org.sdk_key_created_at else None,
        "has_key": bool(org.sdk_key_hash),
    }


@router.post("/keys")
async def create_key(
    request: Request,
    user: SessionUser = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    body = await read_body(request, CreateKeyBody)
    return await run_in_threadpool(_create_key, request, user, session, body)


def _create_key(
    request: Request, user: SessionUser, session: Session, body: CreateKeyBody
) -> Response:
    org = pick_org(session, user, body.org_id)
    rotated = bool(org.sdk_key_hash)
    key = rotate_sdk_key(session, org)
    session.commit()
    info = key_info(org)
    if wants_html(request):
        return render(request, "partials/key.html", key=key, info=info, rotated=rotated)
    return JSONResponse({"key": key, "rotated": rotated, **info})


@router.get("/keys")
def list_keys(
    user: SessionUser = Depends(require_user), session: Session = Depends(get_db)
) -> dict[str, Any]:
    return {"keys": [key_info(org) for org in user_orgs(session, user)]}


__all__ = ["generate_key", "key_info", "rotate_sdk_key", "router"]
