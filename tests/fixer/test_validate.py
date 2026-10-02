from __future__ import annotations

from pathlib import Path

import pytest

from kintsua.fixer.patch import changed_line_count
from kintsua.fixer.sandbox import LocalSandbox
from kintsua.fixer.testcmd import TestPlan, detect_test_command
from kintsua.fixer.validate import (
    BaselineResult,
    baseline_cache_path,
    build_import_graph,
    check_against_schema,
    count_violations,
    extract_call_keys_python,
    extract_call_keys_ts,
    files_within_radius,
    mcp_schema_check,
    parse_failing_tests,
    parse_tests_passed,
    run_baseline,
    suite_not_run_reason,
    synthesize_call,
    validate,
)
from tests.fixer.conftest import (
    AGENT_REPO,
    all_breaks,
    clone_url_for,
    context_from_break,
    git,
    load_break,
    make_git_repo,
)

# --------------------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------------------

PYTEST_OUT = """\
....F.E                                                                  [100%]
=================================== FAILURES ===================================
FAILED tests/test_billing.py::test_charge_passes_amount_in_minor_units - AssertionError: assert 1250 == 12
ERROR tests/test_weather.py::test_forecast - fixture 'client' not found
5 passed, 1 failed, 1 error in 0.21s
"""

JEST_OUT = """\
 FAIL  src/billing.test.ts
  ● charge › passes amount
    ✕ passes amount (12 ms)
    ✓ passes currency (3 ms)
Tests:       1 failed, 4 passed, 5 total
"""

VITEST_OUT = """\
 ❯ src/weather.test.ts (2)
   × calls the weather tool
   ✓ formats the summary
 FAIL  src/weather.test.ts > calls the weather tool
 Tests  1 failed | 3 passed (4)
"""


def test_parse_pytest_failures_and_passed():
    assert parse_failing_tests(PYTEST_OUT, "pytest") == [
        "tests/test_billing.py::test_charge_passes_amount_in_minor_units",
        "tests/test_weather.py::test_forecast",
    ]
    assert parse_tests_passed(PYTEST_OUT) == 5


def test_parse_jest_and_vitest():
    assert parse_failing_tests(JEST_OUT, "jest") == ["passes amount", "src/billing.test.ts"]
    assert parse_tests_passed(JEST_OUT) == 4
    ids = parse_failing_tests(VITEST_OUT, "vitest")
    assert "calls the weather tool" in ids and "src/weather.test.ts > calls the weather tool" in ids
    assert parse_tests_passed(VITEST_OUT) == 3
    assert parse_failing_tests("garbage output", "pytest") == []
    assert parse_tests_passed("garbage") is None


def test_count_violations():
    assert (
        count_violations(
            "ruff check .", "agent/x.py:3:1: F401 `os` imported but unused\nFound 1 error.\n"
        )
        == 1
    )
    assert (
        count_violations(
            "npx tsc --noEmit",
            "src/a.ts(3,5): error TS2322: Type 'x'.\nsrc/b.ts(1,1): error TS1005: ';' expected.\n",
        )
        == 2
    )
    assert count_violations("make lint", "whatever") == 0


# --------------------------------------------------------------------------------------
# import graph / radius
# --------------------------------------------------------------------------------------


def test_import_graph_and_radius(sandbox: LocalSandbox):
    graph = build_import_graph(sandbox)
    assert "agent/helpers.py" in graph["agent/billing.py"]
    assert "tests/test_billing.py" in graph["agent/billing.py"]
    assert "agent/unrelated.py" not in graph["agent/billing.py"]
    assert files_within_radius(graph, ["agent/billing.py"], 0) == {"agent/billing.py"}
    one_hop = files_within_radius(graph, ["agent/billing.py"], 1)
    assert {"agent/billing.py", "agent/helpers.py", "tests/test_billing.py"} <= one_hop
    assert "agent/unrelated.py" not in one_hop
    assert "agent/weather.py" not in one_hop


