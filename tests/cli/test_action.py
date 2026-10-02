"""The composite GitHub Action (``action.yml``): its ``kintsua watch diff`` step runs with a
stand-in ``kintsua`` on PATH. Only "no lockfile yet" (exit 4) becomes a first snapshot; a usage
error (exit 2, e.g. a bad ``fail-on``) fails the step instead of silently passing CI."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ACTION = Path(__file__).resolve().parents[2] / "action.yml"
FAKE = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
if [ "$2" = "diff" ]; then exit "$FAKE_DIFF_RC"; fi
exit "${FAKE_SNAPSHOT_RC:-0}"
"""
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run_step(tmp_path: Path, diff_rc: int, **env: str) -> tuple[int, list[str], str]:
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    script = next(s for s in steps if s.get("id") == "watch")["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "kintsua"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    log, out = tmp_path / "calls.log", tmp_path / "github_output"
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SERVER": "https://mcp.vendor.test/mcp",
        "SPEC": "",
        "FAIL_ON": "breaking",
        "PUSH": "false",
        "KINTSUA_KEY": "",
        "KINTSUA_URL": "https://api.kintsua.dev",
        "KINTSUA_REPO": "acme/agent",
        "DIR": ".kintsua",
        "AUTH_ENV": "",
        "SNAPSHOT_IF_MISSING": "true",
        "UPDATE": "false",
        "GITHUB_OUTPUT": str(out),
        "FAKE_LOG": str(log),
        "FAKE_DIFF_RC": str(diff_rc),
    }
    proc = subprocess.run(
        ["bash", "-c", script], env={**base, **env}, capture_output=True, text=True, timeout=30
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, calls, out.read_text() if out.exists() else ""


def test_no_lockfile_writes_the_first_snapshot(tmp_path):
    rc, calls, out = run_step(tmp_path, diff_rc=4)
    assert rc == 0 and out.strip() == "exit-code=0"
    assert [c.split()[1] for c in calls] == ["diff", "snapshot"]
    assert "--fail-on" not in calls[1]


def test_a_usage_error_is_never_turned_into_a_snapshot(tmp_path):
    rc, calls, out = run_step(tmp_path, diff_rc=2)
    assert rc == 2 and out.strip() == "exit-code=2"
    assert [c.split()[1] for c in calls] == ["diff"]


def test_without_snapshot_if_missing_no_lockfile_fails(tmp_path):
    rc, calls, _ = run_step(tmp_path, diff_rc=4, SNAPSHOT_IF_MISSING="false")
    assert rc == 4 and len(calls) == 1


@pytest.mark.parametrize("rc_in", [0, 1, 3])
def test_other_exit_codes_pass_through(tmp_path, rc_in):
    rc, calls, out = run_step(tmp_path, diff_rc=rc_in)
    assert rc == rc_in and out.strip() == f"exit-code={rc_in}" and len(calls) == 1


@pytest.mark.parametrize("fail_on", ["major", "Breaking", ""])
def test_a_bad_fail_on_fails_before_running_anything(tmp_path, fail_on):
    rc, calls, out = run_step(tmp_path, diff_rc=0, FAIL_ON=fail_on)
    assert rc == 2 and calls == [] and out.strip() == "exit-code=2"


def test_the_cli_exit_codes_match_what_the_action_expects():
    from kintsua.cli import watch

    assert (watch.EXIT_GATE, watch.EXIT_USAGE, watch.EXIT_FETCH) == (1, 2, 3)
    assert watch.EXIT_NO_LOCKFILE == 4
