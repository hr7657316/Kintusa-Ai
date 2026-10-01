"""Job handlers for the fixer and the PR/CI outcome seams.

``fix_impact`` (``{"impact_id"}``): load the impact, refuse early when the category is
not fixable (only ``rename``, ``required_param`` and ``deprecation`` in v0), when the
effective fix mode is not ``pr`` or when the change touches auth; defer (re-enqueue in
60 s) while ``settings.fix_concurrency`` fixes are running; otherwise move the impact to
``fixing``, prepare the sandbox, detect the test plan (refusing repos with neither a test
command nor static checks), install dependencies, build the ``FixContext`` from the
sandbox, run the baseline, the agent and the validator, then commit, push and open the
PR (``pr_open``) or record an honest failure (``failed`` + ``fix_reason`` + an issue
comment with the scrubbed log excerpt). The sandbox is always cleaned up and token spend
is logged per job.

``pr_outcome`` (``{"repo_id", "pr_number", "action", "merged"}``): ``record_pr_outcome``.
``record_ci_result`` is called by the webhook handler for check results on our PR.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kintsua.core.config import Settings, get_settings
from kintsua.core.github import LABEL, get_github_app
from kintsua.core.llm import get_llm
from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Classification,
    Impact,
    Org,
    Repo,
    Snapshot,
    Source,
)
from kintsua.core.queue import JOB_FIX_IMPACT, JOB_PR_OUTCOME, NonRetryable, enqueue, job_handler
from kintsua.core.storage import get_document
from kintsua.core.util import redact_secrets, utcnow
from kintsua.fixer.agent import CallSiteContext, FixContext, read_context, run_agent
from kintsua.fixer.pr import (
    PushRefused,
    branch_name,
    commit_and_push,
    commit_message,
    open_pull_request,
    pr_title,
    render_pr_body,
    severity_label,
)
from kintsua.fixer.sandbox import (
    Sandbox,
    SandboxError,
    SandboxTimeout,
    make_sandbox,
    register_active,
    tail_text,
    unregister_active,
)
from kintsua.fixer.testcmd import TestPlan, detect_test_command
from kintsua.fixer.validate import run_baseline, validate

log = logging.getLogger(__name__)

FIXABLE_CATEGORIES: frozenset[str] = frozenset({"rename", "required_param", "deprecation"})
CI_FAILURE_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "timed_out", "cancelled", "action_required"}
)
DEFER_DELAY_S = 60.0
INSTALL_TIMEOUT_S = 600.0
LOG_EXCERPT_LINES = 40


def fix_impact_dedupe_key(impact_id: Any) -> str:
    return f"fix_impact:{impact_id}"


# --------------------------------------------------------------------------------------
# Policy helpers
# --------------------------------------------------------------------------------------


def effective_fix_mode(repo: Repo, org: Org | None) -> str:
    fix = (repo.config or {}).get("fix") or {}
    mode = fix.get("mode") if isinstance(fix, dict) else None
    if mode in ("pr", "issue_only"):
        return str(mode)
    org_mode = ((org.settings if org is not None else None) or {}).get("fix_mode")
    if org_mode in ("pr", "issue_only"):
        return str(org_mode)
    return "issue_only"


def refusal_reason(
    classification: Classification | None, repo: Repo, org: Org | None
) -> str | None:
    """Why the fixer will not even start (None when it may)."""
    if classification is None:
        return "the change event has no classification"
    if classification.category == "auth":
        return "the change touches authentication or scopes; Kintsua does not patch auth in v0"
    if classification.severity not in ("breaking", "degraded"):
        return f"severity {classification.severity!r} is not actionable; only breaking and degraded changes get PRs"
    if classification.category not in FIXABLE_CATEGORIES:
        return (
            f"category {classification.category!r} is not auto-fixed in v0 (only "
            f"{', '.join(sorted(FIXABLE_CATEGORIES))})"
        )
    mode = effective_fix_mode(repo, org)
    if mode != "pr":
        return f"fix mode is {mode!r}; set fix.mode: pr in .kintsua.yml to get PRs"
    return None


def plan_refusal(plan: TestPlan) -> str | None:
    if plan.command or plan.static_checks:
        return None
    return (
        "the repository has no test command and no static checks, so a patch could not be "
        "verified. Adding a test command (pyproject [tool.kintsua] test, package.json "
        "scripts.test, a Makefile test target, pytest or jest/vitest) or a static check "
        "(ruff config, tsconfig.json) unlocks PRs."
    )


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


def _load(
    session: Session, impact: Impact
) -> tuple[Repo, Org | None, ChangeEventRow, Classification | None, Source | None]:
    repo = session.get(Repo, impact.repo_id)
    if repo is None:
        raise NonRetryable(f"impact {impact.id}: repo {impact.repo_id} does not exist")
    org = session.get(Org, repo.org_id)
    if org is None:
        raise NonRetryable(f"impact {impact.id}: org {repo.org_id} does not exist")
    row = session.get(ChangeEventRow, impact.change_event_id)
    if row is None:
        raise NonRetryable(
            f"impact {impact.id}: change_event {impact.change_event_id} does not exist"
        )
    cls: Classification | None = None
    if impact.classification_id is not None:
        cls = session.get(Classification, impact.classification_id)
    if cls is None:
        cls = session.scalar(select(Classification).where(Classification.change_event_id == row.id))
    source = session.get(Source, row.source_id)
    return repo, org, row, cls, source


def _comment(session: Session, impact: Impact, body: str) -> None:
    try:
        from kintsua.matcher.issues import comment_on_impact

        comment_on_impact(session, impact, body)
    except Exception as e:  # noqa: BLE001 - a failed comment must not mask the real outcome
        log.warning("impact %s: could not comment on the issue: %s", impact.id, e)


def _fail(
    session: Session,
    impact: Impact,
    reason: str,
    *,
    excerpt: str | None = None,
    tokens: int | None = None,
    comment: bool = True,
) -> None:
    reason = redact_secrets(reason)
    impact.status = "failed"
    impact.fix_reason = reason[:4000]
    impact.resolved_at = utcnow()
    if tokens is not None:
        impact.tokens_used = int(tokens)
    session.commit()
    log.info("impact %s failed: %s", impact.id, reason[:300])
    if comment:
        body = f"Kintsua could not open a fix PR: {reason}"
        if excerpt and excerpt.strip():
            body += (
                "\n\n<details><summary>Log excerpt (last lines, secrets redacted)</summary>\n\n"
                f"```\n{_fence_safe(redact_secrets(excerpt))}\n```\n</details>"
            )
        _comment(session, impact, body)


def _fence_safe(text: str) -> str:
    """Keep an excerpt from closing the Markdown code fence it is shown in."""
    return text.replace("```", "'" * 3)


def _tool_name(cls: Classification | None, sites: list[dict[str, Any]]) -> str | None:
    for p in (cls.affected_paths if cls is not None else None) or []:
        if isinstance(p, str) and p.startswith("tool:"):
            return p.split(":", 1)[1]
    for s in sites:
        op = s.get("operation")
        if isinstance(op, str) and op and " " not in op and "/" not in op:
            return op
    return None


def _new_input_schema(
    session: Session, row: ChangeEventRow, tool_name: str | None
) -> dict[str, Any] | None:
    if not tool_name:
        return None
    body: Any = None
    if row.to_snapshot_id is not None:
        snap = session.get(Snapshot, row.to_snapshot_id)
        if snap is not None:
            try:
                body = get_document(snap.storage_ref)
            except Exception as e:  # noqa: BLE001
                log.warning("could not load snapshot %s: %s", snap.id, e)
    if isinstance(body, dict):
        tools = body.get("tools")
        entry = None
        if isinstance(tools, dict):
            entry = tools.get(tool_name)
        elif isinstance(tools, list):
            entry = next(
                (t for t in tools if isinstance(t, dict) and t.get("name") == tool_name), None
            )
        if isinstance(entry, dict) and isinstance(entry.get("inputSchema"), dict):
            return entry["inputSchema"]
    # fall back to an op that carries the whole new schema
    for op in (row.diff or {}).get("ops") or []:
        if not isinstance(op, dict):
            continue
        path = str(op.get("path") or "")
        if path.endswith(f"/{tool_name}/inputSchema") and isinstance(op.get("after"), dict):
            return op["after"]
    return None


def build_fix_context(
    session: Session,
    impact: Impact,
    repo: Repo,
    row: ChangeEventRow,
    cls: Classification,
    source: Source | None,
    sandbox: Sandbox,
    plan: TestPlan,
) -> FixContext:
    """Call sites with 20 lines of context read from the prepared sandbox, the ops, the
    migration hint and (for MCP) the new inputSchema from the to-snapshot."""
    diff = row.diff or {}
    sites_raw = [s for s in (impact.matched_call_sites or []) if isinstance(s, dict)]
    sites: list[CallSiteContext] = []
    for s in sites_raw:
        fp = str(s.get("file_path") or "")
        if not fp or fp.startswith("<"):
            continue  # runtime-observed sites have no file
        line = int(s.get("line") or 0)
        args_shape: dict[str, Any] = {}
        cs_id = s.get("call_site_id")
        if cs_id:
            try:
                cs = session.get(CallSite, UUID(str(cs_id)))
            except (ValueError, TypeError):
                cs = None
            if cs is not None and isinstance(cs.args_shape, dict):
                args_shape = dict(cs.args_shape)
        sites.append(
            CallSiteContext(
                file_path=fp,
                line=line,
                operation=str(s.get("operation") or ""),
                symbol=s.get("symbol"),
                args_shape=args_shape,
                context=read_context(sandbox, fp, line),
                call_site_id=str(cs_id) if cs_id else None,
                in_tests=bool(s.get("in_tests")),
            )
        )
    source_kind = str(diff.get("source_kind") or (source.kind if source else "unknown"))
    vendor = (
        (source.vendor if source else None) or (source.display_name if source else None) or "vendor"
    )
    tool_name = _tool_name(cls, sites_raw) if source_kind in ("mcp", "observed") else None
    fix_cfg = (repo.config or {}).get("fix") or {}
    radius = fix_cfg.get("radius") if isinstance(fix_cfg, dict) else None
    return FixContext(
        repo_full_name=repo.full_name,
        default_branch=repo.default_branch or "main",
        vendor=str(vendor),
        source_kind=source_kind,
        severity=cls.severity,
        category=cls.category,
        summary=cls.summary or "",
        migration_hint=cls.migration_hint,
        affected_paths=[str(p) for p in (cls.affected_paths or [])],
        ops=[o for o in (diff.get("ops") or []) if isinstance(o, dict)],
        vendor_text=diff.get("text") if isinstance(diff.get("text"), str) else None,
        call_sites=sites,
        test_plan=plan,
        source_display=(source.display_name if source else "") or "",
        from_version=diff.get("from_version"),
        to_version=diff.get("to_version"),
        detected_at=row.detected_at,
        new_input_schema=_new_input_schema(session, row, tool_name),
        tool_name=tool_name,
        issue_number=impact.github_issue_number,
        change_event_id=str(row.id),
        impact_id=str(impact.id),
        language=plan.language if plan.language != "unknown" else "python",
        import_radius=int(radius) if isinstance(radius, int) else None,
        base_sha=sandbox.base_sha,
    )


def run_install(sandbox: Sandbox, plan: TestPlan) -> None:
    """Dependency install (the only networked step besides the clone)."""
    for cmd in plan.install:
        res = sandbox.run(cmd, timeout_s=INSTALL_TIMEOUT_S, network=True)
        if res.timed_out:
            raise SandboxError(f"dependency install timed out: `{cmd}`")
        if not res.ok:
            raise SandboxError(
                f"dependency install failed (`{cmd}` exit {res.exit_code}): {res.tail(15)}"
            )


def _fixing_count(session: Session, exclude: Impact) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Impact)
            .where(Impact.status == "fixing", Impact.id != exclude.id)
        )
        or 0
    )


# --------------------------------------------------------------------------------------
# fix_impact
# --------------------------------------------------------------------------------------


@job_handler(JOB_FIX_IMPACT)
def handle_fix_impact(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    try:
        impact_id = UUID(str(payload["impact_id"]))
    except (KeyError, ValueError, TypeError) as e:
        raise NonRetryable(f"fix_impact: bad payload {payload!r}: {e}")
    impact = session.get(Impact, impact_id)
    if impact is None:
        raise NonRetryable(f"fix_impact: impact {impact_id} does not exist")
    if impact.status in ("pr_open", "merged", "dismissed", "failed"):
        log.info("impact %s is %s; nothing to do", impact.id, impact.status)
        return {"impact_id": str(impact.id), "status": impact.status, "skipped": True}

    repo, org, row, cls, source = _load(session, impact)
    reason = refusal_reason(cls, repo, org)
    if reason:
        _fail(session, impact, reason)
        return {"impact_id": str(impact.id), "status": "failed", "refused": reason}

    running = _fixing_count(session, impact)
    if running >= settings.fix_concurrency:
        enqueue(
            session,
            JOB_FIX_IMPACT,
            {"impact_id": str(impact.id)},
            delay_s=settings.fix_defer_delay_s,
            dedupe_key=fix_impact_dedupe_key(impact.id),
        )
        session.commit()
        log.info(
            "impact %s deferred: %s fixes running (limit %s)",
            impact.id,
            running,
            settings.fix_concurrency,
        )
        return {"impact_id": str(impact.id), "status": impact.status, "deferred": True}

    assert cls is not None  # refusal_reason guarantees it
    impact.status = "fixing"
    job_id = payload.get("_job_id")
    if job_id:
        with contextlib.suppress(ValueError):
            impact.fix_job_id = UUID(str(job_id))
    session.commit()
    return _run_fix(session, settings, impact, repo, org, row, cls, source)


def _run_fix(
    session: Session,
    settings: Settings,
    impact: Impact,
    repo: Repo,
    org: Org | None,
    row: ChangeEventRow,
    cls: Classification,
    source: Source | None,
) -> dict[str, Any]:
    app = get_github_app()
    installation_id = int(org.github_installation_id) if org is not None else 0
    sandbox = make_sandbox(settings, job_id=f"{str(impact.id)[:8]}")
    register_active(sandbox)
    tokens_in = tokens_out = 0
    model: str | None = None
    try:
        fix_cfg = (repo.config or {}).get("fix") or {}
        base_branch = (
            (fix_cfg.get("base") if isinstance(fix_cfg, dict) else None)
            or repo.default_branch
            or "main"
        )
        clone_url = app.clone_url(installation_id, repo.full_name)
        workdir = sandbox.prepare(clone_url, base_branch)

        plan = detect_test_command(workdir, repo.config or {})
        refused = plan_refusal(plan)
        if refused:
            _fail(session, impact, refused, comment=True)
            return {"impact_id": str(impact.id), "status": "failed", "refused": refused}

        run_install(sandbox, plan)
        ctx = build_fix_context(session, impact, repo, row, cls, source, sandbox, plan)
        if not ctx.call_sites:
            _fail(
                session, impact, "no call sites with a file location are recorded for this impact"
            )
            return {"impact_id": str(impact.id), "status": "failed"}

        baseline = run_baseline(
            sandbox, plan, repo_key=repo.full_name, sha=sandbox.base_sha, settings=settings
        )
        llm = get_llm()
        outcome = run_agent(sandbox, ctx, llm, max_steps=settings.fix_max_steps, settings=settings)
        tokens_in, tokens_out, model = outcome.tokens_in, outcome.tokens_out, outcome.model
        impact.tokens_used = outcome.tokens_total
        session.commit()
        if outcome.status != "patched":
            label = {
                "needs_human": "the fix needs a human decision",
                "gave_up": "the agent gave up",
                "error": "the fix agent hit an error",
            }.get(outcome.status, outcome.status)
            _fail(
                session,
                impact,
                f"{label}: {outcome.reason}",
                excerpt=sandbox.log_excerpt(LOG_EXCERPT_LINES),
                tokens=outcome.tokens_total,
            )
            return {"impact_id": str(impact.id), "status": "failed", "agent": outcome.status}

        verdict = validate(sandbox, plan, ctx, baseline, settings=settings)
        if not verdict.ok:
            excerpt = verdict.test_output_tail or sandbox.log_excerpt(LOG_EXCERPT_LINES)
            _fail(
                session,
                impact,
                "validation failed: " + "; ".join(verdict.reasons),
                excerpt=excerpt,
                tokens=outcome.tokens_total,
            )
            return {"impact_id": str(impact.id), "status": "failed", "reasons": verdict.reasons}

        gh_repo = app.installation_client(installation_id).get_repo(repo.full_name)
        branch = branch_name(ctx.vendor, row.id)
        try:
            pushed = commit_and_push(
                sandbox,
                branch,
                commit_message(ctx),
                clone_url=clone_url,
                gh_repo=gh_repo,
                base_branch=base_branch,
            )
        except PushRefused as e:
            _fail(session, impact, f"push refused: {e}", tokens=outcome.tokens_total)
            return {"impact_id": str(impact.id), "status": "failed", "push": str(e)}
        impact.ci_result = {
            **(impact.ci_result or {}),
            "pushed_sha": pushed.sha,
            "branch": branch,
            "base": base_branch,
            "pushed_at": utcnow().isoformat(),
        }
        impact.pr_branch = branch
        session.commit()

        body = render_pr_body(ctx, verdict, impact.github_issue_number, agent_notes=outcome.reason)
        labels = [LABEL]
        sev = severity_label(cls.severity)
        if sev:
            labels.append(sev)
        number = open_pull_request(
            gh_repo,
            pr_title(ctx),
            body,
            head=branch,
            base=base_branch,
            labels=labels,
            write_lock=app.write_lock(installation_id),
        )
        impact.github_pr_number = number
        impact.status = "pr_open"
        session.commit()
        pr_url = f"https://github.com/{repo.full_name}/pull/{number}"
        _comment(
            session,
            impact,
            f"Kintsua opened PR #{number} ({pr_url}): {verdict.files_changed} file(s), "
            f"{verdict.call_sites_changed} call site(s), tests exit {verdict.test_exit}"
            + (f" ({verdict.tests_passed} passed)" if verdict.tests_passed is not None else "")
            + ".",
        )
        log.info("impact %s: PR #%s opened on %s (%s)", impact.id, number, repo.full_name, branch)
        return {
            "impact_id": str(impact.id),
            "status": "pr_open",
            "pr_number": number,
            "branch": branch,
        }
    except SandboxTimeout as e:
        _fail(
            session,
            impact,
            f"sandbox wall clock exceeded: {e}",
            excerpt=sandbox.log_excerpt(),
            tokens=tokens_in + tokens_out,
        )
        return {"impact_id": str(impact.id), "status": "failed", "timeout": True}
    except SandboxError as e:
        _fail(
            session,
            impact,
            f"sandbox error: {e}",
            excerpt=sandbox.log_excerpt(),
            tokens=tokens_in + tokens_out,
        )
        return {"impact_id": str(impact.id), "status": "failed", "error": str(e)}
    except Exception as e:  # noqa: BLE001 - record an honest failure, then let the queue see it
        log.exception("impact %s: unexpected fixer failure", impact.id)
        try:
            session.rollback()
            impact = session.get(Impact, impact.id) or impact
            _fail(
                session,
                impact,
                f"unexpected error: {type(e).__name__}: {e}",
                excerpt=sandbox.log_excerpt(),
                tokens=tokens_in + tokens_out,
            )
        except Exception:  # noqa: BLE001
            log.exception("impact %s: could not record the failure", impact.id)
        return {
            "impact_id": str(impact.id),
            "status": "failed",
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        log.info(
            "fix_impact tokens_in=%s tokens_out=%s model=%s impact=%s repo=%s",
            tokens_in,
            tokens_out,
            model or settings.llm_fixer_model,
            impact.id,
            repo.full_name,
        )
        unregister_active(sandbox)
        try:
            sandbox.cleanup()
        except Exception:  # noqa: BLE001
            log.exception("impact %s: sandbox cleanup failed", impact.id)


# --------------------------------------------------------------------------------------
# PR / CI outcomes (seams used by the webhook handlers)
# --------------------------------------------------------------------------------------


def _impact_for_pr(session: Session, repo: Repo, pr_number: int) -> Impact | None:
    return session.scalar(
        select(Impact)
        .where(Impact.repo_id == repo.id, Impact.github_pr_number == int(pr_number))
        .order_by(Impact.created_at.desc())
        .limit(1)
    )


def _pr_head_sha(repo: Repo, pr_number: int) -> str | None:
    """The PR's current head sha via PyGithub (None when it cannot be read)."""
    try:
        org = repo.org
        app = get_github_app()
        pr = (
            app.installation_client(int(org.github_installation_id))
            .get_repo(repo.full_name)
            .get_pull(int(pr_number))
        )
        head = getattr(pr, "head", None)
        sha = getattr(head, "sha", None)
        return str(sha) if sha else None
    except Exception as e:  # noqa: BLE001
        log.info("could not read head sha of %s#%s: %s", repo.full_name, pr_number, e)
        return None


