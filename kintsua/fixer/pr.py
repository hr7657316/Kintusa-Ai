"""PR creation: branch naming, the PR body template from the spec, commit + push from the
sandbox, and opening the pull request through PyGithub under the per-org write lock.

Safety rules from the spec: never push to a protected branch, never force-push, only
push to branches Kintsua owns (``kintsua/<vendor>-<short-hash>``), and never persist the
installation token in the clone (the push uses the token URL once, on the command line
of a scrubbed subprocess, and the clone's remote stays token-free).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from kintsua.core.github import (
    BRANCH_PREFIX,
    ISSUE_PREFIX,
    LABEL,
    LABEL_BREAKING,
    LABEL_DEGRADED,
    is_protected_branch,
)
from kintsua.core.util import short_hash
from kintsua.fixer.sandbox import Sandbox, SandboxError, scrub

if TYPE_CHECKING:  # pragma: no cover
    from kintsua.fixer.agent import FixContext
    from kintsua.fixer.validate import Verdict

log = logging.getLogger(__name__)

PR_BODY_TEMPLATE = """## What changed upstream
{vendor} {source_kind} {from_version} → {to_version}, detected {detected_at}.
{summary}

## What this PR changes
{files_changed} files, {call_sites_changed} call sites.
{changes}

## How it was verified
- Test command: `{test_cmd}` → exit {test_exit} ({tests_passed} passed)
- Baseline on unpatched tree: {baseline_result}
- Static checks: {static_result}

## Review notes
{review_notes}

