"""Detect how to install, test and statically check a customer repository.

``detect_test_command(workdir, repo_config)`` returns a ``TestPlan``. The test command is
chosen in this order (first hit wins):

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
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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


def _read_text(path: Path, max_bytes: int = 512_000) -> str | None:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > max_bytes:
            return None
        return path.read_text("utf-8", errors="replace")
    except OSError:
        return None


def _read_pyproject(root: Path) -> dict[str, Any]:
    text = _read_text(root / "pyproject.toml")
    if not text:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        log.info("pyproject.toml unparsable: %s", e)
        return {}


def _read_package_json(root: Path) -> dict[str, Any]:
    text = _read_text(root / "package.json")
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        log.info("package.json unparsable: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _has_test_files(root: Path, max_entries: int = 20_000) -> bool:
    if (root / "tests").is_dir() or (root / "test").is_dir():
        return True
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for fn in filenames:
            seen += 1
            if seen > max_entries:
                return False
            if fn.endswith(".py") and (fn.startswith("test_") or fn.endswith("_test.py")):
                return True
    return False


def _make_has_test_target(root: Path) -> bool:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        text = _read_text(root / name)
        if text and any(_MAKE_TARGET_RE.match(line) for line in text.splitlines()):
            return True
    return False


def _looks_python(root: Path, pyproject: dict[str, Any]) -> bool:
    if pyproject or (root / "setup.py").exists() or (root / "setup.cfg").exists():
        return True
    if any((root / n).exists() for n in ("requirements.txt", "uv.lock", "poetry.lock", "Pipfile")):
        return True
    try:
        for entry in root.iterdir():
            if entry.suffix == ".py":
                return True
            if (
                entry.is_dir()
                and entry.name not in _IGNORED_DIRS
                and (entry / "__init__.py").exists()
            ):
                return True
    except OSError:
        pass
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


def _python_runner_prefix(root: Path) -> str:
    """``uv run`` / ``poetry run`` so the auto-detected pytest sees the lockfile env."""
    if (root / "uv.lock").exists():
        return "uv run --frozen "
    if (root / "poetry.lock").exists():
        return "poetry run "
    return ""


def _ruff_configured(root: Path, pyproject: dict[str, Any]) -> bool:
    if (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        return True
    tool = pyproject.get("tool")
    return isinstance(tool, dict) and "ruff" in tool


def detect_test_command(
    workdir: str | Path,
    repo_config: dict[str, Any] | None = None,
    *,
    tool_available: Callable[[str], bool] | None = None,
) -> TestPlan:
    """See the module docstring. ``tool_available(name)`` says whether a binary exists
    in the sandbox (defaults to ``shutil.which`` on this machine)."""
    root = Path(workdir)
    repo_config = repo_config or {}
    available = tool_available or (lambda name: shutil.which(name) is not None)
    pyproject = _read_pyproject(root)
    pkg = _read_package_json(root)
    kintsua_tool = (
        (pyproject.get("tool") or {}).get("kintsua")
        if isinstance(pyproject.get("tool"), dict)
        else None
    )
    kintsua_tool = kintsua_tool if isinstance(kintsua_tool, dict) else {}

    is_python = _looks_python(root, pyproject)
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
        elif _make_has_test_target(root):
            command, source, runner = "make test", "Makefile", "make"
        elif is_python and _has_test_files(root):
            command = _python_runner_prefix(root) + PYTEST_COMMAND
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
            if (root / "uv.lock").exists():
                install.append("uv sync --frozen")
            elif (root / "poetry.lock").exists():
                install.append("poetry install --no-interaction")
            elif (root / "requirements.txt").exists():
                install.append("python -m pip install -r requirements.txt")
                for extra in (
                    "requirements-dev.txt",
                    "requirements-test.txt",
                    "requirements_dev.txt",
                ):
                    if (root / extra).exists():
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
            if (root / "pnpm-lock.yaml").exists():
                install.append("pnpm install --frozen-lockfile")
            elif (root / "yarn.lock").exists():
                install.append("yarn install --frozen-lockfile")
            elif (root / "package-lock.json").exists():
                install.append("npm ci")
            else:
                install.append("npm install")

    # ---- static checks -----------------------------------------------------------
    static: list[str] = _as_str_list(kintsua_tool.get("static_checks"))
    if not static:
        if is_python and (_ruff_configured(root, pyproject) or available("ruff")):
            static.append(RUFF_COMMAND)
        if (root / "tsconfig.json").exists():
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
        root,
        plan.command,
        plan.source,
        plan.install,
        plan.static_checks,
    )
    return plan


__all__ = ["PYTEST_COMMAND", "RUFF_COMMAND", "TSC_COMMAND", "TestPlan", "detect_test_command"]
