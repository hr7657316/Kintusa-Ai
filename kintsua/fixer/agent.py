"""The bounded tool-using loop that turns a ``FixContext`` into a patch in the sandbox.

Not an open-ended coding agent: five tools (``read_file``, ``search``, ``apply_patch``,
``run_tests``, ``done``), at most ``settings.fix_max_steps`` model turns, strict JSON
schemas on every tool, a refusal rule on the patch tool (no auth/secrets files, no
secret-looking additions), and a size guard that stops the loop as soon as the sandbox
diff exceeds ``settings.fix_max_changed_lines``.

The loop talks to the model only through ``kintsua.core.llm`` (``complete_tools``) so
tests script it with ``FakeLLM``. Tokens are summed per job for the unit-cost log line.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from kintsua.core.config import Settings, get_settings
from kintsua.core.llm import LLM, ChatMessage, LLMRefused, ToolCall, ToolResult, ToolSpec
from kintsua.fixer.patch import (
    changed_line_count,
    diff_stat,
    is_secret_path,
    secret_line_hits,
)
from kintsua.fixer.prompts import SYSTEM_PROMPT, build_user_message
from kintsua.fixer.sandbox import IGNORED_DIRS, Sandbox, SandboxError, SandboxTimeout
from kintsua.fixer.testcmd import TestPlan

log = logging.getLogger(__name__)

AgentStatus = Literal["patched", "needs_human", "gave_up", "error"]

MAX_READ_LINES = 400
MAX_SEARCH_HITS = 200
MAX_TOOL_OUTPUT_CHARS = 12_000
TEST_TAIL_LINES = 80
CONTEXT_LINES = 20


# --------------------------------------------------------------------------------------
# Context in / outcome out
# --------------------------------------------------------------------------------------


@dataclass
class CallSiteContext:
    file_path: str
    line: int
    operation: str
    symbol: str | None = None
    args_shape: dict[str, Any] = field(default_factory=dict)
    context: str = ""  # numbered source lines around ``line``
    call_site_id: str | None = None
    in_tests: bool = False


@dataclass
class FixContext:
    """Everything the agent and the validator need, DB-free."""

    repo_full_name: str
    default_branch: str
    vendor: str
    source_kind: str
    severity: str
    category: str
    summary: str
    migration_hint: str | None = None
    affected_paths: list[str] = field(default_factory=list)
    ops: list[dict[str, Any]] = field(default_factory=list)
    vendor_text: str | None = None
    call_sites: list[CallSiteContext] = field(default_factory=list)
    test_plan: TestPlan | None = None
    source_display: str = ""
    from_version: str | None = None
    to_version: str | None = None
    detected_at: datetime | None = None
    new_input_schema: dict[str, Any] | None = None
    tool_name: str | None = None
    issue_number: int | None = None
    change_event_id: str | None = None
    impact_id: str | None = None
    language: str = "python"
    import_radius: int | None = None  # repo override of settings.fix_import_radius
    base_sha: str | None = None

    @property
    def call_site_files(self) -> list[str]:
        return sorted({s.file_path for s in self.call_sites if s.file_path})


@dataclass
class AgentOutcome:
    status: AgentStatus
    reason: str
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    patch_summary: str = ""
    model: str | None = None
    needs_human: bool = False
    tool_calls: list[str] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)
    tests_ran: bool = False
    last_test_exit: int | None = None

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------------------
# Tool contract (strict schemas: additionalProperties false, every property required)
# --------------------------------------------------------------------------------------


def _schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="read_file",
        description=(
            "Read a file from the repository with line numbers. Pass start_line/end_line "
            "(1-based, inclusive) to read a slice, or null for the whole file (first 400 lines)."
        ),
        input_schema=_schema(
            {
                "path": {"type": "string", "description": "Repository-relative path"},
                "start_line": {"type": ["integer", "null"]},
                "end_line": {"type": ["integer", "null"]},
            }
        ),
    ),
    ToolSpec(
        name="search",
        description=(
            "Search file contents with a regular expression (Python re syntax). Returns up to "
            "200 'path:line: text' hits. glob limits the files searched (e.g. '**/*.py'); null "
            "searches every text file."
        ),
        input_schema=_schema(
            {
                "pattern": {"type": "string"},
                "glob": {"type": ["string", "null"]},
            }
        ),
    ),
    ToolSpec(
        name="apply_patch",
        description=(
            "Replace exactly one occurrence of `old` with `new` in `path`. `old` must match the "
            "file text exactly and be unique (include surrounding lines to disambiguate). Fails "
            "on files outside the repository, binary files, and files that handle authentication "
            "or secrets."
        ),
        input_schema=_schema(
            {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            }
        ),
    ),
    ToolSpec(
        name="run_tests",
        description="Run the repository's test command and return its exit code and output tail.",
        input_schema=_schema({}),
    ),
    ToolSpec(
        name="done",
        description=(
            "Finish. reason: one or two sentences for the pull request's review notes. "
            "needs_human: true when the fix needs a decision a person must make (then the patch "
            "is not shipped)."
        ),
        input_schema=_schema(
            {
                "reason": {"type": "string"},
                "needs_human": {"type": "boolean"},
            }
        ),
    ),
]


# --------------------------------------------------------------------------------------
# Tool execution
# --------------------------------------------------------------------------------------


class PatchTooLarge(Exception):
    pass


def number_lines(text: str, start: int = 1) -> str:
    width = max(4, len(str(start + text.count("\n"))))
    return "\n".join(f"{i:>{width}} | {ln}" for i, ln in enumerate(text.splitlines(), start=start))


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(truncated, {len(text) - limit} more characters)"


class ToolRunner:
    """Executes the model's tool calls against the sandbox, enforcing the refusal rule."""

    def __init__(self, sandbox: Sandbox, ctx: FixContext, settings: Settings):
        self.sandbox = sandbox
        self.ctx = ctx
        self.settings = settings
        self.patches_applied = 0
        self.tests_ran = False
        self.last_test_exit: int | None = None

    # ---- read_file ----------------------------------------------------------------

    def read_file(self, path: str, start_line: int | None, end_line: int | None) -> str:
        text = self.sandbox.read_file(path)
        lines = text.splitlines()
        total = len(lines)
        start = max(1, int(start_line or 1))
        if end_line is None:
            end = min(total, start + MAX_READ_LINES - 1)
        else:
            end = min(total, int(end_line))
        if start > total:
            return f"{path}: {total} lines; start_line {start} is past the end"
        chunk = "\n".join(lines[start - 1 : end])
        header = f"{path} (lines {start}-{end} of {total})"
        return _clip(f"{header}\n{number_lines(chunk, start)}")

    # ---- search -------------------------------------------------------------------

    def search(self, pattern: str, glob: str | None) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise SandboxError(f"invalid regular expression: {e}")
        files = self.sandbox.list_files(glob or "**/*")
        hits: list[str] = []
        scanned = 0
        for rel in files:
            if any(part in IGNORED_DIRS for part in rel.split("/")):
                continue
            if rel == ".git" or rel.startswith(".git/"):
                continue
            try:
                text = self.sandbox.read_file(rel)
            except SandboxError:
                continue  # binary or unreadable
            scanned += 1
            for i, ln in enumerate(text.splitlines(), start=1):
                if rx.search(ln):
                    hits.append(f"{rel}:{i}: {ln.strip()[:300]}")
                    if len(hits) >= MAX_SEARCH_HITS:
                        break
            if len(hits) >= MAX_SEARCH_HITS:
                break
        if not hits:
            return f"no matches for {pattern!r} in {scanned} files"
        suffix = f"\n…(stopped at {MAX_SEARCH_HITS} hits)" if len(hits) >= MAX_SEARCH_HITS else ""
        return _clip("\n".join(hits) + suffix)

    # ---- apply_patch ----------------------------------------------------------------

    def apply_patch(self, path: str, old: str, new: str) -> str:
        rel = self.sandbox.repo_path(path)  # raises on escapes / .git
        if is_secret_path(rel):
            raise SandboxError(
                f"refused: {rel} looks like authentication or secrets handling, which Kintsua "
                "never patches. Call done with needs_human=true if the fix requires it."
            )
        if not self.sandbox.file_exists(rel):
            raise SandboxError(f"{rel} does not exist; the fix must stay in existing files")
        added = [ln for ln in new.splitlines() if ln not in old.splitlines()]
        for ln in added:
            if secret_line_hits(ln):
                raise SandboxError(
                    "refused: the new text adds credential-like content (api_key=, token=, "
                    "Authorization ...). Kintsua never patches secrets handling."
                )
        ok, msg = self.sandbox.apply_patch({"path": rel, "old": old, "new": new})
        if not ok:
            raise SandboxError(msg)
        self.patches_applied += 1
        limit = self.settings.fix_max_changed_lines
        try:
            n = changed_line_count(self.sandbox.diff())
        except SandboxError as e:  # pragma: no cover - git trouble
            log.warning("could not measure the diff: %s", e)
            n = 0
        if n > limit:
            raise PatchTooLarge(f"patch too large: {n} changed lines > {limit}")
        return f"{msg}; diff is now {n} changed lines (limit {limit})"

    # ---- run_tests ------------------------------------------------------------------

    def run_tests(self) -> str:
        plan = self.ctx.test_plan
        if plan is None or not plan.command:
            raise SandboxError("this repository has no test command; rely on static checks")
        res = self.sandbox.run(plan.command, timeout_s=float(self.settings.fix_test_timeout_s))
        self.tests_ran = True
        self.last_test_exit = res.exit_code
        status = "timed out" if res.timed_out else f"exit {res.exit_code}"
        return _clip(f"$ {plan.command}\n{status}\n{res.tail(TEST_TAIL_LINES)}")