def merged_outcome(pushed_sha: str | None, head_sha: str | None) -> str:
    """``merged_as_is`` when the merged head is exactly what Kintsua pushed, ``merged_edited``
    when it differs, ``merged_unverified`` when either sha is unknown."""
    if pushed_sha and head_sha:
        return "merged_as_is" if pushed_sha.lower() == head_sha.lower() else "merged_edited"
    return "merged_unverified"


def record_pr_outcome(
    session: Session,
    repo: Repo,
    pr_number: int,
    merged: bool,
    action: str,
    *,
    head_sha: str | None = None,
    merge_commit_sha: str | None = None,
) -> Impact | None:
    """Record what happened to a Kintsua PR. ``closed`` + merged -> status ``merged`` with
    ``pr_outcome`` ``merged_as_is`` / ``merged_edited`` (head sha vs the sha Kintsua pushed);
    ``closed`` without merge -> ``pr_outcome`` ``closed``, status ``dismissed``. Other
    actions are recorded on ``ci_result`` and leave the status alone."""
    impact = _impact_for_pr(session, repo, pr_number)
    if impact is None:
        return None
    ci = dict(impact.ci_result or {})
    ci["pr_action"] = action
    ci["pr_merged"] = bool(merged)
    if merge_commit_sha:
        ci["merge_commit_sha"] = merge_commit_sha
    if action == "closed":
        if merged:
            head = head_sha or _pr_head_sha(repo, pr_number)
            if head:
                ci["merged_head_sha"] = head
            impact.pr_outcome = merged_outcome(ci.get("pushed_sha"), head)
            impact.status = "merged"
        else:
            impact.pr_outcome = "closed"
            if impact.status not in ("merged", "failed"):
                impact.status = "dismissed"
            impact.fix_reason = impact.fix_reason or "PR closed without merge"
        impact.resolved_at = impact.resolved_at or utcnow()
    elif action == "reopened" and impact.status == "dismissed" and impact.pr_outcome == "closed":
        impact.status = "pr_open"
        impact.pr_outcome = None
        impact.resolved_at = None
    impact.ci_result = ci
    session.commit()
    log.info(
        "impact %s: PR #%s %s merged=%s -> %s/%s",
        impact.id,
        pr_number,
        action,
        merged,
        impact.status,
        impact.pr_outcome,
    )
    return impact


