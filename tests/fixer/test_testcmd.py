from __future__ import annotations

import json
from pathlib import Path

from kintsua.fixer.testcmd import PYTEST_COMMAND, RUFF_COMMAND, TSC_COMMAND, detect_test_command

NO_TOOLS = {"tool_available": lambda name: False}
WITH_RUFF = {"tool_available": lambda name: name == "ruff"}


def _py(
    root: Path, *, tests: bool = True, pyproject: str = "[project]\nname='x'\nversion='0'\n"
) -> None:
    (root / "pyproject.toml").write_text(pyproject)
    (root / "app.py").write_text("x = 1\n")
    if tests:
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n")


def test_kintsua_yml_override_wins(tmp_path: Path):
    _py(tmp_path)
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    plan = detect_test_command(tmp_path, {"test_command": "make verify"}, **NO_TOOLS)
    assert (
        plan.command == "make verify" and plan.source == ".kintsua.yml" and plan.runner == "custom"
    )


def test_pyproject_tool_kintsua(tmp_path: Path):
    _py(
        tmp_path,
        pyproject="[project]\nname='x'\nversion='0'\n[tool.kintsua]\ntest = 'pytest -x tests/unit'\nstatic_checks = ['mypy .']\n",
    )
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.command == "pytest -x tests/unit"
    assert plan.source == "pyproject [tool.kintsua]" and plan.runner == "pytest"
    assert plan.static_checks == ["mypy ."]


def test_package_json_scripts_test(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "^2"}})
    )
    (tmp_path / "package-lock.json").write_text("{}")
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.command == "npm test" and plan.runner == "vitest" and plan.language == "node"
    assert plan.install == ["npm ci"]


def test_npm_placeholder_is_ignored_and_devdeps_fall_back(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"test": 'echo "Error: no test specified" && exit 1'},
                "devDependencies": {"jest": "^29"},
            }
        )
    )
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.command == "npx jest" and plan.runner == "jest"
    assert plan.install == ["npm install"]
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^2"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.command == "npx vitest run" and plan.install == ["pnpm install --frozen-lockfile"]


def test_makefile_test_target(tmp_path: Path):
    _py(tmp_path, tests=False)
    (tmp_path / "Makefile").write_text("lint:\n\truff check .\n\ntest:\n\tpytest\n")
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.command == "make test" and plan.runner == "make"
    (tmp_path / "Makefile").write_text("test_var := 1\nlint:\n\truff check .\n")
    assert detect_test_command(tmp_path, {}, **NO_TOOLS).command is None


def test_pytest_from_tests_dir_and_install_from_lockfiles(tmp_path: Path):
    _py(tmp_path)
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.command == PYTEST_COMMAND and plan.source == "pytest" and plan.language == "python"
    assert plan.install == []  # no deps, no lockfile: nothing to install
    (tmp_path / "requirements.txt").write_text("stripe\n")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.install == [
        "python -m pip install -r requirements.txt",
        "python -m pip install -r requirements-dev.txt",
    ]
    (tmp_path / "uv.lock").write_text("")
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.install == ["uv sync --frozen"]
    assert plan.command == "uv run --frozen " + PYTEST_COMMAND
    (tmp_path / "uv.lock").unlink()
    (tmp_path / "poetry.lock").write_text("")
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.install == ["poetry install --no-interaction"]
    assert plan.command == "poetry run " + PYTEST_COMMAND


def test_pyproject_dependencies_install_editable(tmp_path: Path):
    _py(
        tmp_path,
        pyproject="[project]\nname='x'\nversion='0'\ndependencies=['stripe']\n[project.optional-dependencies]\ndev=['pytest']\n",
    )
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.install == ["python -m pip install -e .[dev]"]


def test_pytest_from_test_files_without_tests_dir(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "test_pkg.py").write_text("def test_x():\n    pass\n")
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.command == PYTEST_COMMAND


def test_static_checks(tmp_path: Path):
    _py(
        tmp_path,
        tests=False,
        pyproject="[project]\nname='x'\nversion='0'\n[tool.ruff]\nline-length=100\n",
    )
    (tmp_path / "tsconfig.json").write_text("{}")
    (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.static_checks == [RUFF_COMMAND, TSC_COMMAND]
    assert plan.language == "mixed"
    assert plan.has_checks


def test_ruff_from_availability_when_unconfigured(tmp_path: Path):
    _py(tmp_path, tests=False)
    assert detect_test_command(tmp_path, {}, **NO_TOOLS).static_checks == []
    assert detect_test_command(tmp_path, {}, **WITH_RUFF).static_checks == [RUFF_COMMAND]


def test_nothing_detected(tmp_path: Path):
    (tmp_path / "README.md").write_text("hi\n")
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.command is None and plan.static_checks == [] and plan.install == []
    assert not plan.has_checks and plan.language == "unknown"
    assert plan.as_dict()["source"] == "none"


def test_unparsable_manifests_do_not_crash(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("this is [not toml")
    (tmp_path / "package.json").write_text("{not json")
    plan = detect_test_command(tmp_path, {}, **NO_TOOLS)
    assert plan.command is None
