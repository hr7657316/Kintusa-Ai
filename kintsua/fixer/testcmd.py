"""Detect how to install, test and statically check a customer repository.

``detect_test_command(workdir, repo_config)`` returns a ``TestPlan``. ``workdir`` is either
a host path or a prepared ``Sandbox``: with a sandbox every manifest is read and every
file listed **through the sandbox** (``read_file`` / ``list_files``) and tool availability
is probed inside it, so the Docker backend (whose clone lives in a container at
``/work/repo``, not on the worker's filesystem) gets the same plan as a local clone. The
test command is chosen in this order (first hit wins):

1. ``.kintsua.yml`` ``test_command`` (``repo_config["test_command"]``);
2. ``pyproject.toml`` ``[tool.kintsua] test``;
3. ``package.json`` ``scripts.test`` (ignoring npm's placeholder ``no test specified``);
4. a ``test`` target in a ``Makefile``;
5. ``python -m pytest -q`` when a ``tests/`` directory or ``test_*.py`` files exist;
6. ``npx vitest run`` / ``npx jest`` when either is a (dev)dependency;
7. none.

Install steps come from lockfiles when present (``uv.lock`` -> ``uv sync --frozen``,
``poetry.lock`` -> ``poetry install``, ``requirements*.txt`` -> ``pip install -r``,
``package-lock.json`` -> ``npm ci``, ``pnpm-lock.yaml`` / ``yarn.lock`` -> their frozen
installs), falling back to ``pip install -e .`` for a ``pyproject.toml`` with
dependencies and ``npm install`` for a bare ``package.json``.

Static checks: ``ruff check .`` when a ruff configuration exists (or ruff is available
and the repo is Python), ``npx tsc --noEmit`` when ``tsconfig.json`` exists.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kintsua.fixer.sandbox import Sandbox, SandboxError

log = logging.getLogger(__name__)

NPM_PLACEHOLDER_RE = re.compile(r"no test specified", re.IGNORECASE)
_IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build"}
_MAKE_TARGET_RE = re.compile(r"^test\s*:(?!=)")
PYTEST_COMMAND = "python -m pytest -q"
RUFF_COMMAND = "ruff check ."
TSC_COMMAND = "npx tsc --noEmit"


@dataclass
class TestPlan:
    __test__ = False  # not a pytest test class

    command: str | None
    install: list[str] = field(default_factory=list)
    static_checks: list[str] = field(default_factory=list)
    language: str = "unknown"  # python | node | mixed | unknown
    source: str = "none"  # where ``command`` came from
    runner: str | None = None  # pytest | jest | vitest | npm | make | custom

    @property
    def has_checks(self) -> bool:
        return bool(self.command) or bool(self.static_checks)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


MAX_MANIFEST_BYTES = 512_000
MAX_SCANNED_FILES = 20_000


class _PathTree:
    """The repository as a directory on this machine (``LocalSandbox.workdir``, tests)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.label = str(self.root)

    def read_text(self, rel: str) -> str | None:
        path = self.root / rel
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_MANIFEST_BYTES:
                return None
            return path.read_text("utf-8", errors="replace")
        except OSError:
            return None

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def is_dir(self, rel: str) -> bool:
        return (self.root / rel).is_dir()

    def top_level(self) -> list[tuple[str, bool]]:
        """``(name, is_dir)`` for the entries of the repository root."""
        try:
            return [(e.name, e.is_dir()) for e in self.root.iterdir()]
        except OSError:
            return []

    def files(self) -> list[str]:
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
            rel_dir = os.path.relpath(dirpath, self.root)
            for fn in filenames:
                out.append(fn if rel_dir == "." else f"{rel_dir}/{fn}".replace(os.sep, "/"))
                if len(out) >= MAX_SCANNED_FILES:
                    return out
        return out