def _ci_excerpt(details: dict[str, Any]) -> str:
    for key in ("log", "log_excerpt", "output", "text", "summary"):
        v = details.get(key)
        if isinstance(v, str) and v.strip():
            return tail_text(redact_secrets(v), lines=LOG_EXCERPT_LINES, max_chars=6000)
    return ""


def record_ci_result(
    session: Session, repo: Repo, pr_number: int, conclusion: str, details: dict[str, Any] | None
) -> Impact | None:
    """Store the customer's CI result on the impact. A failing conclusion moves it to
    ``failed`` with an issue comment carrying the log excerpt; success leaves ``pr_open``."""
    impact = _impact_for_pr(session, repo, pr_number)
    if impact is None:
        return None
    details = dict(details or {})
    ci = dict(impact.ci_result or {})
    ci["conclusion"] = conclusion
    ci["details"] = {k: v for k, v in details.items() if k not in ("log", "output", "text")}
    ci["recorded_at"] = utcnow().isoformat()
    impact.ci_result = ci
    if conclusion in CI_FAILURE_CONCLUSIONS and impact.status == "pr_open":
        name = details.get("name") or details.get("check_name") or "CI"
        url = details.get("details_url") or details.get("html_url")
        reason = f"CI {name} reported {conclusion} on PR #{pr_number}"
        impact.status = "failed"
        impact.fix_reason = reason
        impact.resolved_at = utcnow()
        session.commit()
        excerpt = _ci_excerpt(details)
        body = (
            f"{reason}. Kintsua could not fix this automatically; the PR stays open for a human "
            "to finish or close."
        )
        if url:
            body += f"\n\nCI details: {url}"
        if excerpt:
            body += f"\n\n```\n{_fence_safe(excerpt)}\n```"
        _comment(session, impact, body)
    else:
        session.commit()
    log.info("impact %s: CI %s on PR #%s -> %s", impact.id, conclusion, pr_number, impact.status)
    return impact


