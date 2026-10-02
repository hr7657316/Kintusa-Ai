"""The validator is the product: no PR ships unless every check here passes.

``run_baseline`` runs the test command and the static checks on the **unpatched** tree
(cached per ``(repo, sha)`` under ``settings.data_dir/baselines``) so a repo with
pre-existing red tests still gets fixes: the patched tree may fail with the same set of
failing test ids as the baseline, never with new ones.

``validate`` then checks, in order:

1. the diff is non-empty, reverse-applies on the working tree and forward-applies on a
   fresh checkout of the base sha;
2. it touches only files that contain affected call sites or files within
   ``fix_import_radius`` import hops of them (a lightweight import graph for Python and
   TypeScript/JavaScript, relative modules resolved);
3. it changes at most ``fix_max_changed_lines`` lines;
4. no touched file matches the auth/secrets patterns and no added line looks like a
   credential (``api_key=``, ``token=``, ``Authorization`` ...);
5. the test command exits 0, or with the same failing test ids as the baseline (pytest
   ``FAILED path::test`` lines, jest/vitest ``✕``/``FAIL`` lines; exit codes when
   unparsable). A red run whose suite did not actually run (pytest collection errors,
   usage errors, no tests collected) never counts as "the same failures": that is a
   broken environment, not a verified patch;
6. static checks pass where present (or, when they already failed on the baseline, do
   not get worse);
7. for MCP tool changes, a synthetic call built from the patched call site's argument
   keys validates against the new ``inputSchema`` with ``jsonschema``.
"""

from __future__ import annotations

import ast
import json
import logging
import posixpath
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kintsua.core.config import Settings, get_settings
from kintsua.fixer.agent import FixContext
from kintsua.fixer.patch import (
    FilePatch,
    is_secret_path,
    parse_unified_diff,
    secret_line_hits,
)
from kintsua.fixer.sandbox import IGNORED_DIRS, RunResult, Sandbox, SandboxError, tail_text
from kintsua.fixer.testcmd import TestPlan

log = logging.getLogger(__name__)


TEST_TIMEOUT_S = 600.0  # fallback; the live value comes from Settings.fix_test_timeout_s


def _test_timeout_s(settings: Settings | None = None) -> float:
    try:
        return float((settings or get_settings()).fix_test_timeout_s)
    except Exception:  # noqa: BLE001
        return TEST_TIMEOUT_S


STATIC_TIMEOUT_S = 300.0
MAX_GRAPH_FILES = 4000
PY_EXTS = (".py",)
TS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


