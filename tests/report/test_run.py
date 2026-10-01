"""The weekly_report job writes the Markdown/JSON files; the CLI prints Markdown."""

from __future__ import annotations

import json
from pathlib import Path

from kintsua.core.models import Source
from kintsua.core.queue import HANDLERS
from kintsua.report.run import main as report_main
from kintsua.report.run import read_latest, weekly_report


def test_weekly_report_job_writes_dated_and_latest_files(session, settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    session.add(
        Source(kind="pypi", locator="openai", display_name="openai (PyPI)", vendor="openai")
    )
    session.flush()
    assert HANDLERS["weekly_report"] is weekly_report

    out = weekly_report(session, {})
    dated = Path(out["path"])
    assert dated.exists() and dated.parent == tmp_path / "reports"
    assert dated.name.endswith(".md") and out["items"] == 0 and out["breaking"] == 0
    latest_md = tmp_path / "reports" / "latest.md"
    latest_json = tmp_path / "reports" / "latest.json"
    assert latest_md.read_text("utf-8") == dated.read_text("utf-8")
    data = json.loads(latest_json.read_text("utf-8"))
    assert data["stats"]["sources_watched"] == 1 and data["since"] < data["until"]
    text, parsed = read_latest(tmp_path)
    assert (
        text.startswith("# Kintsua weekly drift report") and parsed["stats"]["sources_watched"] == 1
    )
    assert read_latest(tmp_path / "nowhere") == (None, None)

    # a custom window via the payload
    out2 = weekly_report(session, {"since": "24h"})
    assert out2["since"] > out["since"]


def test_cli_prints_markdown_and_json(session, db, capsys):
    assert report_main(["--since", "7d"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Kintsua weekly drift report") and "| Sources watched | 0 |" in out
    assert report_main(["--json", "--since", "1d"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stats"]["change_events"] == 0 and data["items"] == []