@job_handler(JOB_PR_OUTCOME)
def handle_pr_outcome(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        repo_id = UUID(str(payload["repo_id"]))
        pr_number = int(payload["pr_number"])
    except (KeyError, ValueError, TypeError) as e:
        raise NonRetryable(f"pr_outcome: bad payload {payload!r}: {e}")
    repo = session.get(Repo, repo_id)
    if repo is None:
        raise NonRetryable(f"pr_outcome: repo {repo_id} does not exist")
    impact = record_pr_outcome(
        session,
        repo,
        pr_number,
        bool(payload.get("merged")),
        str(payload.get("action") or "closed"),
        head_sha=payload.get("head_sha"),
        merge_commit_sha=payload.get("merge_commit_sha"),
    )
    if impact is None:
        return {"pr_number": pr_number, "impact": None}
    return {
        "pr_number": pr_number,
        "impact": str(impact.id),
        "status": impact.status,
        "pr_outcome": impact.pr_outcome,
    }


__all__ = [
    "CI_FAILURE_CONCLUSIONS",
    "FIXABLE_CATEGORIES",
    "build_fix_context",
    "effective_fix_mode",
    "fix_impact_dedupe_key",
    "handle_fix_impact",
    "handle_pr_outcome",
    "merged_outcome",
    "plan_refusal",
    "record_ci_result",
    "record_pr_outcome",
    "refusal_reason",
    "run_install",
]
