"""Worker process: ``python -m kintsua.worker [--kinds a,b] [--once]``.

Imports every component so its job handlers register, seeds the self-perpetuating
``tick`` job, then loops on the Postgres queue.

Shutdown: the first SIGTERM/SIGINT stops claiming new jobs and lets the running one
finish for ``Settings.worker_shutdown_grace_s``. After that (or on a second signal) the
running job is aborted: the interrupt unwinds its handler (the fixer's ``finally`` removes
its sandbox), the queue puts the job straight back for another worker, and any sandbox
still registered is cleaned up. A worker that is SIGKILLed instead leaves its job to the
queue's lease reaper and its Docker sandbox to ``reap_orphaned_sandboxes`` on the next
start.

``python -m kintsua.worker --requeue-failed [--kinds k1,k2] [--since 24h]`` re-drives
failed jobs (after an outage outlasted their retries) and exits.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from kintsua.core.config import Settings, get_settings
from kintsua.core.db import new_session, session_scope
from kintsua.core.queue import HANDLERS, requeue_failed, run_worker
from kintsua.core.storage import storage_problem
from kintsua.core.util import parse_duration, utcnow
from kintsua.log import setup_logging

log = logging.getLogger(__name__)

# label kintsua.fixer.sandbox.DockerSandbox puts on its containers and volumes
SANDBOX_LABEL = "kintsua.job"
# a live fix job cannot hold a sandbox longer than its wall clock; this is the slack
SANDBOX_REAP_SLACK_S = 600


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


class Shutdown:
    """Signal handler: first signal = stop claiming (and arm the grace timer), second
    signal (or the timer) = raise ``KeyboardInterrupt`` in the main thread to abort the
    running job."""

    def __init__(self, stop: threading.Event, grace_s: float) -> None:
        self.stop = stop
        self.grace_s = grace_s
        self.signals = 0
        self._timer: threading.Timer | None = None

    def handle(self, signum: int, frame: Any) -> None:
        self.signals += 1
        if self.signals == 1:
            self.stop.set()
            if self.grace_s > 0:
                log.warning(
                    "signal %s: no new jobs; the running job has %ss to finish "
                    "(signal again to abort it now)",
                    signum,
                    self.grace_s,
                )
                self._timer = threading.Timer(self.grace_s, self._escalate)
                self._timer.daemon = True
                self._timer.start()
            else:
                log.warning(
                    "signal %s: no new jobs; waiting for the running job (signal again to abort)",
                    signum,
                )
            return
        log.warning("signal %s: aborting the running job", signum)
        raise KeyboardInterrupt

    def _escalate(self) -> None:
        log.warning("shutdown grace of %ss is over; aborting the running job", self.grace_s)
        os.kill(os.getpid(), signal.SIGINT)

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()


def cleanup_active_sandboxes() -> int:
    """Best-effort removal of fixer sandboxes still registered in this process."""
    try:
        from kintsua.fixer.sandbox import active_sandboxes, unregister_active
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for sb in active_sandboxes():
        try:
            sb.cleanup()
            n += 1
        except Exception:  # noqa: BLE001
            log.exception("could not clean up sandbox %s", getattr(sb, "job_id", "?"))
        finally:
            unregister_active(sb)
    return n


def _docker_time(value: Any) -> datetime | None:
    """Docker's RFC 3339 timestamps (nanosecond fractions, ``Z`` or an offset)."""
    if not isinstance(value, str):
        return None
    m = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?", value.strip()
    )
    if not m:
        return None
    micro = int((m.group(2) or "0")[:6].ljust(6, "0"))
    tz = m.group(3)
    tzinfo = UTC if tz in (None, "Z") else datetime.fromisoformat(f"2000-01-01T00:00:00{tz}").tzinfo
    return datetime.fromisoformat(m.group(1)).replace(microsecond=micro, tzinfo=tzinfo)


def reap_orphaned_sandboxes(
    settings: Settings, *, client: Any = None, now: datetime | None = None
) -> int:
    """Remove fixer containers and volumes a killed worker left on the Docker host: anything
    labelled ``kintsua.job`` older than the sandbox wall clock plus slack (no live fix job
    is that old). Docker backend only; best-effort. Returns how many objects were removed."""
    if client is None:
        if settings.sandbox_backend != "docker":
            return 0
        try:
            import docker

            client = docker.from_env()
        except Exception as e:  # noqa: BLE001
            log.warning("sandbox reaper: Docker is not reachable: %s", e)
            return 0
    cutoff = (now or utcnow()) - timedelta(
        seconds=settings.sandbox_wall_clock_s + SANDBOX_REAP_SLACK_S
    )
    removed = 0
    for what, lister, created_key in (
        (
            "container",
            lambda: client.containers.list(all=True, filters={"label": SANDBOX_LABEL}),
            "Created",
        ),
        ("volume", lambda: client.volumes.list(filters={"label": SANDBOX_LABEL}), "CreatedAt"),
    ):
        try:
            objs = lister()
        except Exception as e:  # noqa: BLE001
            log.warning("sandbox reaper: could not list %ss: %s", what, e)
            continue
        for obj in objs:
            attrs = getattr(obj, "attrs", None) or {}
            created = _docker_time(attrs.get(created_key))
            if created is None or created > cutoff:
                continue
            try:
                obj.remove(force=True)
                removed += 1
                log.warning(
                    "sandbox reaper: removed orphaned %s %s (created %s)",
                    what,
                    getattr(obj, "name", "?"),
                    created.isoformat(),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("sandbox reaper: could not remove %s: %s", what, e)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kintsua-worker")
    parser.add_argument("--kinds", help="comma-separated job kinds to process (default: all)")
    parser.add_argument("--once", action="store_true", help="drain the queue and exit")
    parser.add_argument("--no-tick", action="store_true", help="do not seed the scheduler tick job")
    parser.add_argument(
        "--requeue-failed",
        action="store_true",
        help="put failed jobs (of --kinds, failed within --since) back in the queue and exit",
    )
    parser.add_argument("--since", help="with --requeue-failed: e.g. 24h, 7d (default: all)")
    args = parser.parse_args(argv)
    setup_logging()
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()] if args.kinds else None

    if args.requeue_failed:
        since = utcnow() - parse_duration(args.since) if args.since else None
        with session_scope() as session:
            n = requeue_failed(session, kinds=kinds, since=since)
        log.info("requeued %s failed job(s)", n)
        return 0

    settings = get_settings()
    problem = storage_problem(settings)
    if problem:
        log.error("refusing to start: %s", problem)
        return 2
    register_all_handlers()
    reap_orphaned_sandboxes(settings)
    if not args.no_tick:
        ensure_tick()
    log.info("worker starting; handlers=%s kinds=%s", sorted(HANDLERS), kinds or "all")
    stop = threading.Event()
    shutdown = Shutdown(stop, settings.worker_shutdown_grace_s)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown.handle)
    n: int | str = "?"
    try:
        n = run_worker(new_session, kinds, once=args.once, stop_event=stop)
    except KeyboardInterrupt:
        log.warning("worker aborted the running job (it is back in the queue)")
    finally:
        shutdown.cancel()
        cleanup_active_sandboxes()
    log.info("worker exiting after %s jobs", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
