"""The ``weekly_report`` job and the report CLI.

The job builds the last-7-days report, renders Markdown and writes
``<data_dir>/reports/<YYYY-MM-DD>.md`` plus ``latest.md`` / ``latest.json`` (what the API's
``GET /report/weekly`` serves). CLI: ``python -m kintsua.report.run [--since 7d] [--json]
[--write]`` prints the Markdown (or JSON) to stdout.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kintsua.core.config import get_settings
from kintsua.core.queue import JOB_WEEKLY_REPORT, job_handler
from kintsua.core.util import parse_duration, utcnow
from kintsua.report.build import build_weekly_report
from kintsua.report.markdown import WeeklyReport, render_markdown

log = logging.getLogger(__name__)

REPORTS_SUBDIR = "reports"
DEFAULT_WINDOW = timedelta(days=7)


def reports_dir(data_dir: str | Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else Path(get_settings().data_dir)
    return base / REPORTS_SUBDIR


def write_report(report: WeeklyReport, data_dir: str | Path | None = None) -> dict[str, Path]:
    """Write ``<date>.md``, ``latest.md`` and ``latest.json``; returns the paths."""
    d = reports_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(report)
    payload = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)
    dated = d / f"{report.until.strftime('%Y-%m-%d')}.md"
    latest_md = d / "latest.md"
    latest_json = d / "latest.json"
    dated.write_text(markdown, "utf-8")
    latest_md.write_text(markdown, "utf-8")
    latest_json.write_text(payload, "utf-8")
    return {"dated": dated, "latest_md": latest_md, "latest_json": latest_json}


def read_latest(data_dir: str | Path | None = None) -> tuple[str | None, dict[str, Any] | None]:
    """The stored ``latest.md`` and ``latest.json`` (None when no report was written yet)."""
    d = reports_dir(data_dir)
    md = d / "latest.md"
    js = d / "latest.json"
    text = md.read_text("utf-8") if md.exists() else None
    data = json.loads(js.read_text("utf-8")) if js.exists() else None
    return text, data


def build_report_for_window(
    session: Session, *, until: datetime | None = None, window: timedelta = DEFAULT_WINDOW
) -> WeeklyReport:
    until = until or utcnow()
    return build_weekly_report(session, until - window, until)


def _register(kind: str) -> Any:
    """``@job_handler`` unless run as a script (see kintsua.watchers.run)."""
    if __name__ == "__main__":
        return lambda fn: fn
    return job_handler(kind)


@_register(JOB_WEEKLY_REPORT)
def weekly_report(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    window = DEFAULT_WINDOW
    if payload.get("since"):
        window = parse_duration(str(payload["since"]))
    report = build_report_for_window(session, window=window)
    paths = write_report(report)
    log.info(
        "weekly report written: %s (%d items, %d breaking)",
        paths["dated"],
        len(report.items),
        report.stats.breaking,
    )
    return {
        "path": str(paths["dated"]),
        "items": len(report.items),
        "breaking": report.stats.breaking,
        "since": report.since.isoformat(),
        "until": report.until.isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kintsua.report.run")
    parser.add_argument("--since", default="7d", help="window, e.g. 7d, 24h (default 7d)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--write", action="store_true", help="also write <data_dir>/reports/")
    parser.add_argument("--no-safe", action="store_true", help="omit the safe section")
    args = parser.parse_args(argv)

    from kintsua.core.db import new_session
    from kintsua.log import setup_logging

    setup_logging("WARNING")
    session = new_session()
    try:
        report = build_report_for_window(session, window=parse_duration(args.since))
    finally:
        session.close()
    if args.write:
        paths = write_report(report)
        print(f"wrote {paths['dated']}", file=sys.stderr)
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    else:
        sys.stdout.write(render_markdown(report, include_safe=not args.no_safe))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
