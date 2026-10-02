"""``kintsua watch`` end to end against the stdio fixture MCP server
(tests/fixtures/watchers/mcp_server.py): snapshot writes the lockfile, diff with no change
exits 0, a schema change (KINTSUA_MCP_VARIANT=v2) exits 1 with a breaking row and grows
history.jsonl, --github-action annotations, --json shape, --update, report, error exits."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kintsua.cli.main import app
from kintsua.watchers.mcp import tool_fingerprint

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = f"{sys.executable} {REPO_ROOT / 'tests' / 'fixtures' / 'watchers' / 'mcp_server.py'}"
runner = CliRunner()
pytestmark = pytest.mark.timeout(240)


def watch(*args: str, variant: str = "v1"):
    cmd = [
        "watch",
        *args,
        "--env",
        f"KINTSUA_MCP_VARIANT={variant}",
        "--env",
        f"PYTHONPATH={REPO_ROOT}",
    ]
    return runner.invoke(app, cmd, catch_exceptions=False)


def lockfile(d: Path) -> dict:
    files = list(d.glob("*.lock.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


def history(d: Path) -> list[dict]:
    p = d / "history.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture(scope="module")
def lock_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("kintsua") / ".kintsua"
    res = watch("snapshot", "--server", SERVER, "--dir", str(d))
    assert res.exit_code == 0, res.output
    return d


def test_snapshot_writes_a_lockfile_with_fingerprints(lock_dir):
    data = lockfile(lock_dir)
    assert data["kintsua_lockfile"] == 1 and data["kind"] == "mcp" and data["locator"] == SERVER
    assert data["version"] == "1.0.0" and data["display_name"] == "kintsua-fixture"
    assert set(data["body"]["tools"]) == {"get_weather", "search"}
    assert set(data["fingerprints"]) == {"get_weather", "search"}
    schema = data["body"]["tools"]["get_weather"]["inputSchema"]
    assert data["fingerprints"]["get_weather"] == tool_fingerprint(schema)
    assert data["content_hash"] and data["source_id"] and data["captured_at"]


def test_snapshot_prints_the_one_line_summary(tmp_path):
    res = watch("snapshot", "--server", SERVER, "--dir", str(tmp_path / "k"))
    assert res.exit_code == 0
    line = res.stdout.strip().splitlines()[-1]
    assert line.startswith("kintsua watch: 2 tools snapshotted -> ")
    assert ".lock.json" in line and "https://github.com/apps/kintsua" in line
    assert not history(tmp_path / "k")


def test_diff_without_changes_exits_zero(lock_dir):
    res = watch("diff", "--server", SERVER, "--dir", str(lock_dir))
    assert res.exit_code == 0, res.output
    assert "no changes in kintsua-fixture (2 tools)" in res.stdout
    assert not history(lock_dir)


def test_diff_with_a_breaking_change_exits_one_and_records_history(lock_dir):
    res = watch("diff", "--server", SERVER, "--dir", str(lock_dir), variant="v2")
    assert res.exit_code == 1, res.output
    out = res.stdout
    assert "BREAKING" in out and "removal" in out
    for needle in (
        "removed_tool",
        "required_added",
        "type_changed",
        "tool:search",
        "tool:get_weather",
    ):
        assert needle in out, needle
    assert "kintsua-fixture" in out and "1.0.0 -> 2.0.0" in out
    assert "history.jsonl" in out and "https://github.com/apps/kintsua" in out
    rows = history(lock_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row["classification"]["severity"] == "breaking"
    assert row["classification"]["rule_hits"][0] == "removed_tool"
    assert "tool:search" in row["classification"]["affected_paths"]
    ops = {o["op"] for o in row["ops"]}
    assert ops >= {"removed", "added", "required_added", "type_changed", "text_changed"}
    assert (row["from_version"], row["to_version"], row["source_kind"]) == ("1.0.0", "2.0.0", "mcp")
    assert row["detected_at"] and row["locator"] == SERVER and row["exit_code"] == 1
    assert row["text"] and "get_weather:" in row["text"]  # vendor text travels as data
    assert lockfile(lock_dir)["version"] == "1.0.0"  # untouched without --update


def test_github_action_annotations_and_step_summary(lock_dir, tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    res = watch("diff", "--server", SERVER, "--dir", str(lock_dir), "--github-action", variant="v2")
    assert res.exit_code == 1
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("::")]
    assert lines[0].startswith("::error file=") and ".lock.json" in lines[0]
    assert "title=kintsua watch%3A breaking::" in lines[0] and "Tool search was removed" in lines[0]
    assert any(
        ln.startswith("::error file=") and "removed /tools/search [removed_tool]" in ln
        for ln in lines
    )
    assert any(ln.startswith("::error file=") and "required_added" in ln for ln in lines)
    assert any(ln.startswith("::notice file=") and "added /tools/lookup" in ln for ln in lines)
    md = summary.read_text()
    assert md.startswith("## kintsua watch: kintsua-fixture (1.0.0 -> 2.0.0)")
    assert "BREAKING" in md and "| tool:search | removed |" in md and "`/tools/search`" in md
    assert len(history(lock_dir)) == 2


def test_json_output_shape(lock_dir):
    res = watch("diff", "--server", SERVER, "--dir", str(lock_dir), "--json", variant="v2")
    assert res.exit_code == 1
    data = json.loads(res.stdout)
    assert data["changed"] is True and data["exit_code"] == 1 and data["fail_on"] == "breaking"
    assert data["source"] == {"kind": "mcp", "locator": SERVER, "display_name": "kintsua-fixture"}
    assert data["lockfile"].endswith(".lock.json")
    assert data["classification"]["severity"] == "breaking"
    assert data["event"]["to_version"] == "2.0.0" and data["event"]["source_kind"] == "mcp"
    assert {r["op"] for r in data["rows"]} >= {"removed", "required_added", "type_changed"}
    assert all(
        {"operation", "op", "path", "severity", "rule", "detail"} <= set(r) for r in data["rows"]
    )
    breaking = [r for r in data["rows"] if r["severity"] == "breaking"]
    assert {r["rule"] for r in breaking} == {"removed_tool", "required_added", "type_changed"}


def test_json_without_changes(lock_dir):
    res = watch("diff", "--server", SERVER, "--dir", str(lock_dir), "--json")
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["changed"] is False and data["exit_code"] == 0


def test_fail_on_degraded_and_update_rewrites_the_lockfile(lock_dir):
    res = watch(
        "diff",
        "--server",
        SERVER,
        "--dir",
        str(lock_dir),
        "--fail-on",
        "degraded",
        "--update",
        variant="v2",
    )
    assert res.exit_code == 1 and "lockfile updated" in res.stdout
    data = lockfile(lock_dir)
    assert data["version"] == "2.0.0" and set(data["fingerprints"]) == {"get_weather", "lookup"}
    res = watch("diff", "--server", SERVER, "--dir", str(lock_dir), variant="v2")
    assert res.exit_code == 0 and "no changes" in res.stdout


def test_report_renders_markdown_from_history(lock_dir):
    res = runner.invoke(
        app, ["watch", "report", "--dir", str(lock_dir), "--since", "1d"], catch_exceptions=False
    )
    assert res.exit_code == 0, res.output
    md = res.stdout
    assert md.startswith("# Kintsua watch report")
    assert "## 🔴 Breaking" in md and "Tool search was removed" in md
    assert "| Change events | 4 |" in md and "**kintsua-fixture** · mcp · 1.0.0 → 2.0.0" in md
    assert "`tool:search`" in md and "https://github.com/apps/kintsua" in md


def test_report_json(lock_dir):
    res = runner.invoke(
        app, ["watch", "report", "--dir", str(lock_dir), "--json"], catch_exceptions=False
    )
    data = json.loads(res.stdout)
    assert data["stats"]["breaking"] == 4 and data["items"][0]["severity"] == "breaking"
    assert data["items"][0]["source_name"] == "kintsua-fixture"


def test_diff_without_a_lockfile_exits_four_with_a_hint(tmp_path):
    res = watch("diff", "--server", SERVER, "--dir", str(tmp_path / "empty"))
    assert res.exit_code == 4  # its own code: the Action snapshots on 4, never on a usage error
    assert "no lockfile" in res.stderr and "kintsua watch snapshot --server" in res.stderr


def test_a_corrupt_lockfile_is_a_usage_error_not_the_gate(tmp_path):
    d = tmp_path / ".kintsua"
    assert watch("snapshot", "--server", SERVER, "--dir", str(d)).exit_code == 0
    (lock,) = d.glob("*.lock.json")
    text = lock.read_text()
    lock.write_text(f"<<<<<<< HEAD\n{text}=======\n{text}>>>>>>> branch\n")
    res = watch("diff", "--server", SERVER, "--dir", str(d))
    assert res.exit_code == 2, res.output  # never 1, the "breaking change" code
    assert "is not a valid kintsua lockfile" in res.stderr and lock.name in res.stderr
    assert "Traceback" not in res.output
    lock.write_text('{"kintsua_lockfile": 1}\n')  # JSON, but not a lockfile
    res = watch("diff", "--server", SERVER, "--dir", str(d))
    assert res.exit_code == 2 and "is not a valid kintsua lockfile" in res.stderr


def test_connection_failure_exits_three_and_is_recorded(tmp_path):
    res = runner.invoke(
        app,
        [
            "watch",
            "snapshot",
            "--server",
            "kintsua-no-such-binary --x",
            "--dir",
            str(tmp_path / "k"),
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 3
    assert "could not fetch" in res.stderr and "kintsua-no-such-binary" in res.stderr
    rows = history(tmp_path / "k")
    assert rows and "error" in rows[0] and rows[0]["locator"] == "kintsua-no-such-binary --x"
    res = runner.invoke(
        app, ["watch", "report", "--dir", str(tmp_path / "k")], catch_exceptions=False
    )
    assert "Sources failing" in res.stdout and "kintsua-no-such-binary" in res.stdout


def test_missing_auth_env_var_exits_three(tmp_path):
    res = runner.invoke(
        app,
        [
            "watch",
            "snapshot",
            "--server",
            "https://127.0.0.1:9/mcp",
            "--auth-env",
            "KINTSUA_TEST_TOKEN_UNSET",
            "--dir",
            str(tmp_path),
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 3 and "KINTSUA_TEST_TOKEN_UNSET" in res.stderr


def test_usage_errors(tmp_path):
    res = runner.invoke(app, ["watch", "diff", "--dir", str(tmp_path)])
    assert res.exit_code == 2 and "exactly one of" in res.stderr
    res = runner.invoke(app, ["watch", "snapshot", "--server", "x", "--spec", "y"])
    assert res.exit_code == 2 and "exactly one of" in res.stderr
    res = runner.invoke(
        app,
        ["watch", "snapshot", "--server", SERVER, "--header", "nocolon", "--dir", str(tmp_path)],
    )
    assert res.exit_code == 2 and "K=V" in res.stderr
    res = runner.invoke(
        app, ["watch", "diff", "--server", SERVER, "--fail-on", "nope", "--dir", str(tmp_path)]
    )
    assert res.exit_code == 2


def test_version_and_help():
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0 and res.stdout.startswith("kintsua ")
    res = runner.invoke(app, ["watch", "--help"])
    assert res.exit_code == 0
    for cmd in ("snapshot", "diff", "report"):
        assert cmd in res.stdout