class _SandboxTree:
    """The repository inside a prepared sandbox: one ``list_files`` (cached) plus
    ``read_file`` per manifest, so nothing touches the worker's filesystem."""

    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox
        self.label = f"{sandbox.backend}:{sandbox.workdir}"
        self._files: list[str] | None = None
        self._set: set[str] = set()
        self._dirs: set[str] = set()

    def files(self) -> list[str]:
        if self._files is None:
            listed = self.sandbox.list_files("**/*")
            self._files = [
                f for f in listed if not any(part in _IGNORED_DIRS for part in f.split("/")[:-1])
            ]
            self._set = set(self._files)
            for f in self._files:
                parts = f.split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    self._dirs.add("/".join(parts[:i]))
        return self._files

    def read_text(self, rel: str) -> str | None:
        self.files()
        if rel not in self._set:
            return None
        try:
            text = self.sandbox.read_file(rel)
        except SandboxError:
            return None
        return None if len(text) > MAX_MANIFEST_BYTES else text

    def exists(self, rel: str) -> bool:
        self.files()
        return rel in self._set or rel in self._dirs

    def is_dir(self, rel: str) -> bool:
        self.files()
        return rel in self._dirs

    def top_level(self) -> list[tuple[str, bool]]:
        self.files()
        names = {f.split("/", 1)[0]: "/" in f for f in self._files or []}
        return list(names.items())

    def tool_available(self, name: str) -> bool:
        """Whether ``name`` is on the sandbox's PATH (the image, not the worker)."""
        try:
            res = self.sandbox.run(["sh", "-c", f"command -v {shlex.quote(name)}"], timeout_s=30)
        except SandboxError:
            return False
        return res.ok


_Tree = _PathTree | _SandboxTree


def _read_pyproject(tree: _Tree) -> dict[str, Any]:
    text = tree.read_text("pyproject.toml")
    if not text:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        log.info("pyproject.toml unparsable: %s", e)
        return {}


