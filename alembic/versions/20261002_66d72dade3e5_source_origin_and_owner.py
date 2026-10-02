"""source origin and owner

Adds ``source.origin`` (seed | customer | push | runtime | observed) and
``source.owner_org_id``, widens ``source.display_name`` to text, and backfills the origin
of existing rows: entries of kintsua/seed/sources.yml become ``seed``, CLI-pushed rows
``push`` and synthetic runtime rows ``observed``. Everything else stays ``customer``,
which is the safe default (no stdio execution, network-guarded, private).

Revision ID: 66d72dade3e5
Revises: c200016e7165
Create Date: 2026-10-02 04:50:00
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
import yaml
from alembic import op

revision = "66d72dade3e5"
down_revision = "c200016e7165"
branch_labels = None
depends_on = None

SEED_FILE = Path(__file__).resolve().parents[2] / "kintsua" / "seed" / "sources.yml"


def _seed_pairs() -> list[tuple[str, str]]:
    try:
        data = yaml.safe_load(SEED_FILE.read_text("utf-8")) or {}
    except OSError:
        return []
    raw = data.get("sources") if isinstance(data, dict) else data
    return [
        (str(e["kind"]), str(e["locator"]).strip())
        for e in raw or []
        if isinstance(e, dict) and e.get("kind") and e.get("locator")
    ]


def upgrade() -> None:
    op.add_column(
        "source",
        sa.Column("origin", sa.String(length=16), server_default="customer", nullable=False),
    )
    op.add_column("source", sa.Column("owner_org_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_source_owner_org_id_org", "source", "org", ["owner_org_id"], ["id"], ondelete="CASCADE"
    )
    op.alter_column(
        "source",
        "display_name",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=False,
    )
    conn = op.get_bind()
    for kind, locator in _seed_pairs():
        conn.execute(
            sa.text("UPDATE source SET origin = 'seed' WHERE kind = :k AND locator = :l"),
            {"k": kind, "l": locator},
        )
    conn.execute(
        sa.text(
            "UPDATE source SET origin = 'push' WHERE meta->>'pushed_by' = 'cli' AND origin = 'customer'"
        )
    )
    conn.execute(sa.text("UPDATE source SET origin = 'observed' WHERE locator LIKE 'observed:%'"))


def downgrade() -> None:
    op.alter_column(
        "source",
        "display_name",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
    op.drop_constraint("fk_source_owner_org_id_org", "source", type_="foreignkey")
    op.drop_column("source", "owner_org_id")
    op.drop_column("source", "origin")