def _arg(call: ToolCall, name: str, default: Any = None) -> Any:
    v = call.input.get(name, default) if isinstance(call.input, dict) else default
    return default if v is None else v


# --------------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------------


def run_agent(
    sandbox: Sandbox,
    context: FixContext,
    llm: LLM,
    *,
    max_steps: int | None = None,
    settings: Settings | None = None,
) -> AgentOutcome:
    """Run the bounded loop; never raises for model or tool trouble (returns ``error``)."""
    settings = settings or get_settings()
    max_steps = int(max_steps or settings.fix_max_steps)
    model = llm.fixer_model()
    runner = ToolRunner(sandbox, context, settings)
    outcome = AgentOutcome(status="gave_up", reason="", model=model)
    messages: list[ChatMessage] = [ChatMessage(role="user", content=build_user_message(context))]
    system = SYSTEM_PROMPT

    def finish(status: AgentStatus, reason: str, needs_human: bool = False) -> AgentOutcome:
        outcome.status = status
        outcome.reason = reason
        outcome.needs_human = needs_human
        outcome.tests_ran = runner.tests_ran
        outcome.last_test_exit = runner.last_test_exit
        outcome.patch_summary = summarize_patch(sandbox)
        outcome.transcript.append(f"finished: {status}: {reason}")
        log.info(
            "fix agent %s: %s after %s steps (%s tool calls, tokens in=%s out=%s)",
            status,
            reason[:200],
            outcome.steps,
            len(outcome.tool_calls),
            outcome.tokens_in,
            outcome.tokens_out,
        )
        return outcome

    for _ in range(max_steps):
        try:
            sandbox.deadline.check("agent step")
        except SandboxTimeout as e:
            # the usual expiry point (a run clamped to the budget returns normally): return
            # the partial outcome so the job still records the tokens it spent
            return finish("error", str(e))
        try:
            turn = llm.complete_tools(system, messages, TOOL_SPECS, model=model)
        except LLMRefused as e:
            return finish("gave_up", f"model refused: {e}")
        except SandboxTimeout as e:
            return finish("error", str(e))
        except Exception as e:  # noqa: BLE001 - provider trouble is an honest failure
            log.exception("fix agent: model call failed")
            return finish("error", f"model call failed: {type(e).__name__}: {e}")
        outcome.steps += 1
        outcome.tokens_in += int(turn.usage.input_tokens or 0)
        outcome.tokens_out += int(turn.usage.output_tokens or 0)
        if turn.usage.model:
            outcome.model = turn.usage.model
        messages.append(ChatMessage(role="assistant", content=turn))
        if turn.text:
            outcome.transcript.append(f"model: {turn.text[:500]}")

        if turn.stop_reason == "refusal":
            return finish("gave_up", "model refused the task")
        if not turn.tool_calls:
            if turn.stop_reason == "max_tokens":
                return finish("gave_up", "model output hit max_tokens without calling done")
            return finish("gave_up", "model stopped without calling done")

        results: list[ToolResult] = []
        done_call: ToolCall | None = None
        for call in turn.tool_calls:
            outcome.tool_calls.append(call.name)
            if call.name == "done":
                done_call = call
                results.append(ToolResult(tool_call_id=call.id, content="ok"))
                continue
            try:
                content = _execute(runner, call)
                results.append(ToolResult(tool_call_id=call.id, content=content))
                outcome.transcript.append(f"{call.name}({_short_args(call)}) -> ok")
            except PatchTooLarge as e:
                results.append(ToolResult(tool_call_id=call.id, content=str(e), is_error=True))
                messages.append(ChatMessage(role="tool", content=results))
                return finish("gave_up", str(e))
            except SandboxTimeout as e:
                results.append(ToolResult(tool_call_id=call.id, content=str(e), is_error=True))
                messages.append(ChatMessage(role="tool", content=results))
                return finish("error", str(e))
            except (SandboxError, ValueError, TypeError, KeyError) as e:
                results.append(
                    ToolResult(tool_call_id=call.id, content=f"error: {e}", is_error=True)
                )
                outcome.transcript.append(
                    f"{call.name}({_short_args(call)}) -> error: {str(e)[:200]}"
                )
            except Exception as e:  # noqa: BLE001 - keep the loop honest, not crashing
                log.exception("fix agent: tool %s crashed", call.name)
                results.append(
                    ToolResult(
                        tool_call_id=call.id,
                        content=f"error: {type(e).__name__}: {e}",
                        is_error=True,
                    )
                )
        messages.append(ChatMessage(role="tool", content=results))

        if done_call is not None:
            reason = str(_arg(done_call, "reason", "") or "").strip() or "no reason given"
            needs_human = bool(_arg(done_call, "needs_human", False))
            if needs_human:
                return finish("needs_human", reason, needs_human=True)
            if not _has_changes(sandbox):
                return finish("gave_up", f"done without changes: {reason}")
            return finish("patched", reason)

    return finish("gave_up", f"max steps ({max_steps}) reached without done")