@dataclass
class StaticCheckResult:
    command: str
    exit_code: int
    violations: int
    output_tail: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class BaselineResult:
    sha: str | None
    exit_code: int | None  # None when the repo has no test command
    failing_tests: list[str] = field(default_factory=list)
    output_tail: str = ""
    tests_passed: int | None = None
    timed_out: bool = False
    static: dict[str, dict[str, Any]] = field(default_factory=dict)  # command -> result
    plan: dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    #: why the red run did not actually run the suite (collection errors, ...), else None
    not_run: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code in (0, None) and not self.timed_out

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("cached", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BaselineResult:
        return cls(
            sha=d.get("sha"),
            exit_code=d.get("exit_code"),
            failing_tests=list(d.get("failing_tests") or []),
            output_tail=str(d.get("output_tail") or ""),
            tests_passed=d.get("tests_passed"),
            timed_out=bool(d.get("timed_out")),
            static={str(k): dict(v) for k, v in (d.get("static") or {}).items()},
            plan=dict(d.get("plan") or {}),
            cached=True,
            not_run=d.get("not_run"),
        )

    def summary(self) -> str:
        if self.exit_code is None:
            parts = ["no test command"]
        elif self.timed_out:
            parts = ["tests timed out"]
        elif self.not_run:
            parts = [f"exit {self.exit_code}, tests did not run ({self.not_run})"]
        elif self.exit_code == 0:
            parts = [
                f"exit 0{f', {self.tests_passed} passed' if self.tests_passed is not None else ''}"
            ]
        else:
            n = len(self.failing_tests)
            parts = [
                f"exit {self.exit_code}, {n} failing test{'s' if n != 1 else ''}"
                if n
                else f"exit {self.exit_code}"
            ]
        for cmd, res in self.static.items():
            code = res.get("exit_code")
            parts.append(
                f"`{cmd}` exit {code}" + (f" ({res.get('violations')} findings)" if code else "")
            )
        return "; ".join(parts)


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    tests_passed: int | None = None
    test_exit: int | None = None
    baseline_result: str = ""
    static_result: str = ""
    files_changed: int = 0
    call_sites_changed: int = 0
    lines_changed: int = 0
    changed_files: list[str] = field(default_factory=list)
    failing_tests: list[str] = field(default_factory=list)
    test_output_tail: str = ""
    file_notes: list[str] = field(default_factory=list)
    applied: bool = False
    mcp_check: str | None = None
    test_command: str | None = None
    preexisting_failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------------------
# Output parsing
# --------------------------------------------------------------------------------------

# the id runs up to the `` - message`` separator; a ``[...]`` parametrize id may contain
# spaces (and even `` - ``), so it is matched as one bracket group
_PYTEST_FAIL_RE = re.compile(r"^(?:FAILED|ERROR)\s+((?:[^\s\[]|\[[^\]]*\])+?)(?:\s+-\s.*)?$")
_PYTEST_PASSED_RE = re.compile(r"(\d+) passed")
_JS_FAIL_MARK_RE = re.compile(r"^\s*(?:✕|×|✗)\s+(.+?)(?:\s+\(?\d+\s*ms\)?)?\s*$")
_JS_FAIL_FILE_RE = re.compile(r"^\s*FAIL\s+(\S+(?:\s+>\s+.+)?)\s*$")
_JEST_PASSED_RE = re.compile(r"Tests:\s+(?:\d+\s+\w+,\s+)*(\d+)\s+passed")
_VITEST_PASSED_RE = re.compile(r"Tests\s+(?:\d+\s+failed\s+\|\s+)?(\d+)\s+passed")
# ``path:line:col: CODE msg`` (concise / legacy) or the ``  --> path:line:col`` location
# line of ruff >= 0.9's default ``full`` format
_RUFF_VIOLATION_RE = re.compile(r"^(?:\S+:\d+:\d+:\s+\S+|\s*-->\s+\S+:\d+:\d+)")
_RUFF_FOUND_RE = re.compile(
    r"^Found (\d+) errors?(?: \((?:\d+ fixed, )?(\d+) remaining\))?", re.MULTILINE
)
_TSC_ERROR_RE = re.compile(r"error TS\d+:")


def parse_failing_tests(output: str, runner: str | None = None) -> list[str]:
    """Failing test ids from pytest / jest / vitest output (empty when unparsable)."""
    ids: list[str] = []
    for raw in (output or "").splitlines():
        line = raw.rstrip()
        m = _PYTEST_FAIL_RE.match(line)
        if m and "::" in m.group(1):
            ids.append(m.group(1))
            continue
        if (
            m
            and (m.group(1).endswith(".py") or "/" in m.group(1))
            and runner in (None, "pytest", "custom")
        ):
            ids.append(m.group(1))
            continue
        m = _JS_FAIL_FILE_RE.match(line)
        if m:
            ids.append(m.group(1).strip())
            continue
        m = _JS_FAIL_MARK_RE.match(line)
        if m:
            ids.append(m.group(1).strip())
    return sorted(dict.fromkeys(ids))


#: pytest exit codes that mean the suite never ran
PYTEST_NOT_RUN_EXITS: dict[int, str] = {
    2: "interrupted, e.g. collection errors",
    3: "internal error",
    4: "usage error",
    5: "no tests collected",
}
_PYTEST_NOT_RUN_RE = re.compile(
    r"Interrupted: \d+ errors? during collection|\bno tests ran\b", re.IGNORECASE
)


def suite_not_run_reason(
    exit_code: int | None,
    output: str,
    runner: str | None,
    failing: list[str],
    tests_passed: int | None,
) -> str | None:
    """Why a red test run did not actually run the suite (None when it passed or ran):
    a pytest exit code for interrupted/usage/no-tests, pytest's own "Interrupted: N errors
    during collection" / "no tests ran" lines (also behind ``make test`` or a custom
    command), or a failure list made only of collection errors (file paths, no
    ``::test``) with nothing passing."""
    if exit_code in (None, 0):
        return None
    if runner == "pytest" and exit_code in PYTEST_NOT_RUN_EXITS:
        return f"pytest exit {exit_code}: {PYTEST_NOT_RUN_EXITS[exit_code]}"
    m = _PYTEST_NOT_RUN_RE.search(output or "")
    if m:
        return m.group(0)
    if failing and not tests_passed and all("::" not in f and f.endswith(".py") for f in failing):
        return "only collection errors: " + ", ".join(failing[:5])
    return None


def parse_tests_passed(output: str) -> int | None:
    out = output or ""
    for rx in (_PYTEST_PASSED_RE, _JEST_PASSED_RE, _VITEST_PASSED_RE):
        hits = rx.findall(out)
        if hits:
            try:
                return int(hits[-1])
            except ValueError:
                continue
    return None


def count_violations(command: str, output: str) -> int:
    low = command.lower()
    lines = (output or "").splitlines()
    if "ruff" in low:
        # ruff's own summary is authoritative; count finding lines (either output format)
        # only when it is missing
        found = _RUFF_FOUND_RE.findall(output or "")
        if found:
            return sum(int(remaining or total) for total, remaining in found)
        return sum(1 for ln in lines if _RUFF_VIOLATION_RE.match(ln))
    if "tsc" in low:
        return sum(1 for ln in lines if _TSC_ERROR_RE.search(ln))
    return 0


# --------------------------------------------------------------------------------------
# Running checks
# --------------------------------------------------------------------------------------


def run_tests(
    sandbox: Sandbox, plan: TestPlan, *, timeout_s: float | None = None
) -> RunResult | None:
    """The test command, with ``timeout_s`` defaulting to ``Settings.fix_test_timeout_s``
    (``Sandbox.run`` still clamps it to the job's wall clock)."""
    if not plan.command:
        return None
    return sandbox.run(
        plan.command, timeout_s=timeout_s if timeout_s is not None else _test_timeout_s()
    )


def run_static_checks(
    sandbox: Sandbox, plan: TestPlan, *, timeout_s: float = STATIC_TIMEOUT_S
) -> dict[str, StaticCheckResult]:
    out: dict[str, StaticCheckResult] = {}
    for cmd in plan.static_checks:
        # one finding per line, whatever the repo's ruff version defaults to
        env = {"RUFF_OUTPUT_FORMAT": "concise"} if "ruff" in cmd.lower() else None
        res = sandbox.run(cmd, timeout_s=timeout_s, env=env)
        out[cmd] = StaticCheckResult(
            command=cmd,
            exit_code=res.exit_code,
            violations=count_violations(cmd, res.output),
            output_tail=res.tail(30),
            timed_out=res.timed_out,
        )
    return out


BASELINE_CACHE_VERSION = 2  # bump when BaselineResult gains a field a cached entry lacks


def _plan_fingerprint(plan: TestPlan) -> dict[str, Any]:
    return {
        "command": plan.command,
        "static_checks": list(plan.static_checks),
        "v": BASELINE_CACHE_VERSION,
    }


def baseline_cache_path(settings: Settings, repo_key: str, sha: str) -> Path:
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]+", "__", repo_key) or "repo"
    safe_sha = re.sub(r"[^0-9a-fA-F]", "", sha)[:40] or "unknown"
    return Path(settings.data_dir) / "baselines" / safe_repo / f"{safe_sha}.json"


