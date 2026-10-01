"""The two pages (plus the landing page): ``GET /`` (what Kintsua does, install button, the
one-line pricing, privacy defaults, CLI snippet, report link, subscribe form),
``GET /settings`` (the user's repos) and ``GET /settings/{owner}/{name}`` (dependency map,
change list with draft issues, mute rules, fix mode, SDK key, data deletion), server-rendered
with HTMX, no SPA, no build step.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from kintsua.api.billing import pricing_line, stripe_configured, watched_integration_count
from kintsua.api.deps import (
    current_session_user,
    get_db,
    render,
    repo_by_full_name,
    user_orgs,
    user_repos,
)
from kintsua.api.keys import key_info
from kintsua.api.repos import change_entries, config_sources, mute_list, repo_fix_mode
from kintsua.api.schemas import SessionUser
from kintsua.core.config import get_settings
from kintsua.core.models import Org, Repo
from kintsua.indexer.config import effective_fix_mode
from kintsua.indexer.depmap import dependency_map

log = logging.getLogger(__name__)

router = APIRouter(tags=["pages"])

WHAT_IT_DOES = [
    "Kintsua finds every external API and MCP tool your agent repo depends on and maps the "
    "exact files and lines that call them.",
    "It watches those specs, servers, packages and changelogs every day and opens a GitHub "
    "issue within an hour of a breaking change landing upstream.",
    "For the common breaks (renamed fields, new required parameters, SDK major bumps) it opens "
    "a fix PR that already passed your tests.",
]

PRIVACY_PARAGRAPH = (
    "Privacy defaults: no payloads and no PII ever leave your process - the runtime SDK "
    "reports tool names, argument keys and error classes only; SDK keys are hashed at rest; "
    "the indexer keeps file paths, line numbers and symbol names, never your source; and every "
    "repo has a one-click data deletion endpoint from day one."
)

CLI_SNIPPET = """pip install kintsua
kintsua watch snapshot --server https://mcp.vendor.com/mcp   # writes .kintsua/vendor.lock.json
kintsua watch diff --server https://mcp.vendor.com/mcp       # exit 1 on breaking changes
kintsua watch report --since 7d                               # Markdown report to stdout"""

SDK_SNIPPET = """import kintsua

session = kintsua.wrap_mcp(session, repo="{repo}", key=KINTSUA_KEY)
client = kintsua.wrap_http(httpx.AsyncClient(), repo="{repo}", key=KINTSUA_KEY)"""

KINTSUA_YML_SNIPPET = """sources:
  - kind: openapi
    url: https://api.vendor.com/openapi.json
  - kind: changelog
    url: https://vendor.com/changelog
mute:
  - stripe:/v1/legacy/*
fix:
  mode: pr          # pr | issue_only
  base: {branch}"""


def install_url() -> str:
    return f"https://github.com/apps/{get_settings().github_app_slug}/installations/new"


def _login_redirect(request: Request) -> Response:
    target = request.url.path
    if request.url.query:
        target += f"?{request.url.query}"
    return RedirectResponse(url=f"/login?next={quote(target, safe='/')}", status_code=302)


@router.get("/")
def landing(request: Request) -> Response:
    return render(
        request,
        "landing.html",
        sentences=WHAT_IT_DOES,
        privacy=PRIVACY_PARAGRAPH,
        cli_snippet=CLI_SNIPPET,
        install_url=install_url(),
        pricing_line=pricing_line(),
    )


@router.get("/settings")
def settings_index(
    request: Request,
    billing: str | None = None,
    user: SessionUser | None = Depends(current_session_user),
    session: Session = Depends(get_db),
) -> Response:
    if user is None:
        return _login_redirect(request)
    orgs = user_orgs(session, user)
    repos = user_repos(session, user)
    by_org: dict[Any, list[Repo]] = {o.id: [] for o in orgs}
    for r in repos:
        by_org.setdefault(r.org_id, []).append(r)
    org_rows = [
        {
            "org": o,
            "repos": by_org.get(o.id, []),
            "key": key_info(o),
            "watched": watched_integration_count(session, o),
        }
        for o in orgs
    ]
    return render(
        request,
        "settings_list.html",
        orgs=org_rows,
        install_url=install_url(),
        billing=billing,
        billing_enabled=stripe_configured(),
        pricing_line=pricing_line(),
    )


def repo_page_context(session: Session, repo: Repo, org: Org) -> dict[str, Any]:
    org_settings = dict(org.settings or {})
    depmap = dependency_map(session, repo, org_settings=org_settings)
    changes = change_entries(session, repo, 100)
    return {
        "repo": repo,
        "org": org,
        "depmap": depmap,
        "changes": changes,
        "drafts": [c for c in changes if c["draft"]],
        "mutes": mute_list(repo),
        "sources": config_sources(repo),
        "fix_mode": effective_fix_mode(repo.config, org_settings),
        "repo_fix_mode": repo_fix_mode(repo),
        "org_fix_mode": org_settings.get("fix_mode") or "issue_only",
        "auto_issue": bool(org_settings.get("auto_issue")) or get_settings().auto_issue,
        "key": key_info(org),
        "sdk_snippet": SDK_SNIPPET.format(repo=repo.full_name),
        "kintsua_yml": KINTSUA_YML_SNIPPET.format(branch=repo.default_branch or "main"),
        "watched": watched_integration_count(session, org),
        "billing_enabled": stripe_configured(),
        "pricing_line": pricing_line(),
        "billing_warning": org_settings.get("billing_warning"),
    }


@router.get("/settings/{owner}/{name}")
def settings_repo(
    request: Request,
    owner: str,
    name: str,
    user: SessionUser | None = Depends(current_session_user),
    session: Session = Depends(get_db),
) -> Response:
    if user is None:
        return _login_redirect(request)
    repo, org = repo_by_full_name(session, user, owner, name)
    return render(request, "settings_repo.html", **repo_page_context(session, repo, org))


__all__ = [
    "CLI_SNIPPET",
    "KINTSUA_YML_SNIPPET",
    "PRIVACY_PARAGRAPH",
    "WHAT_IT_DOES",
    "install_url",
    "repo_page_context",
    "router",
]
