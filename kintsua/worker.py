"""Worker process: ``python -m kintsua.worker [--kinds a,b] [--once]``.

Imports every component so its job handlers register, seeds the self-perpetuating
``tick`` job, then loops on the Postgres queue.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading

from kintsua.core.db import new_session, session_scope
from kintsua.core.queue import HANDLERS, run_worker
from kintsua.log import setup_logging

log = logging.getLogger(__name__)


def register_all_handlers() -> None:
    """Import side effects register ``@job_handler`` functions."""
    import kintsua.api.billing  # noqa: F401
    import kintsua.classify.run  # noqa: F401
    import kintsua.fixer.run  # noqa: F401
    import kintsua.indexer.run  # noqa: F401
    import kintsua.matcher.match  # noqa: F401
    import kintsua.matcher.runtime  # noqa: F401
    import kintsua.report.run  # noqa: F401
    import kintsua.watchers.run  # noqa: F401


def ensure_tick() -> None:
    from kintsua.watchers.run import ensure_tick_job

    with session_scope() as session:
        ensure_tick_job(session)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kintsua-worker")
    parser.add_argument("--kinds", help="comma-separated job kinds to process (default: all)")
    parser.add_argument("--once", action="store_true", help="drain the queue and exit")
    parser.add_argument("--no-tick", action="store_true", help="do not seed the scheduler tick job")
    args = parser.parse_args(argv)
    setup_logging()
    register_all_handlers()
    if not args.no_tick:
        ensure_tick()
    kinds = [k.strip() for k in args.kinds.split(",")] if args.kinds else None
    log.info("worker starting; handlers=%s kinds=%s", sorted(HANDLERS), kinds or "all")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    n = run_worker(new_session, kinds, once=args.once, stop_event=stop)
    log.info("worker exiting after %s jobs", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
