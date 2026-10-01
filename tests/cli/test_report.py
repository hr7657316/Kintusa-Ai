"""``kintsua watch report`` from a hand-written history.jsonl: window filtering, severity
counts, needs-human items, failing sources (cleared by a later successful diff), --json."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from kintsua.cli.main import app
from kintsua.cli.watch import build_report
from kintsua.report.markdown import render_markdown
from kintsua.watchers.base import FileSnapshotStore

runner = CliRunner()


def write_history(d: Path, rows: list[dict]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def change(
    ts: datetime, sev: str, locator: str = "https://mcp.a.test/mcp", summary: str = "x"
) -> dict:
    return {
        "source_id": "11111111-1111-4111-8111-111111111111",
        "source_kind": "mcp",
        "from_version": "1",
        "to_version": "2",
        "ops": [{"op": "removed", "path": "/tools/x", "before": None, "after": None}],
        "raw_diff_ref": "inline",
        "text": None,
        "detected_at": ts.isoformat(),
        "display_name": "A",
        "locator": locator,
        "classification": {
            "severity": sev,
            "category": "removal",
            "summary": summary,
            "affected_paths": ["tool:x"],
            "migration_hint": "stop calling x",
            "rule_hits": ["removed_tool"],
            "llm_confidence": None,
        },
    }


def error(ts: datetime, locator: str, msg: str, name: str = "Failing") -> dict:
    return {
        "error": msg,
        "source_kind": "mcp",
        "locator": locator,
        "display_name": name,
        "detected_at": ts.isoformat(),
    }


def test_build_report_filters_the_window_and_counts(tmp_path):
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    d = tmp_path / ".kintsua"
    write_history(
        d,
        [
            change(now - timedelta(days=10), "breaking", summary="old"),
            change(now - timedelta(days=2), "breaking", summary="recent break"),
            change(now - timedelta(days=1), "safe", locator="https://b.test", summary="safe one"),
            change(now - timedelta(hours=3), "unknown", summary="needs a look"),
            error(now - timedelta(days=3), "cmd failing", "MCPConnectError: refused"),
            error(now - timedelta(days=1), "cmd failing", "MCPConnectError: refused again"),
            error(now - timedelta(days=4), "https://b.test", "boom", name="B"),
        ],
    )
    rep = build_report(FileSnapshotStore(d), timedelta(days=7), now=now)
    st = rep.stats
    assert (st.change_events, st.breaking, st.safe, st.unknown, st.needs_human) == (3, 1, 1, 1, 1)
    assert st.sources_watched == 3 and st.sources_polled == 3
    assert [i.summary for i in rep.items] == ["needs a look", "safe one", "recent break"]
    assert rep.items[0].needs_human and rep.items[2].migration_hint == "stop calling x"
    assert rep.items[2].op_count == 1 and rep.items[2].rule_hits == ["removed_tool"]
    assert [f.locator for f in rep.failing_sources] == ["cmd failing"]  # b.test recovered later
    f = rep.failing_sources[0]
    assert f.consecutive_failures == 2 and f.last_error.endswith("again")
    assert f.failing_since == now - timedelta(days=3)
    md = render_markdown(rep)
    assert "## 🔴 Breaking (1)" in md and "## ⚪ Needs a human look (1)" in md
    assert "| Failing | mcp | 2026-09-28 | MCPConnectError: refused again |" in md


def test_cli_report_markdown_and_since(tmp_path):
    now = datetime.now(UTC)
    d = tmp_path / ".kintsua"
    write_history(
        d,
        [
            change(now - timedelta(days=3), "breaking", summary="three days ago"),
            change(now - timedelta(hours=2), "degraded", summary="two hours ago"),
        ],
    )
    res = runner.invoke(app, ["watch", "report", "--dir", str(d)], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "three days ago" in res.stdout and "two hours ago" in res.stdout
    assert "## 🔴 Breaking (1)" in res.stdout and "## 🟠 Degraded (1)" in res.stdout
    res = runner.invoke(
        app, ["watch", "report", "--dir", str(d), "--since", "1d"], catch_exceptions=False
    )
    assert "three days ago" not in res.stdout and "two hours ago" in res.stdout
    res = runner.invoke(app, ["watch", "report", "--dir", str(d), "--since", "soon"])
    assert res.exit_code == 2
    res = runner.invoke(
        app, ["watch", "report", "--dir", str(tmp_path / "nothing")], catch_exceptions=False
    )
    assert res.exit_code == 0 and "| Change events | 0 |" in res.stdout
    res = runner.invoke(app, ["watch", "report", "--dir", str(d), "--json"], catch_exceptions=False)
    data = json.loads(res.stdout)
    assert data["stats"]["change_events"] == 2 and data["items"][0]["summary"] == "two hours ago"
