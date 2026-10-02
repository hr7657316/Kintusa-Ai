"""A module nothing in the affected call sites imports (outside the fix radius)."""

from __future__ import annotations


def daily_report(rows: list[dict[str, str]]) -> str:
    return "\n".join(f"{r['id']}: {r['status']}" for r in rows)