def run_baseline(
    sandbox: Sandbox,
    plan: TestPlan,
    *,
    repo_key: str = "repo",
    sha: str | None = None,
    settings: Settings | None = None,
    use_cache: bool = True,
) -> BaselineResult:
    """Tests + static checks on the unpatched tree, cached per ``(repo, sha)``."""
    settings = settings or get_settings()
    sha = sha or sandbox.base_sha
    fingerprint = _plan_fingerprint(plan)
    path = baseline_cache_path(settings, repo_key, sha) if sha else None
    if use_cache and path is not None and path.exists():
        try:
            cached = BaselineResult.from_dict(json.loads(path.read_text("utf-8")))
            if cached.plan == fingerprint:
                log.info("baseline for %s@%s served from cache", repo_key, sha[:12] if sha else "?")
                return cached
        except (ValueError, OSError) as e:
            log.info("baseline cache unreadable (%s); rerunning", e)
    res = run_tests(sandbox, plan, timeout_s=_test_timeout_s(settings))
    static = run_static_checks(sandbox, plan)
    failing = parse_failing_tests(res.output, plan.runner) if res is not None else []
    passed = parse_tests_passed(res.output) if res is not None else None
    baseline = BaselineResult(
        sha=sha,
        exit_code=None if res is None else res.exit_code,
        failing_tests=failing,
        output_tail=res.tail(40) if res is not None else "",
        tests_passed=passed,
        timed_out=bool(res is not None and res.timed_out),
        static={
            cmd: {"exit_code": r.exit_code, "violations": r.violations, "timed_out": r.timed_out}
            for cmd, r in static.items()
        },
        plan=fingerprint,
        not_run=(
            suite_not_run_reason(res.exit_code, res.output, plan.runner, failing, passed)
            if res is not None and not res.timed_out
            else None
        ),
    )
    if use_cache and path is not None and not baseline.timed_out:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(baseline.as_dict(), indent=1, sort_keys=True), "utf-8")
        except OSError as e:  # pragma: no cover - a read-only data dir is not fatal
            log.warning("could not cache baseline: %s", e)
    log.info("baseline for %s@%s: %s", repo_key, (sha or "?")[:12], baseline.summary())
    return baseline


# --------------------------------------------------------------------------------------
# Import graph (radius rule)
# --------------------------------------------------------------------------------------

_PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([.\w]+)\s+import\s+([^\n#]+)|import\s+([\w.]+(?:\s*,\s*[\w.]+)*))"
)
_TS_IMPORT_RE = re.compile(
    r"""(?:import|export)\s+(?:[^'";]*?\s+from\s+)?['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)|import\(\s*['"]([^'"]+)['"]\s*\)"""
)


def _is_code_file(rel: str) -> bool:
    if any(part in IGNORED_DIRS for part in rel.split("/")):
        return False
    return rel.endswith(PY_EXTS) or rel.endswith(TS_EXTS)


def _python_candidates(importer: str, module: str, level: int, names: list[str]) -> list[str]:
    parts = module.split(".") if module else []
    cands: list[str] = []
    importer_dir = posixpath.dirname(importer)
    if level > 0:
        base = importer_dir.split("/") if importer_dir else []
        up = level - 1
        base = base[: len(base) - up] if up else base
        root = "/".join([*base, *parts])
        roots = [root]
    else:
        roots = []
        dir_parts = importer_dir.split("/") if importer_dir else []
        for i in range(len(dir_parts), -1, -1):  # importer's dir, its parents, then repo root
            prefix = "/".join(dir_parts[:i])
            roots.append("/".join([p for p in [prefix, *parts] if p]))
        roots.append("/".join(["src", *parts]))
        roots.append("/".join(["lib", *parts]))
    for root in dict.fromkeys(roots):
        if root:
            cands.append(f"{root}.py")
            cands.append(f"{root}/__init__.py")
        for name in names:
            if root:
                cands.append(f"{root}/{name}.py")
                cands.append(f"{root}/{name}/__init__.py")
            else:
                cands.append(f"{name}.py")
                cands.append(f"{name}/__init__.py")
    return [posixpath.normpath(c) for c in cands]


def _python_imports(importer: str, text: str) -> list[tuple[str, int, list[str]]]:
    """``(module, level, names)`` for every import statement."""
    out: list[tuple[str, int, list[str]]] = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        for line in text.splitlines():
            m = _PY_IMPORT_RE.match(line)
            if not m:
                continue
            if m.group(1) is not None:
                mod = m.group(1)
                level = len(mod) - len(mod.lstrip("."))
                names = [
                    n.strip().split(" as ")[0].strip()
                    for n in m.group(2).replace("(", "").replace(")", "").split(",")
                ]
                out.append((mod.lstrip("."), level, [n for n in names if n and n != "*"]))
            else:
                for mod in m.group(3).split(","):
                    out.append((mod.strip().split(" as ")[0].strip(), 0, []))
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, 0, []))
        elif isinstance(node, ast.ImportFrom):
            names = [a.name for a in node.names if a.name != "*"]
            out.append((node.module or "", node.level or 0, names))
    return out


def _ts_candidates(importer: str, spec: str) -> list[str]:
    if not spec.startswith("."):
        return []
    base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    cands = [base]
    stem = base
    for ext in (".js", ".jsx", ".mjs", ".cjs"):
        if base.endswith(ext):
            stem = base[: -len(ext)]
            break
    for ext in TS_EXTS:
        cands.append(f"{stem}{ext}")
    for ext in TS_EXTS:
        cands.append(f"{base}/index{ext}")
    return cands


def build_import_graph(sandbox: Sandbox, files: list[str] | None = None) -> dict[str, set[str]]:
    """Undirected adjacency between repo files that import each other."""
    files = [
        f for f in (files if files is not None else sandbox.list_files("**/*")) if _is_code_file(f)
    ]
    files = files[:MAX_GRAPH_FILES]
    present = set(files)
    graph: dict[str, set[str]] = {f: set() for f in files}

    def connect(a: str, b: str) -> None:
        if a == b or b not in present:
            return
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)

    for f in files:
        try:
            text = sandbox.read_file(f)
        except SandboxError:
            continue
        if f.endswith(PY_EXTS):
            for module, level, names in _python_imports(f, text):
                for cand in _python_candidates(f, module, level, names):
                    if cand in present:
                        connect(f, cand)
        else:
            for m in _TS_IMPORT_RE.finditer(text):
                spec = m.group(1) or m.group(2) or m.group(3)
                if not spec:
                    continue
                for cand in _ts_candidates(f, spec):
                    if cand in present:
                        connect(f, cand)
                        break
    return graph


def files_within_radius(graph: dict[str, set[str]], seeds: list[str], radius: int) -> set[str]:
    allowed = set(seeds)
    frontier = set(seeds)
    for _ in range(max(0, int(radius))):
        nxt: set[str] = set()
        for f in frontier:
            nxt.update(graph.get(f, set()))
        nxt -= allowed
        if not nxt:
            break
        allowed |= nxt
        frontier = nxt
    return allowed


# --------------------------------------------------------------------------------------
# MCP schema check
# --------------------------------------------------------------------------------------


def _tool_name_for(ctx: FixContext) -> str | None:
    if ctx.tool_name:
        return ctx.tool_name
    for p in ctx.affected_paths:
        if isinstance(p, str) and p.startswith("tool:"):
            return p.split(":", 1)[1]
    for s in ctx.call_sites:
        if s.operation and " " not in s.operation and "/" not in s.operation:
            return s.operation
    return None


def _dict_keys(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.Dict):
        keys: list[str] = []
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.append(k.value)
            elif k is None:
                return None  # ``**spread``: unknown shape
            else:
                return None
        return keys
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        return [kw.arg for kw in node.keywords if kw.arg]
    return None