Closes #{issue_number}. Opened by Kintsua.
"""

LABEL_COLOURS: dict[str, tuple[str, str]] = {
    LABEL: ("5319e7", "Opened by Kintsua"),
    LABEL_BREAKING: ("b60205", "Breaking upstream change (Kintsua)"),
    LABEL_DEGRADED: ("d93f0b", "Degraded / deprecated upstream (Kintsua)"),
}


class PushRefused(RuntimeError):
    """A safety rule stopped the push (protected branch, foreign branch, no changes)."""


@dataclass
class PushResult:
    branch: str
    sha: str
    replaced_existing: bool = False


# --------------------------------------------------------------------------------------
# Naming and rendering
# --------------------------------------------------------------------------------------


def vendor_slug(name: str | None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:30].strip("-")
    return slug or "vendor"


def branch_name(vendor: str | None, change_event_id: Any) -> str:
    """``kintsua/<vendor>-<short-hash>``; the hash comes from the change event id so a
    retry of the same impact lands on the same branch."""
    return f"{BRANCH_PREFIX}{vendor_slug(vendor)}-{short_hash(str(change_event_id))}"


def _plain(text: Any, max_len: int | None = None, *, block: bool = False) -> str:
    """One-line inert Markdown for vendor-supplied text: no HTML, and (for ``block``
    fields that start a line) no headings or list markers."""
    s = "" if text is None else str(text)
    s = re.sub(r"\s+", " ", s.replace("\r", "")).strip()
    if max_len and len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if block:
        s = re.sub(r"^([-+#>*]|\d+\.)", r"\\\1", s)
    return s


def _fmt_dt(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d %H:%M UTC") if d else "unknown time"


def pr_title(ctx: FixContext) -> str:
    summary = re.sub(r"\s+", " ", ctx.summary or "").strip() or "adapt to upstream change"
    title = f"{ISSUE_PREFIX} fix: {summary}"
    return title if len(title) <= 200 else title[:199].rstrip() + "…"


def render_pr_body(
    ctx: FixContext,
    verdict: Verdict,
    issue_number: int | None,
    *,
    agent_notes: str | None = None,
) -> str:
    """The PR body, exactly the template from the spec."""
    changes = "\n".join(verdict.file_notes) if verdict.file_notes else "- (see the diff)"
    notes: list[str] = []
    if ctx.migration_hint:
        notes.append(f"Migration hint: {_plain(ctx.migration_hint, 1500, block=True)}")
    if agent_notes:
        notes.append(f"Agent: {_plain(agent_notes, 1500)}")
    if verdict.preexisting_failures:
        notes.append(
            "Pre-existing failing tests (unchanged by this PR): "
            + ", ".join(f"`{t}`" for t in verdict.preexisting_failures[:10])
        )
    if verdict.mcp_check:
        notes.append(f"MCP schema check: {_plain(verdict.mcp_check, 600)}")
    if not notes:
        notes.append("No open questions.")
    test_exit = verdict.test_exit if verdict.test_exit is not None else "n/a"
    tests_passed = verdict.tests_passed if verdict.tests_passed is not None else "n/a"
    body = PR_BODY_TEMPLATE.format(
        vendor=_plain(ctx.vendor or ctx.source_display or "vendor"),
        source_kind=_plain(ctx.source_kind),
        from_version=_plain(ctx.from_version or "?"),
        to_version=_plain(ctx.to_version or "?"),
        detected_at=_fmt_dt(ctx.detected_at),
        summary=_plain(ctx.summary, 1000, block=True),
        files_changed=verdict.files_changed,
        call_sites_changed=verdict.call_sites_changed,
        changes=changes,
        test_cmd=(
            verdict.test_command or (ctx.test_plan.command if ctx.test_plan else None) or "none"
        ),
        test_exit=test_exit,
        tests_passed=tests_passed,
        baseline_result=verdict.baseline_result or "not run",
        static_result=verdict.static_result or "none configured",
        review_notes="\n".join(notes),
        issue_number=issue_number if issue_number is not None else "?",
    )
    if issue_number is None:
        body = body.replace("Closes #?. Opened by Kintsua.", "Opened by Kintsua.")
    return body


def commit_message(ctx: FixContext) -> str:
    summary = re.sub(r"\s+", " ", ctx.summary or "").strip() or "adapt to upstream change"
    first = f"fix({vendor_slug(ctx.vendor)}): {summary}"
    if len(first) > 72:
        first = first[:71].rstrip() + "…"
    body = [
        "",
        f"Upstream: {ctx.vendor} {ctx.source_kind} {ctx.from_version or '?'} -> {ctx.to_version or '?'}",
    ]
    if ctx.issue_number:
        body.append(f"Closes #{ctx.issue_number}")
    body.append("")
    body.append("Opened by Kintsua.")
    return "\n".join([first, *body])


# --------------------------------------------------------------------------------------
# git: commit and push (tests monkeypatch ``remote_branch_exists`` / ``push_branch``)
# --------------------------------------------------------------------------------------


def remote_branch_exists(sandbox: Sandbox, clone_url: str, branch: str) -> bool:
    res = sandbox.git(
        "ls-remote", "--heads", clone_url, f"refs/heads/{branch}", timeout_s=120, network=True
    )
    if not res.ok:
        raise SandboxError(f"git ls-remote failed: {scrub(res.tail(5), sandbox.secrets)}")
    return any(line.strip() for line in res.stdout.splitlines())


def delete_remote_branch(sandbox: Sandbox, clone_url: str, branch: str) -> None:
    res = sandbox.git(
        "push", clone_url, "--delete", f"refs/heads/{branch}", timeout_s=120, network=True
    )
    if not res.ok:
        raise SandboxError(
            f"could not delete our stale branch {branch}: {scrub(res.tail(5), sandbox.secrets)}"
        )


def push_branch(sandbox: Sandbox, clone_url: str, branch: str) -> None:
    """A plain (never forced) push of the sandbox branch to the remote."""
    res = sandbox.git(
        "push",
        clone_url,
        f"refs/heads/{branch}:refs/heads/{branch}",
        timeout_s=300,
        network=True,
    )
    if not res.ok:
        raise SandboxError(
            f"git push failed (exit {res.exit_code}): {scrub(res.tail(10), sandbox.secrets)}"
        )


def commit_and_push(
    sandbox: Sandbox,
    branch: str,
    message: str,
    *,
    clone_url: str,
    gh_repo: Any = None,
    base_branch: str | None = None,
) -> PushResult:
    """Commit the sandbox changes (exactly ``sandbox.changed_files()``, the validated
    patch) on ``branch`` and push it. Refuses protected branches, branches that are not
    ours, and pushes with nothing to commit."""
    if not branch.startswith(BRANCH_PREFIX):
        raise PushRefused(f"refusing to push to {branch!r}: not a {BRANCH_PREFIX} branch")
    if base_branch and branch == base_branch:
        raise PushRefused(f"refusing to push to the base branch {branch!r}")
    if gh_repo is not None and is_protected_branch(gh_repo, branch):
        raise PushRefused(f"refusing to push to protected branch {branch!r}")
    if gh_repo is not None and base_branch and is_protected_branch(gh_repo, base_branch):
        log.info(
            "base branch %s is protected; the PR targets it, nothing is pushed there", base_branch
        )
    files = sandbox.changed_files()
    if not files:
        raise PushRefused("nothing to commit")

    replaced = False
    if remote_branch_exists(sandbox, clone_url, branch):
        # by construction the name carries our prefix, so the branch is a stale one of ours
        log.info("remote branch %s exists (ours); replacing it", branch)
        delete_remote_branch(sandbox, clone_url, branch)
        replaced = True

    sandbox.git_ok("checkout", "--quiet", "-B", branch)
    # only the patch: never ``--all``, which would commit what the install or the test run
    # left in the tree (lockfiles, *.egg-info, report files)
    sandbox.git_ok("add", "--all", "--", *files)
    commit = sandbox.git("commit", "--quiet", "--no-verify", "-m", message, timeout_s=120)
    if not commit.ok:
        raise SandboxError(f"git commit failed: {scrub(commit.tail(10), sandbox.secrets)}")
    sha = sandbox.head_sha()
    push_branch(sandbox, clone_url, branch)
    sandbox.record(f"pushed {branch} at {sha[:12]}")
    return PushResult(branch=branch, sha=sha, replaced_existing=replaced)


# --------------------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------------------


def ensure_labels(gh_repo: Any, names: list[str]) -> None:
    for name in names:
        try:
            gh_repo.get_label(name)
            continue
        except Exception:  # noqa: BLE001 - UnknownObjectException (real) / KeyError (fake)
            pass
        colour, description = LABEL_COLOURS.get(name, ("ededed", ""))
        try:
            gh_repo.create_label(name=name, color=colour, description=description)
        except Exception as e:  # noqa: BLE001
            log.info("could not create label %s: %s", name, e)


def severity_label(severity: str | None) -> str | None:
    if severity == "breaking":
        return LABEL_BREAKING
    if severity == "degraded":
        return LABEL_DEGRADED
    return None


def open_pull_request(
    gh_repo: Any,
    title: str,
    body: str,
    head: str,
    base: str,
    *,
    labels: list[str] | None = None,
    write_lock: Any = None,
) -> int:
    """Create the PR (under the per-org write lock when given), label it, return its number."""
    labels = list(labels or [LABEL])

    def _do() -> int:
        ensure_labels(gh_repo, labels)
        pr = gh_repo.create_pull(title=title, body=body, head=head, base=base)
        try:
            adder = getattr(pr, "add_to_labels", None)
            if callable(adder):
                adder(*labels)
            else:  # pragma: no cover - older PyGithub: labels live on the issue
                gh_repo.get_issue(int(pr.number)).add_to_labels(*labels)
        except Exception as e:  # noqa: BLE001 - labels are nice-to-have
            log.info("could not label PR #%s: %s", getattr(pr, "number", "?"), e)
        return int(pr.number)

    if write_lock is not None:
        with write_lock:
            return _do()
    return _do()


__all__ = [
    "PR_BODY_TEMPLATE",
    "PushRefused",
    "PushResult",
    "branch_name",
    "commit_and_push",
    "commit_message",
    "delete_remote_branch",
    "ensure_labels",
    "open_pull_request",
    "pr_title",
    "push_branch",
    "remote_branch_exists",
    "render_pr_body",
    "severity_label",
    "vendor_slug",
]
