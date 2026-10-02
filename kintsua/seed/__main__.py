"""``python -m kintsua.seed [--file sources.yml] [--dry-run] [--list]``: upsert the seeded
sources into Postgres."""

from __future__ import annotations

import argparse
import sys

from kintsua.seed.load import SEED_FILE, load_seed_sources, upsert_sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kintsua.seed")
    parser.add_argument("--file", default=str(SEED_FILE), help="seed YAML (default: bundled)")
    parser.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    parser.add_argument("--list", action="store_true", help="print the seed entries and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="print every entry's outcome")
    args = parser.parse_args(argv)

    try:
        entries = load_seed_sources(args.file)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.list:
        for e in entries:
            print(f"{e.kind:9} {e.vendor or '-':14} {e.display_name[:40]:40} {e.locator}")
        print(f"\n{len(entries)} entries")
        return 0

    from kintsua.core.db import session_scope
    from kintsua.log import setup_logging

    setup_logging()
    with session_scope() as session:
        result = upsert_sources(session, entries, dry_run=args.dry_run)
        if args.dry_run:
            session.rollback()
    if args.verbose:
        for label in result.created:
            print(f"created   {label}")
        for label in result.updated:
            print(f"updated   {label}")
        for label in result.unchanged:
            print(f"unchanged {label}")
    print(result.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