def test_ts_import_graph(sandbox: LocalSandbox):
    sandbox.write_file(
        "web/src/api.ts",
        "import { charge } from './billing';\nimport fs from 'fs';\nexport const x = charge;\n",
    )
    sandbox.write_file("web/src/billing.ts", "export const charge = () => 1;\n")
    sandbox.write_file("web/src/lib/index.ts", "export const y = 1;\n")
    sandbox.write_file(
        "web/src/use.js",
        "const lib = require('./lib');\nconst { charge } = require('./billing.js');\n",
    )
    graph = build_import_graph(sandbox)
    assert "web/src/billing.ts" in graph["web/src/api.ts"]
    assert "web/src/lib/index.ts" in graph["web/src/use.js"]
    assert "web/src/billing.ts" in graph["web/src/use.js"]


# --------------------------------------------------------------------------------------
# MCP synthetic call
# --------------------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "units": {"type": "string", "enum": ["metric", "imperial"], "default": "metric"},
        "days": {"type": "integer", "minimum": 1},
        "opts": {
            "type": "object",
            "properties": {"lang": {"type": "string"}},
            "required": ["lang"],
        },
    },
    "required": ["city", "units"],
    "additionalProperties": False,
}


def test_synthesize_and_validate_calls():
    call = synthesize_call(["city", "units", "days", "opts"], SCHEMA)
    assert call == {"city": "x", "units": "metric", "days": 1, "opts": {"lang": "x"}}
    assert check_against_schema(call, SCHEMA) == []
    missing = check_against_schema(synthesize_call(["city"], SCHEMA), SCHEMA)
    assert missing and "units" in missing[0]
    extra = check_against_schema(synthesize_call(["city", "units", "unit"], SCHEMA), SCHEMA)
    assert extra and "unit" in extra[0]


def test_extract_call_keys_python_and_ts():
    src = 'def f(c):\n    return c.call_tool("get_weather", {"city": city, "units": "metric"})\n'
    assert extract_call_keys_python(src, "get_weather", 2) == ["city", "units"]
    src2 = 'r = session.call_tool(name="get_weather", arguments=dict(city=c))\n'
    assert extract_call_keys_python(src2, "get_weather", 1) == ["city"]
    src3 = 'r = call("get_weather", city=c, units=u)\n'
    assert extract_call_keys_python(src3, "get_weather", 1) == ["city", "units"]
    assert extract_call_keys_python("x = 1\n", "get_weather", 1) is None
    assert extract_call_keys_python("def f(:\n", "get_weather", 1) is None
    ts = "const r = await client.callTool({ name: 'get_weather', arguments: { city, units: \"metric\" } });\n"
    keys = extract_call_keys_ts(ts, "get_weather", 1)
    assert keys is not None and "units" in keys
    ts2 = "await call('get_weather', { city: c, 'days': 3 })"
    assert extract_call_keys_ts(ts2, "get_weather") == ["city", "days"]


# --------------------------------------------------------------------------------------
# baseline
# --------------------------------------------------------------------------------------


def test_run_baseline_green_tree_and_cache(sandbox: LocalSandbox, settings, data_dir: Path):
    plan = detect_test_command(sandbox.workdir, {})
    assert plan.command and "ruff check ." in plan.static_checks
    base = run_baseline(sandbox, plan, repo_key="acme/agent", settings=settings)
    assert base.exit_code == 0 and base.failing_tests == [] and base.tests_passed == 3
    assert base.static["ruff check ."]["exit_code"] == 0 and not base.cached
    assert baseline_cache_path(settings, "acme/agent", sandbox.base_sha).exists()
    again = run_baseline(sandbox, plan, repo_key="acme/agent", settings=settings)
    assert again.cached and again.exit_code == 0 and again.tests_passed == 3
    # a different plan invalidates the cache
    other = TestPlan(command=plan.command, static_checks=[])
    assert not run_baseline(sandbox, other, repo_key="acme/agent", settings=settings).cached
    assert "exit 0" in base.summary()