def _read_package_json(tree: _Tree) -> dict[str, Any]:
    text = tree.read_text("package.json")
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        log.info("package.json unparsable: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _has_test_files(tree: _Tree) -> bool:
    if tree.is_dir("tests") or tree.is_dir("test"):
        return True
    for rel in tree.files():
        fn = rel.rsplit("/", 1)[-1]
        if fn.endswith(".py") and (fn.startswith("test_") or fn.endswith("_test.py")):
            return True
    return False


def _make_has_test_target(tree: _Tree) -> bool:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        text = tree.read_text(name)
        if text and any(_MAKE_TARGET_RE.match(line) for line in text.splitlines()):
            return True
    return False


def _looks_python(tree: _Tree, pyproject: dict[str, Any]) -> bool:
    if pyproject or tree.exists("setup.py") or tree.exists("setup.cfg"):
        return True
    if any(tree.exists(n) for n in ("requirements.txt", "uv.lock", "poetry.lock", "Pipfile")):
        return True
    for name, is_dir in tree.top_level():
        if not is_dir and name.endswith(".py"):
            return True
        if is_dir and name not in _IGNORED_DIRS and tree.exists(f"{name}/__init__.py"):
            return True
    return False


def _deps(pkg: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        val = pkg.get(key)
        if isinstance(val, dict):
            out.update(str(k) for k in val)
    return out


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [
            str(v).strip() for v in value if isinstance(v, str | int | float) and str(v).strip()
        ]
    return []


def _python_runner_prefix(tree: _Tree) -> str:
    """``uv run`` / ``poetry run`` so the auto-detected pytest sees the lockfile env."""
    if tree.exists("uv.lock"):
        return "uv run --frozen "
    if tree.exists("poetry.lock"):
        return "poetry run "
    return ""


def _ruff_configured(tree: _Tree, pyproject: dict[str, Any]) -> bool:
    if tree.exists("ruff.toml") or tree.exists(".ruff.toml"):
        return True
    tool = pyproject.get("tool")
    return isinstance(tool, dict) and "ruff" in tool


def detect_test_command(
    workdir: str | Path | Sandbox,
    repo_config: dict[str, Any] | None = None,
    *,
    tool_available: Callable[[str], bool] | None = None,
) -> TestPlan:
    """See the module docstring. ``workdir`` is a host path or a prepared ``Sandbox``
    (the fixer passes the sandbox, so detection never reads the worker's filesystem at a
    container path). ``tool_available(name)`` says whether a binary exists in the
    sandbox (defaults to ``command -v`` inside the sandbox, or ``shutil.which`` on this
    machine for a host path)."""
    tree: _Tree = _SandboxTree(workdir) if isinstance(workdir, Sandbox) else _PathTree(workdir)
    repo_config = repo_config or {}
    if tool_available is not None:
        available = tool_available
    elif isinstance(tree, _SandboxTree):
        available = tree.tool_available
    else:
        available = lambda name: shutil.which(name) is not None  # noqa: E731
    pyproject = _read_pyproject(tree)
    pkg = _read_package_json(tree)
    kintsua_tool = (
        (pyproject.get("tool") or {}).get("kintsua")
        if isinstance(pyproject.get("tool"), dict)
        else None
    )
    kintsua_tool = kintsua_tool if isinstance(kintsua_tool, dict) else {}

    is_python = _looks_python(tree, pyproject)
    is_node = bool(pkg)
    language = (
        "mixed"
        if is_python and is_node
        else "python"
        if is_python
        else "node"
        if is_node
        else "unknown"
    )

    # ---- test command ------------------------------------------------------------
    command: str | None = None
    source = "none"
    runner: str | None = None
    override = repo_config.get("test_command")
    if isinstance(override, str) and override.strip():
        command, source = override.strip(), ".kintsua.yml"
    elif isinstance(kintsua_tool.get("test"), str) and kintsua_tool["test"].strip():
        command, source = kintsua_tool["test"].strip(), "pyproject [tool.kintsua]"
    else:
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        script = scripts.get("test") if scripts else None
        if isinstance(script, str) and script.strip() and not NPM_PLACEHOLDER_RE.search(script):
            command, source = "npm test", "package.json scripts.test"
            runner = "vitest" if "vitest" in script else "jest" if "jest" in script else "npm"
        elif _make_has_test_target(tree):
            command, source, runner = "make test", "Makefile", "make"
        elif is_python and _has_test_files(tree):
            command = _python_runner_prefix(tree) + PYTEST_COMMAND
            source, runner = "pytest", "pytest"
        elif is_node:
            deps = _deps(pkg)
            if "vitest" in deps:
                command, source, runner = "npx vitest run", "package.json devDependencies", "vitest"
            elif "jest" in deps:
                command, source, runner = "npx jest", "package.json devDependencies", "jest"
    if runner is None and command:
        low = command.lower()
        runner = (
            "pytest"
            if "pytest" in low
            else "vitest"
            if "vitest" in low
            else "jest"
            if "jest" in low
            else "custom"
        )

    # ---- install steps -----------------------------------------------------------
    install: list[str] = _as_str_list(kintsua_tool.get("install"))
    if not install:
        if is_python:
            if tree.exists("uv.lock"):
                install.append("uv sync --frozen")
            elif tree.exists("poetry.lock"):
                install.append("poetry install --no-interaction")
            elif tree.exists("requirements.txt"):
                install.append("python -m pip install -r requirements.txt")
                for extra in (
                    "requirements-dev.txt",
                    "requirements-test.txt",
                    "requirements_dev.txt",
                ):
                    if tree.exists(extra):
                        install.append(f"python -m pip install -r {extra}")
            elif pyproject:
                project = (
                    pyproject.get("project") if isinstance(pyproject.get("project"), dict) else {}
                )
                deps = project.get("dependencies") if project else None
                optional = project.get("optional-dependencies") if project else None
                extras = [
                    g
                    for g in ("dev", "test", "tests")
                    if isinstance(optional, dict) and g in optional
                ]
                if deps or extras:
                    target = f".[{','.join(extras)}]" if extras else "."
                    install.append(f"python -m pip install -e {target}")
        if is_node:
            if tree.exists("pnpm-lock.yaml"):
                install.append("pnpm install --frozen-lockfile")
            elif tree.exists("yarn.lock"):
                install.append("yarn install --frozen-lockfile")
            elif tree.exists("package-lock.json"):
                install.append("npm ci")
            else:
                install.append("npm install")

    # ---- static checks -----------------------------------------------------------
    static: list[str] = _as_str_list(kintsua_tool.get("static_checks"))
    if not static:
        if is_python and (_ruff_configured(tree, pyproject) or available("ruff")):
            static.append(RUFF_COMMAND)
        if tree.exists("tsconfig.json"):
            static.append(TSC_COMMAND)

    plan = TestPlan(
        command=command,
        install=install,
        static_checks=static,
        language=language,
        source=source,
        runner=runner,
    )
    log.info(
        "test plan for %s: command=%r (%s) install=%s static=%s",
        tree.label,
        plan.command,
        plan.source,
        plan.install,
        plan.static_checks,
    )
    return plan


__all__ = ["PYTEST_COMMAND", "RUFF_COMMAND", "TSC_COMMAND", "TestPlan", "detect_test_command"]
