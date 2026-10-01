"""``POST /webhooks/github``: the GitHub App's webhook endpoint.

The raw body is verified against ``X-Hub-Signature-256`` first (401 on failure, 503 when no
secret is configured), then dispatched on ``X-GitHub-Event``:

* ``installation`` created -> Org + Repo rows + ``index_repo`` jobs; deleted -> org and repos
  inactive and sources no other active org depends on stop being polled; suspend /
  unsuspend toggle the org.
* ``installation_repositories`` added / removed.
* ``push`` to the default branch -> ``index_repo`` debounced by ``settings.push_debounce_s``
  (a queued run is rescheduled, never duplicated); ``.kintsua.yml`` in the changed files
  marks the reason ``config``.
* ``pull_request`` closed / reopened on a ``kintsua/`` branch -> ``pr_outcome`` job.
* ``issues`` closed by a human on an issue Kintsua opened -> impact dismissed; reopened ->
  issued again.
* ``check_run`` / ``check_suite`` / ``workflow_run`` completed on a ``kintsua/`` branch ->
  ``record_ci_result``.
* ``ping`` -> 200.

Handled events answer ``200 {"ok": true, "handled": <event>, ...}``; events and actions we
do not act on answer ``202 {"ok": true, "ignored": ...}``. Redeliveries are idempotent: the
``X-GitHub-Delivery`` id is remembered in a small in-memory LRU and every write is an
upsert keyed on GitHub ids.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from kintsua.core.config import get_settings
from kintsua.core.db import get_db
from kintsua.core.github import BRANCH_PREFIX, LABEL, get_github_app, verify_webhook_signature
from kintsua.core.models import Dependency, Impact, Org, Repo, Source
from kintsua.core.queue import JOB_PR_OUTCOME, enqueue
from kintsua.core.util import utcnow
from kintsua.fixer.run import record_ci_result
from kintsua.indexer.run import enqueue_index_repo

log = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

WEBHOOK_EVENTS: list[str] = [
    "installation",
    "installation_repositories",
    "push",
    "pull_request",
    "issues",
    "check_suite",
    "check_run",
]
CONFIG_FILE = ".kintsua.yml"
DEFAULT_ORG_SETTINGS: dict[str, Any] = {"fix_mode": "issue_only", "auto_issue": False}
MAX_DEFAULT_BRANCH_LOOKUPS = 25
MARKER_RE = re.compile(r"<!--\s*kintsua:change_event:([0-9a-fA-F-]{36})\s*-->")

WebhookHandler = Callable[[Session, dict[str, Any]], dict[str, Any] | None]


class DeliveryCache:
    """Remembers the last N ``X-GitHub-Delivery`` ids (redeliveries are acknowledged, not
    re-run). Per process; the row-level upserts make a replay harmless anyway."""

    def __init__(self, capacity: int = 4096):
        self.capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def __contains__(self, delivery_id: str) -> bool:
        with self._lock:
            return delivery_id in self._seen

    def add(self, delivery_id: str) -> None:
        with self._lock:
            self._seen[delivery_id] = None
            self._seen.move_to_end(delivery_id)
            while len(self._seen) > self.capacity:
                self._seen.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


deliveries = DeliveryCache()


# --------------------------------------------------------------------------------------
# Row helpers
# --------------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _installation_id(payload: dict[str, Any]) -> int | None:
    inst = payload.get("installation")
    return _int(inst.get("id")) if isinstance(inst, dict) else None


def _account(payload: dict[str, Any]) -> tuple[str, str | None]:
    inst = payload.get("installation") if isinstance(payload.get("installation"), dict) else {}
    acct = inst.get("account") if isinstance(inst.get("account"), dict) else {}
    login = str(acct.get("login") or inst.get("target_id") or "unknown")
    return login, (str(acct["type"]) if acct.get("type") else None)


def org_for_installation(session: Session, installation_id: int) -> Org | None:
    return session.scalar(select(Org).where(Org.github_installation_id == installation_id))


def upsert_org(session: Session, installation_id: int, login: str, account_type: str | None) -> Org:
    org = org_for_installation(session, installation_id)
    if org is None:
        org = Org(
            github_installation_id=installation_id,
            name=login,
            account_type=account_type,
            plan="free",
            settings=dict(DEFAULT_ORG_SETTINGS),
            active=True,
        )
        session.add(org)
        session.flush()
        log.info("org %s created for installation %s", login, installation_id)
        return org
    org.active = True
    org.name = login or org.name
    if account_type:
        org.account_type = account_type
    merged = {**DEFAULT_ORG_SETTINGS, **(org.settings or {})}
    if merged != (org.settings or {}):
        org.settings = merged
    session.flush()
    return org


def _default_branch_lookup(org: Org, full_name: str) -> str | None:
    """Ask GitHub for the default branch (installation events do not carry it). Any failure
    (app not configured, network) falls back to ``main``; the first push corrects it."""
    try:
        app = get_github_app()
        gh_repo = app.installation_client(int(org.github_installation_id)).get_repo(full_name)
        branch = getattr(gh_repo, "default_branch", None)
        return str(branch) if branch else None
    except Exception as e:  # noqa: BLE001 - best effort
        log.info("default branch lookup for %s failed: %s", full_name, type(e).__name__)
        return None


def repo_by_github_id(session: Session, github_repo_id: int) -> Repo | None:
    return session.scalar(select(Repo).where(Repo.github_repo_id == github_repo_id))


def upsert_repos(
    session: Session, org: Org, repo_dicts: list[Any], *, lookup_default_branch: bool = True
) -> list[Repo]:
    out: list[Repo] = []
    lookups = 0
    for item in repo_dicts:
        if not isinstance(item, dict):
            continue
        gh_id = _int(item.get("id"))
        full_name = item.get("full_name")
        if gh_id is None or not full_name:
            continue
        private = bool(item.get("private"))
        repo = repo_by_github_id(session, gh_id)
        if repo is None:
            branch = item.get("default_branch")
            if not branch and lookup_default_branch and lookups < MAX_DEFAULT_BRANCH_LOOKUPS:
                lookups += 1
                branch = _default_branch_lookup(org, str(full_name))
            repo = Repo(
                org_id=org.id,
                github_repo_id=gh_id,
                full_name=str(full_name),
                default_branch=str(branch or "main"),
                private=private,
                active=True,
            )
            session.add(repo)
            session.flush()
            log.info("repo %s added to org %s", full_name, org.name)
        else:
            repo.org_id = org.id
            repo.full_name = str(full_name)
            repo.private = private
            repo.active = True
            if item.get("default_branch"):
                repo.default_branch = str(item["default_branch"])
            session.flush()
        out.append(repo)
    return out


def _repo_from_payload(session: Session, payload: dict[str, Any]) -> Repo | None:
    info = payload.get("repository")
    if not isinstance(info, dict):
        return None
    gh_id = _int(info.get("id"))
    if gh_id is not None:
        repo = repo_by_github_id(session, gh_id)
        if repo is not None:
            return repo
    full_name = info.get("full_name")
    if full_name:
        from sqlalchemy import func

        return session.scalar(
            select(Repo)
            .where(func.lower(Repo.full_name) == str(full_name).lower())
            .order_by(Repo.active.desc(), Repo.created_at.desc())
            .limit(1)
        )
    return None


def _seeded_locators() -> set[tuple[str, str]]:
    try:
        from kintsua.seed.load import load_seed_sources

        return {(e.kind, e.locator) for e in load_seed_sources()}
    except Exception as e:  # noqa: BLE001 - the seed file is optional here
        log.info("seed file not readable (%s); treating no source as seeded", type(e).__name__)
        return set()


def deactivate_orphan_sources(session: Session, org: Org) -> int:
    """Stop polling sources only this org's repos depend on (seeded sources keep running for
    the public weekly report)."""
    ours = set(
        session.scalars(
            select(Dependency.source_id)
            .join(Repo, Repo.id == Dependency.repo_id)
            .where(Repo.org_id == org.id, Dependency.source_id.is_not(None))
            .distinct()
        )
    )
    if not ours:
        return 0
    still_used = set(
        session.scalars(
            select(Dependency.source_id)
            .join(Repo, Repo.id == Dependency.repo_id)
            .join(Org, Org.id == Repo.org_id)
            .where(
                Org.id != org.id,
                Org.active.is_(True),
                Repo.active.is_(True),
                Dependency.source_id.in_(list(ours)),
            )
            .distinct()
        )
    )
    seeded = _seeded_locators()
    n = 0
    for sid in ours - still_used:
        src = session.get(Source, sid)
        if src is None or not src.active or (src.kind, src.locator) in seeded:
            continue
        src.active = False
        n += 1
        log.info("source %s %s deactivated: no active org depends on it", src.kind, src.locator)
    return n


# --------------------------------------------------------------------------------------
# Event handlers (return a dict for handled, None for an ignored action)
# --------------------------------------------------------------------------------------


def handle_ping(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    return {"zen": payload.get("zen")}


def handle_installation(session: Session, payload: dict[str, Any]) -> dict[str, Any] | None:
    action = payload.get("action")
    inst_id = _installation_id(payload)
    if inst_id is None:
        raise HTTPException(status_code=400, detail="installation.id missing")
    login, account_type = _account(payload)
    if action == "created":
        org = upsert_org(session, inst_id, login, account_type)
        repos = upsert_repos(session, org, payload.get("repositories") or [])
        for repo in repos:
            enqueue_index_repo(session, repo, "install")
        return {"org_id": str(org.id), "repos": len(repos), "jobs": len(repos)}
    org = org_for_installation(session, inst_id)
    if action == "deleted":
        if org is None:
            return {"org_id": None, "repos": 0}
        org.active = False
        repos = list(session.scalars(select(Repo).where(Repo.org_id == org.id)))
        for repo in repos:
            repo.active = False
        session.flush()
        n_sources = deactivate_orphan_sources(session, org)
        log.info("installation %s deleted: org %s inactive", inst_id, org.name)
        return {"org_id": str(org.id), "repos": len(repos), "sources_deactivated": n_sources}
    if action in ("suspend", "unsuspend"):
        if org is None:
            return {"org_id": None}
        org.active = action == "unsuspend"
        return {"org_id": str(org.id), "active": org.active}
    return None


def handle_installation_repositories(
    session: Session, payload: dict[str, Any]
) -> dict[str, Any] | None:
    action = payload.get("action")
    inst_id = _installation_id(payload)
    if inst_id is None:
        raise HTTPException(status_code=400, detail="installation.id missing")
    login, account_type = _account(payload)
    if action == "added":
        org = upsert_org(session, inst_id, login, account_type)
        repos = upsert_repos(session, org, payload.get("repositories_added") or [])
        for repo in repos:
            enqueue_index_repo(session, repo, "install")
        return {"org_id": str(org.id), "added": len(repos)}
    if action == "removed":
        org = org_for_installation(session, inst_id)
        removed = 0
        for item in payload.get("repositories_removed") or []:
            gh_id = _int(item.get("id")) if isinstance(item, dict) else None
            repo = repo_by_github_id(session, gh_id) if gh_id is not None else None
            if repo is not None:
                repo.active = False
                removed += 1
        session.flush()
        if org is not None:
            deactivate_orphan_sources(session, org)
        return {"org_id": str(org.id) if org else None, "removed": removed}
    return None


def _changed_files(payload: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    commits = list(payload.get("commits") or [])
    head = payload.get("head_commit")
    if isinstance(head, dict):
        commits.append(head)
    for c in commits:
        if not isinstance(c, dict):
            continue
        for key in ("added", "modified", "removed"):
            vals = c.get(key)
            if isinstance(vals, list):
                out.update(str(v) for v in vals)
    return out


def handle_push(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    repo = _repo_from_payload(session, payload)
    if repo is None:
        return {"skipped": "repo not installed"}
    info = payload.get("repository") or {}
    payload_branch = info.get("default_branch") if isinstance(info, dict) else None
    if payload_branch and payload_branch != repo.default_branch:
        repo.default_branch = str(payload_branch)
    ref = str(payload.get("ref") or "")
    if ref != f"refs/heads/{repo.default_branch}":
        return {"skipped": "not the default branch", "ref": ref}
    if payload.get("deleted"):
        return {"skipped": "branch deleted", "ref": ref}
    org = session.get(Org, repo.org_id)
    if not repo.active or org is None or not org.active:
        return {"skipped": "repo inactive", "repo_id": str(repo.id)}
    reason = "config" if CONFIG_FILE in _changed_files(payload) else "push"
    debounce = get_settings().push_debounce_s
    enqueue_index_repo(session, repo, reason, debounce_s=debounce)
    head = payload.get("after") or (payload.get("head_commit") or {}).get("id")
    return {"repo_id": str(repo.id), "reason": reason, "debounce_s": debounce, "after": head}


def handle_pull_request(session: Session, payload: dict[str, Any]) -> dict[str, Any] | None:
    action = payload.get("action")
    if action not in ("closed", "reopened"):
        return None
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        raise HTTPException(status_code=400, detail="pull_request missing")
    repo = _repo_from_payload(session, payload)
    if repo is None:
        return {"skipped": "repo not installed"}
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    head_ref = str(head.get("ref") or "")
    if not head_ref.startswith(BRANCH_PREFIX):
        return {"skipped": "not a kintsua branch", "head": head_ref}
    number = _int(pr.get("number"))
    if number is None:
        raise HTTPException(status_code=400, detail="pull_request.number missing")
    merged = bool(pr.get("merged"))
    job = enqueue(
        session,
        JOB_PR_OUTCOME,
        {
            "repo_id": str(repo.id),
            "pr_number": number,
            "action": action,
            "merged": merged,
            "merge_commit_sha": pr.get("merge_commit_sha"),
            "head_sha": head.get("sha"),
        },
        dedupe_key=f"pr_outcome:{repo.id}:{number}:{action}:{int(merged)}",
    )
    return {
        "repo_id": str(repo.id),
        "pr_number": number,
        "merged": merged,
        "job_id": str(job.id) if job is not None else None,
    }


def _impact_for_issue(
    session: Session, repo: Repo, number: int | None, body: str | None
) -> Impact | None:
    if number is not None:
        impact = session.scalar(
            select(Impact)
            .where(Impact.repo_id == repo.id, Impact.github_issue_number == number)
            .order_by(Impact.created_at.desc())
            .limit(1)
        )
        if impact is not None:
            return impact
    m = MARKER_RE.search(body or "")
    if m:
        from uuid import UUID

        try:
            ce_id = UUID(m.group(1))
        except ValueError:
            return None
        return session.scalar(
            select(Impact).where(Impact.repo_id == repo.id, Impact.change_event_id == ce_id)
        )
    return None


def handle_issues(session: Session, payload: dict[str, Any]) -> dict[str, Any] | None:
    action = payload.get("action")
    if action not in ("closed", "reopened"):
        return None
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        raise HTTPException(status_code=400, detail="issue missing")
    repo = _repo_from_payload(session, payload)
    if repo is None:
        return {"skipped": "repo not installed"}
    number = _int(issue.get("number"))
    labels = {str(lb.get("name")) for lb in (issue.get("labels") or []) if isinstance(lb, dict)}
    impact = _impact_for_issue(session, repo, number, issue.get("body"))
    if impact is None:
        return {"skipped": "not a kintsua issue", "issue": number, "labelled": LABEL in labels}
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    login = str(sender.get("login") or "someone")
    if action == "closed":
        if sender.get("type") == "Bot":
            return {"skipped": "closed by a bot", "impact_id": str(impact.id)}
        if impact.status not in ("merged", "failed", "dismissed"):
            impact.status = "dismissed"
            impact.fix_reason = impact.fix_reason or f"issue #{number} closed by {login}"
        impact.resolved_at = impact.resolved_at or utcnow()
        log.info("impact %s dismissed: issue #%s closed by %s", impact.id, number, login)
    else:  # reopened
        if impact.status == "dismissed":
            impact.status = "pr_open" if impact.github_pr_number else "issued"
            impact.resolved_at = None
            impact.fix_reason = None
    session.flush()
    return {"impact_id": str(impact.id), "status": impact.status, "issue": number}


def _ci_from_check_run(payload: dict[str, Any]) -> tuple[Any, Any, list[Any], dict[str, Any]]:
    cr = payload.get("check_run") if isinstance(payload.get("check_run"), dict) else {}
    suite = cr.get("check_suite") if isinstance(cr.get("check_suite"), dict) else {}
    output = cr.get("output") if isinstance(cr.get("output"), dict) else {}
    details = {
        "source": "check_run",
        "name": cr.get("name"),
        "details_url": cr.get("details_url") or cr.get("html_url"),
        "head_sha": cr.get("head_sha"),
        "title": output.get("title"),
        "summary": output.get("summary"),
        "text": output.get("text"),
    }
    return (
        cr.get("conclusion"),
        suite.get("head_branch"),
        list(cr.get("pull_requests") or []),
        details,
    )


def _ci_from_check_suite(payload: dict[str, Any]) -> tuple[Any, Any, list[Any], dict[str, Any]]:
    cs = payload.get("check_suite") if isinstance(payload.get("check_suite"), dict) else {}
    app = cs.get("app") if isinstance(cs.get("app"), dict) else {}
    details = {
        "source": "check_suite",
        "name": app.get("name") or "checks",
        "details_url": cs.get("url"),
        "head_sha": cs.get("head_sha"),
    }
    return cs.get("conclusion"), cs.get("head_branch"), list(cs.get("pull_requests") or []), details


def _ci_from_workflow_run(payload: dict[str, Any]) -> tuple[Any, Any, list[Any], dict[str, Any]]:
    wr = payload.get("workflow_run") if isinstance(payload.get("workflow_run"), dict) else {}
    details = {
        "source": "workflow_run",
        "name": wr.get("name"),
        "details_url": wr.get("html_url"),
        "head_sha": wr.get("head_sha"),
    }
    return wr.get("conclusion"), wr.get("head_branch"), list(wr.get("pull_requests") or []), details


def _handle_ci(
    session: Session,
    payload: dict[str, Any],
    extract: Callable[[dict[str, Any]], tuple[Any, Any, list[Any], dict[str, Any]]],
) -> dict[str, Any] | None:
    if payload.get("action") != "completed":
        return None
    repo = _repo_from_payload(session, payload)
    if repo is None:
        return {"skipped": "repo not installed"}
    conclusion, head_branch, prs, details = extract(payload)
    if not conclusion:
        return {"skipped": "no conclusion"}
    if not head_branch:
        for pr in prs:
            head = (
                pr.get("head") if isinstance(pr, dict) and isinstance(pr.get("head"), dict) else {}
            )
            if head.get("ref"):
                head_branch = head["ref"]
                break
    head_branch = str(head_branch or "")
    if not head_branch.startswith(BRANCH_PREFIX):
        return {"skipped": "not a kintsua branch", "head": head_branch}
    impact = session.scalar(
        select(Impact)
        .where(Impact.repo_id == repo.id, Impact.pr_branch == head_branch)
        .order_by(Impact.created_at.desc())
        .limit(1)
    )
    pr_number = impact.github_pr_number if impact is not None else None
    if pr_number is None:
        for pr in prs:
            n = _int(pr.get("number")) if isinstance(pr, dict) else None
            if n is not None:
                pr_number = n
                break
    if pr_number is None:
        return {"skipped": "no PR for branch", "head": head_branch}
    details = {k: v for k, v in details.items() if v not in (None, "")}
    details["head_branch"] = head_branch
    result = record_ci_result(session, repo, int(pr_number), str(conclusion), details)
    return {
        "pr_number": int(pr_number),
        "conclusion": str(conclusion),
        "impact_id": str(result.id) if result is not None else None,
        "status": result.status if result is not None else None,
    }


def handle_check_run(session: Session, payload: dict[str, Any]) -> dict[str, Any] | None:
    return _handle_ci(session, payload, _ci_from_check_run)


def handle_check_suite(session: Session, payload: dict[str, Any]) -> dict[str, Any] | None:
    return _handle_ci(session, payload, _ci_from_check_suite)


def handle_workflow_run(session: Session, payload: dict[str, Any]) -> dict[str, Any] | None:
    return _handle_ci(session, payload, _ci_from_workflow_run)


HANDLERS: dict[str, WebhookHandler] = {
    "ping": handle_ping,
    "installation": handle_installation,
    "installation_repositories": handle_installation_repositories,
    "push": handle_push,
    "pull_request": handle_pull_request,
    "issues": handle_issues,
    "check_run": handle_check_run,
    "check_suite": handle_check_suite,
    "workflow_run": handle_workflow_run,
}


# --------------------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------------------


@router.post("/webhooks/github")
async def github_webhook(request: Request, session: Session = Depends(get_db)) -> JSONResponse:
    settings = get_settings()
    body = await request.body()
    if not settings.github_webhook_secret:
        raise HTTPException(status_code=503, detail="GITHUB_WEBHOOK_SECRET is not configured")
    if not verify_webhook_signature(
        body, request.headers.get("x-hub-signature-256"), settings.github_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    event = (request.headers.get("x-github-event") or "").strip()
    delivery = (request.headers.get("x-github-delivery") or "").strip()
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="body is not JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    if delivery and delivery in deliveries:
        return JSONResponse({"ok": True, "handled": event, "duplicate": True, "delivery": delivery})
    handler = HANDLERS.get(event)
    if handler is None:
        log.info("webhook %s ignored (delivery %s)", event or "?", delivery or "-")
        return JSONResponse({"ok": True, "ignored": event or None}, status_code=202)
    result = handler(session, payload)
    session.commit()
    if delivery:
        deliveries.add(delivery)
    action = payload.get("action")
    if result is None:
        log.info("webhook %s.%s ignored (delivery %s)", event, action, delivery or "-")
        return JSONResponse(
            {"ok": True, "ignored": event, "action": action, "delivery": delivery or None},
            status_code=202,
        )
    log.info("webhook %s.%s handled (delivery %s): %s", event, action, delivery or "-", result)
    out: dict[str, Any] = {"ok": True, "handled": event}
    if action:
        out["action"] = action
    out.update(result)
    return JSONResponse(out)


__all__ = [
    "DEFAULT_ORG_SETTINGS",
    "HANDLERS",
    "WEBHOOK_EVENTS",
    "DeliveryCache",
    "deactivate_orphan_sources",
    "deliveries",
    "github_webhook",
    "org_for_installation",
    "router",
    "upsert_org",
    "upsert_repos",
]