# --------------------------------------------------------------------------------------
# recorded breaks -> verdicts
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("brk", all_breaks(), ids=lambda b: b["name"])
def test_recorded_breaks_reach_the_expected_verdict(brk, sandbox: LocalSandbox, settings, data_dir):
    plan = detect_test_command(sandbox.workdir, {})
    baseline = run_baseline(sandbox, plan, repo_key="acme/agent", settings=settings)
    assert baseline.exit_code == 0
    for p in brk["patch"]:
        ok, msg = sandbox.apply_patch(p)
        assert ok, msg
    ctx = context_from_break(brk, sandbox, plan)
    verdict = validate(sandbox, plan, ctx, baseline, settings=settings)
    expected = brk["expected_verdict"]
    assert verdict.ok is expected["ok"], verdict.reasons
    if expected.get("reason_contains"):
        assert any(expected["reason_contains"] in r for r in verdict.reasons), verdict.reasons
    if verdict.ok:
        assert verdict.test_exit == 0 and verdict.tests_passed == 3
        assert verdict.files_changed == 1 and verdict.call_sites_changed == 1
        assert verdict.lines_changed == 2
        assert "pass" in verdict.static_result
        assert verdict.file_notes and "call site" in verdict.file_notes[0]


def test_mcp_check_reads_the_patched_call(sandbox: LocalSandbox, settings, data_dir):
    brk = load_break("mcp_get_weather_units_required")
    plan = detect_test_command(sandbox.workdir, {})
    baseline = run_baseline(sandbox, plan, repo_key="acme/agent", settings=settings)
    for p in brk["patch"]:
        assert sandbox.apply_patch(p)[0]
    verdict = validate(
        sandbox, plan, context_from_break(brk, sandbox, plan), baseline, settings=settings
    )
    assert verdict.ok and verdict.mcp_check and "validated" in verdict.mcp_check


def test_size_rule(sandbox: LocalSandbox, settings, data_dir, monkeypatch):
    brk = load_break("stripe_charge_source_renamed")
    plan = detect_test_command(sandbox.workdir, {})
    baseline = run_baseline(sandbox, plan, repo_key="acme/agent", settings=settings)
    assert sandbox.apply_patch(brk["patch"][0])[0]
    monkeypatch.setattr(settings, "fix_max_changed_lines", 1)
    verdict = validate(
        sandbox, plan, context_from_break(brk, sandbox, plan), baseline, settings=settings
    )
    assert not verdict.ok and any("patch too large" in r for r in verdict.reasons)


def test_added_credential_line_is_refused(sandbox: LocalSandbox, settings, data_dir):
    brk = load_break("stripe_charge_source_renamed")
    plan = detect_test_command(sandbox.workdir, {})
    baseline = run_baseline(sandbox, plan, repo_key="acme/agent", settings=settings)
    assert sandbox.apply_patch(
        {
            "path": "agent/billing.py",
            "old": "        source=source,\n",
            "new": (
                '        payment_method=source,\n        api_key="sk_live_a' + 'bcdefghijk",\n'
            ),
        }
    )[0]
    verdict = validate(
        sandbox, plan, context_from_break(brk, sandbox, plan), baseline, settings=settings
    )
    assert not verdict.ok and any("credential-like" in r for r in verdict.reasons)


def test_no_changes_is_rejected(sandbox: LocalSandbox, settings, data_dir):
    brk = load_break("stripe_charge_source_renamed")
    plan = detect_test_command(sandbox.workdir, {})
    baseline = BaselineResult(sha=sandbox.base_sha, exit_code=0)
    verdict = validate(
        sandbox, plan, context_from_break(brk, sandbox, plan), baseline, settings=settings
    )
    assert not verdict.ok and "no changes" in verdict.reasons[0]


@pytest.fixture
def red_sandbox(settings, tmp_path: Path):
    """A clone of the fixture repo whose baseline already has one failing test."""
    src = tmp_path / "red-src"
    import shutil

    shutil.copytree(AGENT_REPO, src, ignore=shutil.ignore_patterns("__pycache__"))
    (src / "tests" / "test_flaky.py").write_text("def test_always_fails():\n    assert False\n")
    origin = tmp_path / "red-origin"
    make_git_repo(src, origin)
    sb = LocalSandbox(settings=settings, root=tmp_path / "red-sb", wall_clock_s=600)
    sb.prepare(clone_url_for(origin), "main")
    try:
        yield sb
    finally:
        sb.cleanup()