#: keywords that carry the tool arguments (``call_tool("x", arguments=...)``)
_ARGUMENT_KWARGS = ("arguments", "args", "params", "input", "inputs")
#: keywords that carry the tool name, never an argument
_TOOL_NAME_KWARGS = ("name", "tool", "tool_name")
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _scope_nodes(scope: ast.AST) -> list[ast.AST]:
    """Every node of ``scope`` without descending into nested functions or classes."""
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        out.append(node)
        if not isinstance(node, _SCOPE_NODES):
            stack.extend(ast.iter_child_nodes(node))
    return out


def _enclosing_scope(tree: ast.Module, target: ast.AST) -> ast.AST:
    """The innermost function (or the module) whose own body contains ``target``."""
    scope: ast.AST = tree
    while True:
        inner = next(
            (
                n
                for n in _scope_nodes(scope)
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                and target in set(ast.walk(n))
            ),
            None,
        )
        if inner is None:
            return scope
        scope = inner


def _is_name(node: ast.AST | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _const_key(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _resolve_dict_name(tree: ast.Module, call: ast.Call, name: str) -> list[str] | None:
    """Keys of the dict a variable holds at ``call``: the nearest earlier assignment of a
    dict literal (or ``dict(...)``) in the same function, plus the keys later added with
    ``name["k"] = ...``, ``name.update({...}/k=...)`` or ``name.setdefault("k", ...)``.
    None when the value or a change to it cannot be read statically."""
    scope = _enclosing_scope(tree, call)
    nodes = _scope_nodes(scope)
    line = call.lineno
    assign: ast.Assign | ast.AnnAssign | None = None
    for node in nodes:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target = node.target
        else:
            continue
        if _is_name(target, name) and node.lineno < line:
            if assign is None or node.lineno > assign.lineno:
                assign = node
    if assign is None or assign.value is None:
        return None
    keys = _dict_keys(assign.value)
    if keys is None:
        return None
    keys = list(keys)
    for node in nodes:
        ln = getattr(node, "lineno", 0)
        if not (assign.lineno < ln <= line) or node is assign:
            continue
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
            if isinstance(node, ast.AugAssign) and _is_name(node.target, name):
                more = _dict_keys(node.value)  # ``params |= {...}``
                if more is None:
                    return None
                keys.extend(more)
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and _is_name(t.value, name):
                    k = _const_key(t.slice)
                    if k is None:
                        return None
                    keys = [x for x in keys if x != k]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if _is_name(node.func.value, name):
                attr = node.func.attr
                if attr == "update":
                    for a in node.args:
                        more = _dict_keys(a)
                        if more is None:
                            return None
                        keys.extend(more)
                    for kw in node.keywords:
                        if kw.arg is None:
                            return None
                        keys.append(kw.arg)
                elif attr in ("setdefault", "pop") and node.args:
                    k = _const_key(node.args[0])
                    if k is None:
                        return None
                    keys = [*keys, k] if attr == "setdefault" else [x for x in keys if x != k]
        for t in targets:
            if isinstance(t, ast.Subscript) and _is_name(t.value, name):
                k = _const_key(t.slice)
                if k is None:
                    return None  # ``params[key] = ...``: unknown key
                keys.append(k)
    return list(dict.fromkeys(keys))


def _argument_value_keys(tree: ast.Module, call: ast.Call, value: ast.AST) -> list[str] | None:
    if isinstance(value, ast.Constant) and value.value is None:
        return []  # ``arguments=None``: the call passes no arguments at all
    keys = _dict_keys(value)
    if keys is None and isinstance(value, ast.Name):
        keys = _resolve_dict_name(tree, call, value.id)
    return keys


def _call_argument_keys(
    tree: ast.Module, node: ast.Call, literal_kws: list[ast.keyword]
) -> list[str] | None:
    """The argument keys one call passes, or None when they cannot be read statically."""
    literal_pos = next(
        (
            i
            for i, a in enumerate(node.args)
            if isinstance(a, ast.Constant) and isinstance(a.value, str)
        ),
        None,
    )
    for a in node.args:
        keys = _dict_keys(a)
        if keys is not None:
            return keys
    for kw in node.keywords:
        if kw.arg in _ARGUMENT_KWARGS:
            return _argument_value_keys(tree, node, kw.value)
    # ``call_tool("get_weather", params)``: the positional value after the tool name
    after = node.args[literal_pos + 1 :] if literal_pos is not None else []
    if after:
        return _argument_value_keys(tree, node, after[0])
    # ``tool(city=..., units=...)``: the keyword names are the arguments
    others = [
        kw.arg
        for kw in node.keywords
        if kw.arg and kw not in literal_kws and kw.arg not in _TOOL_NAME_KWARGS
    ]
    return others or None


def extract_call_keys_python(
    text: str, tool_name: str, near_line: int | None = None
) -> list[str] | None:
    """Argument keys of the call that passes ``tool_name`` as a string literal (closest to
    ``near_line``): the keys of a dict literal argument or of the dict a variable passed
    as the arguments holds (``arguments=params`` with ``params = {...}`` earlier in the
    function), else the call's keyword names (``tool(city=..., units=...)``). A call
    whose arguments cannot be read statically (``arguments=build_args(city)``) gives None
    rather than a misread or another call's keys."""
    return _find_call_keys_python(text, tool_name, near_line)[1]


def _find_call_keys_python(
    text: str, tool_name: str, near_line: int | None = None
) -> tuple[bool, list[str] | None]:
    """``(found, keys)``: whether a call passing ``tool_name`` exists in ``text`` at all,
    and its keys as ``extract_call_keys_python`` reads them."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False, None
    best: tuple[int, int, list[str] | None] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        literal_args = [
            a for a in node.args if isinstance(a, ast.Constant) and a.value == tool_name
        ]
        literal_kws = [
            k
            for k in node.keywords
            if isinstance(k.value, ast.Constant) and k.value.value == tool_name
        ]
        if not literal_args and not literal_kws:
            continue
        keys = _call_argument_keys(tree, node, literal_kws)
        if keys is None and near_line is None:
            continue  # no call site to pin: the first readable call stands for the file
        dist = abs((node.lineno or 0) - (near_line or node.lineno or 0))
        rank = (dist, 0 if keys is not None else 1)  # on a tie prefer a readable call
        if best is None or rank < best[:2]:
            best = (*rank, keys)
    return (True, best[2]) if best else (False, None)


_TS_KEY_RE = re.compile(r"""(?:^|[{,])\s*(?:["']([A-Za-z_$][\w$-]*)["']|([A-Za-z_$][\w$]*))\s*:""")


def extract_call_keys_ts(
    text: str, tool_name: str, near_line: int | None = None
) -> list[str] | None:
    best: tuple[int, list[str]] | None = None
    for m in re.finditer(r"""['"]""" + re.escape(tool_name) + r"""['"]""", text):
        brace = text.find("{", m.end())
        if brace < 0:
            continue
        depth = 0
        end = -1
        for i in range(brace, min(len(text), brace + 4000)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            continue
        body = text[brace + 1 : end]
        if "..." in body:
            continue
        keys = [a or b for a, b in _TS_KEY_RE.findall(body)]
        if not keys:
            continue
        line = text.count("\n", 0, m.start()) + 1
        dist = abs(line - (near_line or line))
        if best is None or dist < best[0]:
            best = (dist, keys)
    return best[1] if best else None


def _placeholder(prop: Any) -> Any:
    if not isinstance(prop, dict):
        return "x"
    for key in ("const", "default"):
        if key in prop:
            return prop[key]
    examples = prop.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]
    enum = prop.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    for combo in ("anyOf", "oneOf", "allOf"):
        opts = prop.get(combo)
        if isinstance(opts, list) and opts:
            return _placeholder(opts[0])
    typ = prop.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), typ[0] if typ else None)
    if typ == "string":
        fmt = prop.get("format")
        if fmt in ("date-time",):
            return "2026-01-01T00:00:00Z"
        if fmt == "date":
            return "2026-01-01"
        if fmt in ("uri", "url"):
            return "https://example.com"
        if fmt == "email":
            return "a@example.com"
        min_len = prop.get("minLength")
        return "x" * max(1, int(min_len)) if isinstance(min_len, int) else "x"
    if typ == "integer":
        return int(prop.get("minimum", 1)) if isinstance(prop.get("minimum"), int | float) else 1
    if typ == "number":
        return (
            float(prop.get("minimum", 1.0)) if isinstance(prop.get("minimum"), int | float) else 1.0
        )
    if typ == "boolean":
        return True
    if typ == "array":
        items = prop.get("items")
        min_items = prop.get("minItems")
        if isinstance(min_items, int) and min_items > 0:
            return [_placeholder(items)] * min_items
        return []
    if typ == "object":
        props = prop.get("properties") if isinstance(prop.get("properties"), dict) else {}
        required = prop.get("required") if isinstance(prop.get("required"), list) else []
        return {k: _placeholder(props.get(k)) for k in required}
    if typ == "null":
        return None
    return "x"


def synthesize_call(keys: list[str], schema: dict[str, Any]) -> dict[str, Any]:
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    return {k: _placeholder(props.get(k)) for k in keys}


def check_against_schema(call: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    import jsonschema

    try:
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        validator = cls(schema)
    except jsonschema.SchemaError as e:
        return [f"new inputSchema is not a valid JSON Schema: {e.message}"]
    errors = []
    for err in sorted(validator.iter_errors(call), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{where}: {err.message}")
    return errors[:8]


def mcp_schema_check(sandbox: Sandbox, ctx: FixContext) -> tuple[bool, str]:
    """Synthetic-call validation of each patched call site against the new inputSchema."""
    schema = ctx.new_input_schema
    if not isinstance(schema, dict) or not schema:
        return True, "no new inputSchema available; skipped"
    tool = _tool_name_for(ctx)
    if not tool:
        return True, "tool name unknown; skipped"
    sites = [
        s for s in ctx.call_sites if (s.operation or "").removeprefix("tool:") == tool
    ] or list(ctx.call_sites)
    if not sites:
        return True, "no call sites; skipped"
    problems: list[str] = []
    checked = 0
    for site in sites:
        keys: list[str] | None = None
        try:
            text = sandbox.read_file(site.file_path)
        except SandboxError:
            text = None
        if text is not None:
            if site.file_path.endswith(PY_EXTS):
                found, keys = _find_call_keys_python(text, tool, site.line)
                if found and keys is None:
                    # the patched call builds its arguments dynamically: the indexer's
                    # pre-patch args_shape would describe the old call, not this one
                    continue
            elif site.file_path.endswith(TS_EXTS):
                keys = extract_call_keys_ts(text, tool, site.line)
        if keys is None:
            shape = site.args_shape if isinstance(site.args_shape, dict) else {}
            raw = shape.get("keys")
            keys = [str(k) for k in raw] if isinstance(raw, list) else None
        if keys is None:
            continue
        checked += 1
        call = synthesize_call(keys, schema)
        errors = check_against_schema(call, schema)
        if errors:
            problems.append(
                f"{site.file_path}:{site.line} call {json.dumps(call, sort_keys=True, default=str)} "
                f"does not satisfy the new inputSchema: {'; '.join(errors)}"
            )
    if not checked:
        return True, "argument shape could not be extracted; skipped"
    if problems:
        return False, " | ".join(problems)
    return True, f"{checked} call site(s) validated against the new inputSchema"


# --------------------------------------------------------------------------------------
# validate()
# --------------------------------------------------------------------------------------


def _file_notes(patches: list[FilePatch], ctx: FixContext) -> list[str]:
    notes: list[str] = []
    for p in patches:
        first = p.hunks[0].new_start if p.hunks else 0
        label = f"{p.path}:{first}" if first else p.path
        sites = [
            s for s in ctx.call_sites if s.file_path == p.path and p.touches_line(s.line, slack=3)
        ]
        if sites:
            syms = sorted({s.symbol or s.operation for s in sites})
            what = f"updated call site{'s' if len(sites) > 1 else ''} {', '.join(f'`{s}`' for s in syms)}"
        elif p.is_new:
            what = "new file"
        elif p.is_deleted:
            what = "deleted"
        else:
            what = "helper / expectation updated"
        notes.append(f"- `{label}` — {what} (+{p.added_lines}/-{p.removed_lines})")
    return notes


def validate(
    sandbox: Sandbox,
    plan: TestPlan,
    context: FixContext,
    baseline: BaselineResult,
    *,
    settings: Settings | None = None,
) -> Verdict:
    settings = settings or get_settings()
    verdict = Verdict(ok=False, test_command=plan.command, baseline_result=baseline.summary())
    try:
        diff_text = sandbox.diff()
    except SandboxError as e:
        verdict.reasons.append(f"could not read the diff: {e}")
        return verdict
    if not diff_text.strip():
        verdict.reasons.append("the agent made no changes")
        return verdict

    patches = parse_unified_diff(diff_text)
    touched = sorted({p.path for p in patches} | {p.old_path for p in patches if p.old_path})
    verdict.changed_files = touched
    verdict.files_changed = len(touched)
    verdict.lines_changed = sum(p.changed_lines for p in patches)
    verdict.file_notes = _file_notes(patches, context)
    verdict.call_sites_changed = sum(
        1
        for s in context.call_sites
        if any(p.path == s.file_path and p.touches_line(s.line, slack=3) for p in patches)
    )

    # 1. applies cleanly
    ok, msg = sandbox.patch_applies_cleanly(diff_text)
    verdict.applied = ok
    if not ok:
        verdict.reasons.append(f"patch does not apply cleanly: {msg}")

    # 2. radius rule
    radius = (
        context.import_radius if context.import_radius is not None else settings.fix_import_radius
    )
    seeds = context.call_site_files
    if seeds:
        graph = build_import_graph(sandbox)
        allowed = files_within_radius(graph, seeds, radius)
        outside = [f for f in touched if f not in allowed]
        if outside:
            verdict.reasons.append(
                f"touches files outside the affected call sites and their imports (radius {radius}): "
                + ", ".join(outside)
            )
    else:
        verdict.reasons.append("no affected call sites are recorded, so no file may be changed")

    # 3. size
    if verdict.lines_changed > settings.fix_max_changed_lines:
        verdict.reasons.append(
            f"patch too large: {verdict.lines_changed} changed lines > {settings.fix_max_changed_lines}"
        )
    binary = [p.path for p in patches if p.is_binary]
    if binary:
        verdict.reasons.append("binary files changed: " + ", ".join(binary))

    # 4. refusal rule
    secret_files = [f for f in touched if is_secret_path(f)]
    if secret_files:
        verdict.reasons.append(
            "touches authentication or secrets handling (refused): " + ", ".join(secret_files)
        )
    for p in patches:
        for h in p.hunks:
            for ln in h.added:
                hits = secret_line_hits(ln)
                if hits:
                    verdict.reasons.append(
                        f"adds credential-like content in {p.path} (refused): {ln.strip()[:80]!r}"
                    )
                    break
            else:
                continue
            break

    if verdict.reasons:
        return verdict  # structural failures: no point running the suite

    # 5. tests
    res = run_tests(sandbox, plan, timeout_s=_test_timeout_s(settings))
    if res is not None:
        verdict.test_exit = res.exit_code
        verdict.test_output_tail = res.tail(40)
        verdict.tests_passed = parse_tests_passed(res.output)
        failing = parse_failing_tests(res.output, plan.runner)
        verdict.failing_tests = failing
        fewer_passed = (
            verdict.tests_passed is not None
            and baseline.tests_passed is not None
            and verdict.tests_passed < baseline.tests_passed
        )
        if res.timed_out:
            verdict.reasons.append(f"test command timed out after {res.duration_s:.0f}s")
        elif res.exit_code != 0:
            base_failing = set(baseline.failing_tests)
            not_run = suite_not_run_reason(
                res.exit_code, res.output, plan.runner, failing, verdict.tests_passed
            )
            if not_run or baseline.not_run:
                # "same failures as the baseline" means nothing when the suite never ran
                # (missing dependency, broken collection): the patch is unverified
                where = [f"after the patch: {not_run}"] if not_run else []
                if baseline.not_run:
                    where.append(f"on the baseline: {baseline.not_run}")
                verdict.reasons.append(
                    f"tests did not run (exit {res.exit_code}; {'; '.join(where)}), so the "
                    "patch could not be verified"
                )
            elif baseline.exit_code in (0, None):
                verdict.reasons.append(
                    f"tests fail after the patch (exit {res.exit_code}) but passed on the baseline: "
                    + (", ".join(failing[:10]) or res.tail(5))
                )
            elif failing and base_failing:
                new = sorted(set(failing) - base_failing)
                if new:
                    verdict.reasons.append(
                        "the patch introduces new test failures: " + ", ".join(new[:10])
                    )
                elif fewer_passed:
                    verdict.reasons.append(
                        f"fewer tests pass after the patch ({verdict.tests_passed} vs "
                        f"{baseline.tests_passed} on the baseline)"
                    )
                else:
                    verdict.preexisting_failures = sorted(base_failing)
            elif res.exit_code == baseline.exit_code:
                if fewer_passed:
                    verdict.reasons.append(
                        f"fewer tests pass after the patch ({verdict.tests_passed} vs "
                        f"{baseline.tests_passed} on the baseline; failures could not be matched)"
                    )
                else:
                    verdict.preexisting_failures = sorted(base_failing)
            else:
                verdict.reasons.append(
                    f"tests exit {res.exit_code} after the patch vs {baseline.exit_code} on the baseline "
                    "(failures could not be matched)"
                )
    elif plan.static_checks:
        verdict.baseline_result = baseline.summary()
    else:
        verdict.reasons.append(
            "no test command and no static checks: nothing could verify the patch"
        )

    # 6. static checks
    static = run_static_checks(sandbox, plan)
    parts: list[str] = []
    for cmd, r in static.items():
        base = baseline.static.get(cmd) or {}
        if r.ok:
            parts.append(f"`{cmd}` → pass")
            continue
        if r.timed_out:
            verdict.reasons.append(f"static check `{cmd}` timed out")
            parts.append(f"`{cmd}` → timed out")
            continue
        base_exit = base.get("exit_code")
        base_viol = int(base.get("violations") or 0)
        if base_exit not in (None, 0) and r.violations <= base_viol:
            parts.append(f"`{cmd}` → {r.violations} pre-existing finding(s), none added")
            continue
        verdict.reasons.append(
            f"static check `{cmd}` fails (exit {r.exit_code}, {r.violations} finding(s)): "
            + tail_text(r.output_tail, lines=8, max_chars=600)
        )
        parts.append(f"`{cmd}` → fail")
    verdict.static_result = "; ".join(parts) if parts else "none configured"

    # 7. MCP schema
    if context.source_kind in ("mcp", "observed") or context.new_input_schema:
        ok, msg = mcp_schema_check(sandbox, context)
        verdict.mcp_check = msg
        if not ok:
            verdict.reasons.append(f"MCP inputSchema check failed: {msg}")

    verdict.ok = not verdict.reasons
    log.info(
        "validate %s: ok=%s files=%s lines=%s reasons=%s",
        context.repo_full_name,
        verdict.ok,
        verdict.files_changed,
        verdict.lines_changed,
        verdict.reasons,
    )
    return verdict


__all__ = [
    "BaselineResult",
    "StaticCheckResult",
    "Verdict",
    "baseline_cache_path",
    "build_import_graph",
    "check_against_schema",
    "count_violations",
    "extract_call_keys_python",
    "extract_call_keys_ts",
    "files_within_radius",
    "mcp_schema_check",
    "parse_failing_tests",
    "parse_tests_passed",
    "run_baseline",
    "run_static_checks",
    "run_tests",
    "synthesize_call",
    "suite_not_run_reason",
    "validate",
]
