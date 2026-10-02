"""Seed file sanity and loader idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from kintsua.core.models import Source
from kintsua.seed.__main__ import main as seed_main
from kintsua.seed.load import load_seed_sources, upsert_sources

SPEC_VENDORS = {
    "stripe",
    "slack",
    "github",
    "notion",
    "hubspot",
    "linear",
    "atlassian",
    "google",
    "twilio",
    "shopify",
}


def test_seed_file_covers_the_spec_list():
    entries = load_seed_sources()
    assert len(entries) >= 40
    kinds = {e.kind for e in entries}
    assert kinds == {"openapi", "mcp", "pypi", "npm", "changelog"}
    assert {e.vendor for e in entries} >= SPEC_VENDORS
    assert len({(e.kind, e.locator) for e in entries}) == len(entries)
    discovery = [e for e in entries if "googleapis.com/discovery" in e.locator]
    assert len(discovery) >= 4 and all(e.meta.get("format") == "discovery" for e in discovery)
    assert any("drive/v3" in e.locator for e in discovery) and any(
        "gmail/v1" in e.locator for e in discovery
    )
    mcp = [e for e in entries if e.kind == "mcp"]
    assert any(e.locator.startswith("https://") and not e.meta.get("auth_env") for e in mcp)
    assert any(e.meta.get("auth_env") for e in mcp)
    assert any(e.locator.startswith(("npx ", "uvx ")) for e in mcp)
    assert {e.locator for e in entries if e.kind == "pypi"} >= {
        "openai",
        "anthropic",
        "stripe",
        "mcp",
        "langchain",
    }
    assert {e.locator for e in entries if e.kind == "npm"} >= {
        "openai",
        "@anthropic-ai/sdk",
        "@modelcontextprotocol/sdk",
    }
    assert all(e.effective_interval >= 3600 for e in entries)
    assert all(e.locator.startswith("http") for e in entries if e.kind in ("openapi", "changelog"))


def test_loader_is_idempotent_and_merges_meta(session):
    entries = load_seed_sources()
    r1 = upsert_sources(session, entries)
    assert len(r1.created) == len(entries) and not r1.updated and not r1.unchanged
    assert session.scalar(select(func.count()).select_from(Source)) == len(entries)
    r2 = upsert_sources(session, entries)
    assert not r2.created and not r2.updated and len(r2.unchanged) == len(entries)

    first = entries[0]
    row = session.scalar(
        select(Source).where(Source.kind == first.kind, Source.locator == first.locator)
    )
    row.meta = {**row.meta, "custom": "kept"}
    row.display_name = "renamed by an operator"
    session.flush()
    r3 = upsert_sources(session, entries)
    assert r3.updated == [f"{first.kind}:{first.locator}"]
    session.refresh(row)
    assert row.display_name == first.display_name and row.meta["custom"] == "kept"
    assert row.poll_interval_s == first.effective_interval
    assert session.scalar(select(func.count()).select_from(Source)) == len(entries)


def test_dry_run_writes_nothing(session):
    entries = load_seed_sources()[:5]
    r = upsert_sources(session, entries, dry_run=True)
    assert r.dry_run and len(r.created) == 5 and "[dry-run]" in r.summary()
    assert session.scalar(select(func.count()).select_from(Source)) == 0


def test_invalid_seed_files(tmp_path: Path):
    bad_kind = tmp_path / "bad.yml"
    bad_kind.write_text("sources:\n  - {kind: rss, locator: x, display_name: X}\n")
    with pytest.raises(ValueError, match="invalid seed entries"):
        load_seed_sources(bad_kind)
    dup = tmp_path / "dup.yml"
    dup.write_text(
        "sources:\n  - {kind: pypi, locator: openai, display_name: a}\n  - {kind: pypi, locator: openai, display_name: b}\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_seed_sources(dup)
    not_a_list = tmp_path / "shape.yml"
    not_a_list.write_text("sources: {kind: pypi}\n")
    with pytest.raises(ValueError, match="expected a top-level"):
        load_seed_sources(not_a_list)


def test_cli_list_and_dry_run(session, db, capsys):
    assert seed_main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "entries" in out and "pypi" in out
    assert seed_main(["--dry-run"]) == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert session.scalar(select(func.count()).select_from(Source)) == 0
    assert seed_main([]) == 0
    assert "created" in capsys.readouterr().out
    assert session.scalar(select(func.count()).select_from(Source)) == len(load_seed_sources())


def test_seeded_rows_are_trusted_public_and_promote_customer_rows(session):
    from kintsua.watchers.run import find_or_create_source

    entries = load_seed_sources()
    first = entries[0]
    customer = find_or_create_source(session, first.kind, first.locator)
    assert customer.origin == "customer" and not customer.trusted and not customer.public
    upsert_sources(session, entries)
    session.refresh(customer)
    assert customer.origin == "seed" and customer.trusted and customer.public
    rows = session.scalars(select(Source)).all()
    assert rows and all(r.origin == "seed" and r.pollable for r in rows)