def test_baseline_equal_failures_are_accepted_and_new_ones_rejected(
    red_sandbox: LocalSandbox, settings, data_dir
):
    sb = red_sandbox
    plan = detect_test_command(sb.workdir, {})
    baseline = run_baseline(sb, plan, repo_key="acme/red", settings=settings)
    assert baseline.exit_code == 1 and baseline.failing_tests == [
        "tests/test_flaky.py::test_always_fails"
    ]
    assert "1 failing test" in baseline.summary()

    good = load_break("stripe_charge_source_renamed")
    assert sb.apply_patch(good["patch"][0])[0]
    verdict = validate(sb, plan, context_from_break(good, sb, plan), baseline, settings=settings)
    assert verdict.ok, verdict.reasons
    assert verdict.test_exit == 1
    assert verdict.preexisting_failures == ["tests/test_flaky.py::test_always_fails"]

    git("checkout", "--", ".", cwd=sb.workdir)
    bad = load_break("patch_breaks_tests")
    assert sb.apply_patch(bad["patch"][0])[0]
    verdict = validate(sb, plan, context_from_break(bad, sb, plan), baseline, settings=settings)
    assert not verdict.ok
    assert any("introduces new test failures" in r and "test_billing" in r for r in verdict.reasons)


# --------------------------------------------------------------------------------------
# review regressions
# --------------------------------------------------------------------------------------