def _execute(runner: ToolRunner, call: ToolCall) -> str:
    name = call.name
    if name == "read_file":
        return runner.read_file(
            str(_arg(call, "path", "")),
            _int_or_none(_arg(call, "start_line")),
            _int_or_none(_arg(call, "end_line")),
        )
    if name == "search":
        glob = _arg(call, "glob")
        return runner.search(str(_arg(call, "pattern", "")), str(glob) if glob else None)
    if name == "apply_patch":
        return runner.apply_patch(
            str(_arg(call, "path", "")), str(_arg(call, "old", "")), str(_arg(call, "new", ""))
        )
    if name == "run_tests":
        return runner.run_tests()
    raise SandboxError(f"unknown tool {name!r}")


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _short_args(call: ToolCall) -> str:
    try:
        s = json.dumps(call.input, sort_keys=True)
    except (TypeError, ValueError):
        s = str(call.input)
    return s[:120]


def _has_changes(sandbox: Sandbox) -> bool:
    try:
        return bool(sandbox.diff().strip())
    except SandboxError:
        return False


def summarize_patch(sandbox: Sandbox) -> str:
    try:
        stat = diff_stat(sandbox.diff())
    except SandboxError:
        return ""
    if not stat:
        return "no changes"
    parts = [f"{path} (+{a}/-{r})" for path, (a, r) in sorted(stat.items())]
    total = sum(a + r for a, r in stat.values())
    return f"{len(stat)} file(s), {total} changed lines: " + ", ".join(parts)


def read_context(sandbox: Sandbox, file_path: str, line: int, radius: int = CONTEXT_LINES) -> str:
    """Numbered source lines ``line ± radius`` from the sandbox (empty when unreadable)."""
    try:
        text = sandbox.read_file(file_path)
    except SandboxError:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""
    line = max(1, int(line or 1))
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    return number_lines("\n".join(lines[start - 1 : end]), start)


__all__ = [
    "CONTEXT_LINES",
    "TOOL_SPECS",
    "AgentOutcome",
    "CallSiteContext",
    "FixContext",
    "ToolRunner",
    "number_lines",
    "read_context",
    "run_agent",
    "summarize_patch",
]
