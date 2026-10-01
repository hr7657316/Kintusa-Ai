"""GitHub App manifest flow: how the operator gets a registered ``kintsua`` App in one click.

``GET /setup/github`` renders the manifest (name, URLs, permissions, events) inside a form
that posts it to ``https://github.com/settings/apps/new?state=<signed>`` (or an org's
``/organizations/<org>/settings/apps/new``). GitHub creates the App and redirects to
``/setup/github/callback?code=...``; that handler exchanges the code
(``POST /app-manifests/{code}/conversions``) and shows the app id, private key, webhook
secret and OAuth client id/secret as ``.env`` lines to copy. Nothing is stored server-side.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response
from itsdangerous import BadSignature, SignatureExpired

from kintsua.api.auth import github_web_url
from kintsua.api.deps import MANIFEST_SALT, render, serializer
from kintsua.core.config import Settings, get_settings
from kintsua.core.util import new_token

log = logging.getLogger(__name__)

router = APIRouter(tags=["setup"])

STATE_MAX_AGE_S = 3600
HTTP_TIMEOUT_S = 30.0

DEFAULT_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "metadata": "read",
    "checks": "read",
}
# ``installation`` / ``installation_repositories`` are delivered to every GitHub App and are
# not subscribable, so they are not listed here (GitHub rejects a manifest that names them).
DEFAULT_EVENTS: list[str] = ["push", "pull_request", "issues", "check_suite", "check_run"]


class SetupError(Exception):
    """GitHub refused the manifest conversion or answered something unusable."""


def build_manifest(settings: Settings | None = None, *, name: str | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    base = s.base_url.rstrip("/")
    return {
        "name": (name or s.github_app_slug or "kintsua")[:34],
        "url": base,
        "description": (
            "Self-maintaining integrations for AI agents: Kintsua maps every external API and "
            "MCP tool a repo depends on, detects breaking changes upstream and opens a tested fix PR."
        ),
        "hook_attributes": {"url": f"{base}/webhooks/github", "active": True},
        "redirect_url": f"{base}/setup/github/callback",
        "callback_urls": [f"{base}/auth/callback"],
        "setup_url": f"{base}/settings",
        "setup_on_update": False,
        "public": True,
        "default_permissions": dict(DEFAULT_PERMISSIONS),
        "default_events": list(DEFAULT_EVENTS),
    }


def manifest_action_url(settings: Settings, state: str, org: str | None = None) -> str:
    web = github_web_url(settings)
    if org:
        return f"{web}/organizations/{org}/settings/apps/new?state={state}"
    return f"{web}/settings/apps/new?state={state}"


def convert_manifest(settings: Settings, code: str) -> dict[str, Any]:
    url = f"{settings.github_api_url.rstrip('/')}/app-manifests/{code}/conversions"
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            r = client.post(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": settings.user_agent,
                },
            )
    except httpx.HTTPError as e:
        raise SetupError(f"could not reach GitHub: {type(e).__name__}") from e
    if r.status_code not in (200, 201):
        detail = ""
        try:
            detail = str(r.json().get("message") or "")
        except Exception:  # noqa: BLE001
            detail = r.text[:200]
        raise SetupError(
            f"GitHub answered HTTP {r.status_code} to the manifest conversion: {detail}"
        )
    try:
        data = r.json()
    except ValueError:
        raise SetupError("GitHub returned a non-JSON body")
    if not isinstance(data, dict) or not data.get("id") or not data.get("pem"):
        raise SetupError("GitHub's response is missing the app id or private key")
    return data


def _pem_one_line(pem: str) -> str:
    return pem.strip().replace("\r\n", "\n").replace("\n", "\\n")


def env_lines(result: dict[str, Any]) -> str:
    """``.env`` lines for the converted app (``GITHUB_APP_PRIVATE_KEY`` uses ``\\n`` escapes,
    which ``Settings.github_private_key_pem`` unescapes)."""
    lines = [
        f"GITHUB_APP_ID={result.get('id')}",
        f"GITHUB_APP_SLUG={result.get('slug') or 'kintsua'}",
        f"GITHUB_WEBHOOK_SECRET={result.get('webhook_secret') or ''}",
        f"GITHUB_OAUTH_CLIENT_ID={result.get('client_id') or ''}",
        f"GITHUB_OAUTH_CLIENT_SECRET={result.get('client_secret') or ''}",
        f'GITHUB_APP_PRIVATE_KEY="{_pem_one_line(str(result.get("pem") or ""))}"',
    ]
    return "\n".join(lines)


@router.get("/setup/github")
def setup_page(request: Request, org: str | None = None, name: str | None = None) -> Response:
    s = get_settings()
    manifest = build_manifest(s, name=name)
    state = serializer(MANIFEST_SALT).dumps({"n": new_token(16)})
    org = (org or "").strip() or None
    return render(
        request,
        "setup.html",
        manifest=manifest,
        manifest_json=json.dumps(manifest, indent=2),
        action=manifest_action_url(s, state, org),
        org=org,
        configured=s.github_configured,
        callback_url=manifest["redirect_url"],
        webhook_url=manifest["hook_attributes"]["url"],
    )


@router.api_route("/setup/github/callback", methods=["GET", "POST"])
async def setup_callback(request: Request) -> Response:
    s = get_settings()
    params: dict[str, Any] = dict(request.query_params)
    if request.method == "POST":
        try:
            form = await request.form()
            params.update({k: form.get(k) for k in form})
        except Exception:  # noqa: BLE001 - a JSON body is fine too
            try:
                body = await request.json()
                if isinstance(body, dict):
                    params.update(body)
            except Exception:  # noqa: BLE001
                pass
    code = str(params.get("code") or "").strip()
    state = str(params.get("state") or "").strip()
    if not code:
        return render(
            request,
            "error.html",
            status_code=400,
            title="Missing code",
            message="GitHub did not send a manifest code. Start again from /setup/github.",
        )
    if state:
        try:
            serializer(MANIFEST_SALT).loads(state, max_age=STATE_MAX_AGE_S)
        except (BadSignature, SignatureExpired):
            return render(
                request,
                "error.html",
                status_code=400,
                title="Setup state expired",
                message="The setup link is older than an hour or was not issued by this server.",
            )
    try:
        result = convert_manifest(s, code)
    except SetupError as e:
        log.warning("manifest conversion failed: %s", e)
        return render(
            request,
            "error.html",
            status_code=502,
            title="GitHub App creation failed",
            message=str(e),
        )
    log.info(
        "GitHub App %s (id %s) created through the manifest flow",
        result.get("slug"),
        result.get("id"),
    )
    return render(
        request,
        "setup_result.html",
        app_id=result.get("id"),
        slug=result.get("slug"),
        html_url=result.get("html_url"),
        env_text=env_lines(result),
        pem=str(result.get("pem") or ""),
        webhook_secret=result.get("webhook_secret"),
        client_id=result.get("client_id"),
        client_secret=result.get("client_secret"),
        install_url=f"https://github.com/apps/{result.get('slug') or 'kintsua'}/installations/new",
    )


__all__ = [
    "DEFAULT_EVENTS",
    "DEFAULT_PERMISSIONS",
    "SetupError",
    "build_manifest",
    "convert_manifest",
    "env_lines",
    "manifest_action_url",
    "router",
]
