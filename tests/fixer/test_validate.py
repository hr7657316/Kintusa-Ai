from __future__ import annotations

from pathlib import Path

import pytest

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
    parse_failing_tests,
    parse_tests_passed,
    run_baseline,
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
            "new": '        payment_method=source,\n        api_key="sk_live_abcdefghijk",\n',
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