def _red_clone(tmp_path: Path, settings, name: str, edit) -> LocalSandbox:
    """A prepared sandbox on a copy of the fixture repo changed by ``edit(src)``."""
    import shutil

    src = tmp_path / f"{name}-src"
    shutil.copytree(AGENT_REPO, src, ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache"))
    edit(src)
    origin = tmp_path / f"{name}-origin"
    make_git_repo(src, origin)
    sb = LocalSandbox(settings=settings, root=tmp_path / f"{name}-sb", wall_clock_s=600)
    sb.prepare(clone_url_for(origin), "main")
    return sb


RENAME = {
    "path": "agent/billing.py",
    "old": "        source=source,\n",
    "new": "        payment_method=source,\n",
}


@pytest.mark.parametrize("fmt", [None, "full", "concise"])
def test_count_violations_reads_real_ruff_output(tmp_path: Path, fmt):
    """ruff >= 0.9 prints ``CODE msg`` + `` --> path:line:col`` by default; every format
    must count the same findings."""
    import os
    import subprocess
    import sys

    (tmp_path / "bad.py").write_text("import os\n\n\ndef f():\n    return undefined_xyz\n")
    env = {k: v for k, v in os.environ.items() if k != "RUFF_OUTPUT_FORMAT"}
    if fmt:
        env["RUFF_OUTPUT_FORMAT"] = fmt
    ruff = str(Path(sys.executable).parent / "ruff")
    proc = subprocess.run(
        [ruff, "check", "--no-cache", "--isolated", "--select", "F", "."],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert count_violations("ruff check .", proc.stdout) == 2
    no_summary = "\n".join(ln for ln in proc.stdout.splitlines() if not ln.startswith("Found"))
    assert count_violations("ruff check .", no_summary) == 2
    assert count_violations("ruff check .", "Found 3 errors (1 fixed, 2 remaining).\n") == 2


def test_new_ruff_finding_on_a_red_static_baseline_is_rejected(tmp_path: Path, settings, data_dir):
    def edit(src: Path) -> None:
        p = src / "agent" / "unrelated.py"
        p.write_text("import os\n" + p.read_text())  # one pre-existing F401

    sb = _red_clone(tmp_path, settings, "ruff-red", edit)
    try:
        plan = detect_test_command(sb.workdir, {})
        baseline = run_baseline(sb, plan, repo_key="acme/ruff-red", settings=settings)
        assert baseline.exit_code == 0
        assert baseline.static["ruff check ."]["exit_code"] == 1
        assert baseline.static["ruff check ."]["violations"] >= 1
        brk = load_break("stripe_charge_source_renamed")
        assert sb.apply_patch(RENAME)[0]
        assert sb.apply_patch(
            {
                "path": "agent/billing.py",
                "old": "import stripe\n",
                "new": "import json\nimport stripe\n",
            }
        )[0]  # an unused import the tests never notice
        verdict = validate(sb, plan, context_from_break(brk, sb, plan), baseline, settings=settings)
        assert not verdict.ok
        assert any("static check `ruff check .` fails" in r for r in verdict.reasons), (
            verdict.reasons
        )
        # the same red baseline with only the real fix stays acceptable
        git("checkout", "--", "agent/billing.py", cwd=sb.workdir)
        assert sb.apply_patch(RENAME)[0]
        verdict = validate(sb, plan, context_from_break(brk, sb, plan), baseline, settings=settings)
        assert verdict.ok, verdict.reasons
        assert "pre-existing finding(s), none added" in verdict.static_result
    finally:
        sb.cleanup()


MCP_PARAMS_SRC = """\
from __future__ import annotations


async def forecast(session, city: str) -> str:
    params = {"city": city}
    params["units"] = "metric"
    result = await session.call_tool("get_weather", arguments=params)
    return str(result)


def via_name(session, city: str):
    params = {"city": city, "units": "metric"}
    return session.call_tool(name="get_weather", arguments=params)


def dynamic(session, city: str):
    return session.call_tool("get_weather", arguments=build_args(city))


def positional(session, city: str):
    p = dict(city=city)
    p.update(units="metric")
    return session.call_tool("get_weather", p)
"""


def test_extract_call_keys_python_follows_an_arguments_variable():
    assert extract_call_keys_python(MCP_PARAMS_SRC, "get_weather", 7) == ["city", "units"]
    assert extract_call_keys_python(MCP_PARAMS_SRC, "get_weather", 13) == ["city", "units"]
    assert extract_call_keys_python(MCP_PARAMS_SRC, "get_weather", 17) is None
    assert extract_call_keys_python(MCP_PARAMS_SRC, "get_weather", 23) == ["city", "units"]
    none_src = 'def f(s):\n    return s.call_tool("get_weather", arguments=None)\n'
    assert extract_call_keys_python(none_src, "get_weather", 2) == []
    # a variable reassigned to something unreadable is not guessed at
    src = 'def f(s, c):\n    p = {"city": c}\n    p = load()\n    return s.call_tool("get_weather", arguments=p)\n'
    assert extract_call_keys_python(src, "get_weather", 4) is None
    src = 'def f(s, c, k):\n    p = {"city": c}\n    p[k] = 1\n    return s.call_tool("get_weather", arguments=p)\n'
    assert extract_call_keys_python(src, "get_weather", 4) is None


def test_mcp_check_accepts_a_correct_fix_through_an_arguments_variable(
    sandbox: LocalSandbox, settings
):
    brk = load_break("mcp_get_weather_units_required")
    plan = detect_test_command(sandbox.workdir, {})
    ctx = context_from_break(brk, sandbox, plan)
    sandbox.write_file(
        "agent/weather.py",
        sandbox.read_file("agent/weather.py").replace(
            '    result = client.call_tool("get_weather", {"city": city})\n',
            '    params = {"city": city}\n'
            '    params["units"] = "metric"\n'
            '    result = client.call_tool("get_weather", arguments=params)\n',
        ),
    )
    ok, msg = mcp_schema_check(sandbox, ctx)
    assert ok and "1 call site(s) validated" in msg, msg
    # the same variable without the new required key is still caught
    sandbox.write_file(
        "agent/weather.py",
        sandbox.read_file("agent/weather.py").replace('    params["units"] = "metric"\n', ""),
    )
    ok, msg = mcp_schema_check(sandbox, ctx)
    assert not ok and "units" in msg and "arguments" not in msg


def test_parse_pytest_ids_with_spaces():
    out = (
        "FAILED tests/test_x.py::test_y[a b] - AssertionError\n"
        "FAILED tests/test_x.py::test_z - boom\n"
        "FAILED tests/test_x.py::test_w[a - b] - x\n"
        "ERROR tests/test_a.py\n"
    )
    assert parse_failing_tests(out, "pytest") == [
        "tests/test_a.py",
        "tests/test_x.py::test_w[a - b]",
        "tests/test_x.py::test_y[a b]",
        "tests/test_x.py::test_z",
    ]


def test_new_failure_with_a_spaced_param_id_is_not_hidden_by_a_red_baseline(
    tmp_path: Path, settings, data_dir
):
    def edit(src: Path) -> None:
        (src / "tests" / "test_flaky.py").write_text("def test_always_fails():\n    assert False\n")
        (src / "tests" / "test_amounts.py").write_text(
            "import pytest\n\n"
            "import stripe\n\n"
            "from agent.billing import charge_customer\n\n\n"
            '@pytest.mark.parametrize("label", ["12 500"])\n'
            "def test_amount(label, monkeypatch):\n"
            "    seen = {}\n"
            "    def create(**kw):\n"
            "        seen.update(kw)\n"
            '        return {"id": "x", "status": "ok"}\n'
            '    monkeypatch.setattr(stripe.Charge, "create", create)\n'
            '    charge_customer(1, "usd", "tok")\n'
            '    assert seen["description"] == "Kintsua fixture charge"\n'
        )

    sb = _red_clone(tmp_path, settings, "spaced", edit)
    try:
        plan = detect_test_command(sb.workdir, {})
        baseline = run_baseline(sb, plan, repo_key="acme/spaced", settings=settings)
        assert baseline.failing_tests == ["tests/test_flaky.py::test_always_fails"]
        brk = load_break("stripe_charge_source_renamed")
        assert sb.apply_patch(RENAME)[0]
        assert sb.apply_patch(
            {
                "path": "agent/billing.py",
                "old": '        description="Kintsua fixture charge",\n',
                "new": '        description="charge",\n',
            }
        )[0]  # breaks only the parametrized test whose id has a space
        verdict = validate(sb, plan, context_from_break(brk, sb, plan), baseline, settings=settings)
        assert not verdict.ok
        assert "tests/test_amounts.py::test_amount[12 500]" in verdict.failing_tests
        assert any("new test failures" in r and "12 500" in r for r in verdict.reasons), (
            verdict.reasons
        )
    finally:
        sb.cleanup()


def test_baseline_and_validation_honour_fix_test_timeout_s(
    sandbox: LocalSandbox, settings, data_dir, monkeypatch
):
    monkeypatch.setattr(settings, "fix_test_timeout_s", 1234)
    plan = detect_test_command(sandbox.workdir, {})
    seen: list[float | None] = []
    real = sandbox.run

    def spy(cmd, **kw):
        if cmd == plan.command:
            seen.append(kw.get("timeout_s"))
        return real(cmd, **kw)

    monkeypatch.setattr(sandbox, "run", spy)
    baseline = run_baseline(sandbox, plan, repo_key="acme/t", settings=settings, use_cache=False)
    brk = load_break("stripe_charge_source_renamed")
    assert sandbox.apply_patch(brk["patch"][0])[0]
    verdict = validate(
        sandbox, plan, context_from_break(brk, sandbox, plan), baseline, settings=settings
    )
    assert verdict.ok, verdict.reasons
    assert seen == [1234, 1234]


def test_suite_that_never_ran_is_not_same_failures(tmp_path: Path, settings, data_dir):
    """A test module importing a package the install did not provide: pytest exits 2
    with a collection error on the baseline and on the patched tree; that is not a
    verified patch."""

    def edit(src: Path) -> None:
        (src / "tests" / "test_needs_dep.py").write_text(
            "import not_installed_pkg_xyz  # noqa: F401\n\n\ndef test_x():\n    pass\n"
        )

    sb = _red_clone(tmp_path, settings, "notrun", edit)
    try:
        plan = detect_test_command(sb.workdir, {})
        baseline = run_baseline(sb, plan, repo_key="acme/notrun", settings=settings)
        assert baseline.exit_code == 2 and baseline.failing_tests == ["tests/test_needs_dep.py"]
        assert baseline.not_run and "pytest exit 2" in baseline.not_run
        assert "tests did not run" in baseline.summary()
        again = run_baseline(sb, plan, repo_key="acme/notrun", settings=settings)
        assert again.cached and again.not_run == baseline.not_run
        brk = load_break("stripe_charge_source_renamed")
        assert sb.apply_patch(brk["patch"][0])[0]
        verdict = validate(sb, plan, context_from_break(brk, sb, plan), baseline, settings=settings)
        assert not verdict.ok
        assert any("did not run" in r for r in verdict.reasons), verdict.reasons
        assert verdict.preexisting_failures == []
    finally:
        sb.cleanup()


def test_suite_not_run_reason_shapes():
    assert suite_not_run_reason(0, "", "pytest", [], 3) is None
    assert suite_not_run_reason(1, "1 failed, 2 passed", "pytest", ["t.py::a"], 2) is None
    assert "no tests collected" in suite_not_run_reason(5, "", "pytest", [], None)
    behind_make = "!!! Interrupted: 2 errors during collection !!!\nmake: *** [test] Error 2"
    assert "Interrupted" in suite_not_run_reason(2, behind_make, "make", [], None)
    assert "collection" in suite_not_run_reason(1, "", "custom", ["tests/test_a.py"], None)
    # jest-style names without ``::`` are real failures, not collection errors
    assert suite_not_run_reason(1, "", "jest", ["passes amount"], None) is None


def test_fewer_passing_tests_with_unmatched_failures_is_rejected(
    sandbox: LocalSandbox, settings, data_dir
):
    """A runner whose failure ids cannot be parsed: same exit code, but fewer passes."""
    brk = load_break("stripe_charge_source_renamed")
    plan = TestPlan(
        command=(
            "if grep -q payment_method agent/billing.py; then echo '1 passed'; "
            "else echo '3 passed'; fi; exit 1"
        ),
        runner="custom",
    )
    baseline = run_baseline(sandbox, plan, repo_key="acme/few", settings=settings)
    assert baseline.exit_code == 1 and baseline.tests_passed == 3 and not baseline.not_run
    assert sandbox.apply_patch(brk["patch"][0])[0]
    verdict = validate(
        sandbox, plan, context_from_break(brk, sandbox, plan), baseline, settings=settings
    )
    assert any("fewer tests pass" in r for r in verdict.reasons), verdict.reasons


def test_untracked_install_and_test_artefacts_are_not_the_patch(
    sandbox: LocalSandbox, settings, data_dir
):
    """``npm install`` without a committed lockfile, a report file the suite writes, an
    ``*.egg-info`` from ``pip install -e .``: none of it is the agent's patch."""
    plan = detect_test_command(sandbox.workdir, {})
    baseline = run_baseline(sandbox, plan, repo_key="acme/agent", settings=settings)
    root = Path(sandbox.workdir)
    (root / "package-lock.json").write_text("{\n" + '  "x": 1,\n' * 2000 + "}\n")
    (root / "tests" / ".last_run.json").write_text('{"ok": true}\n')
    (root / "weather_billing_agent.egg-info").mkdir()
    (root / "weather_billing_agent.egg-info" / "PKG-INFO").write_text("Name: x\n" * 400)
    assert sandbox.apply_patch(RENAME)[0]
    assert sandbox.changed_files() == ["agent/billing.py"]
    assert changed_line_count(sandbox.diff()) == 2
    brk = load_break("stripe_charge_source_renamed")
    verdict = validate(
        sandbox, plan, context_from_break(brk, sandbox, plan), baseline, settings=settings
    )
    assert verdict.ok, verdict.reasons
    assert verdict.changed_files == ["agent/billing.py"] and verdict.lines_changed == 2
