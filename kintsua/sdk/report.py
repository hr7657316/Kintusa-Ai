"""Buffered transport: deque -> daemon thread -> ``POST /v1/ingest``, never raising or blocking:
batches every ``flush_interval_s`` s or ``max_batch`` events; a failed POST retried once."""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger("kintsua.sdk")
SDK_VERSION = "0.1.0"
DEFAULT_URL = "https://api.kintsua.dev"
_DEFAULTS = {"schema_fingerprint": None, "error_class": None, "latency_ms": 0, "sample": None}
_OPTS = {"sample": False, "flush_interval_s": 5.0, "max_batch": 100, "enabled": True}
_UA = {"User-Agent": "kintsua-sdk/" + SDK_VERSION}
Opt = str | None


class Reporter:
    """Queue, thread and HTTP client for one (key, repo); ``opts`` are the ``configure()`` knobs."""

    retry_delay_s, timeout_s, max_queue = 1.0, 5.0, 10_000

    def __init__(self, key: Opt, repo: Opt, url: Opt = None, **opts: Any):
        self.key, self.repo, self.url = key or "", repo or "", (url or DEFAULT_URL).rstrip("/")
        self.opts = {**_OPTS, **opts}
        self.sample, self.flush_interval_s = self.opts["sample"], self.opts["flush_interval_s"]
        self.max_batch, self.enabled = self.opts["max_batch"], bool(self.opts["enabled"] and key)
        self._new()

    def _new(self) -> None:  # also in a forked child: its own queue, lock, flusher and client
        self.sent = self.dropped = 0
        self._q: deque[dict[str, Any]] = deque(maxlen=self.max_queue)
        self._lock, self._wake, self._stop = threading.Lock(), threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._client: httpx.Client | None = None
        self._logged: set[str] = set()

    def record(self, tool_name: str, kind: str, outcome: str, **fields: Any) -> None:
        """Enqueue one ingest event (extra fields: fingerprint, error class, latency, sample)."""
        if not self.enabled:
            return
        try:
            name = str(tool_name)[:512]
            event = {"tool_name": name, "kind": kind, "outcome": outcome, **_DEFAULTS, **fields}
            event["latency_ms"] = int(event["latency_ms"])
            event["observed_at"] = datetime.now(UTC).isoformat(timespec="milliseconds")[:-6] + "Z"
            with self._lock:
                self._q.append(event)
                if len(self._q) >= self.max_batch:
                    self._wake.set()
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, daemon=True)
                    self._thread.start()
                    atexit.register(self.close)
        except Exception as e:  # noqa: BLE001 - the SDK must never raise into the agent
            self._debug_once("record", e)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.flush_interval_s)
            self._wake.clear()
            self.flush()

    def flush(self, timeout_s: float | None = None) -> int:
        """Send everything queued, synchronously; returns the number of events delivered."""
        delivered, deadline = 0, (time.monotonic() + timeout_s) if timeout_s else None
        try:
            while not (deadline and time.monotonic() >= deadline):
                with self._lock:
                    batch = [self._q.popleft() for _ in range(min(len(self._q), self.max_batch))]
                if not batch:
                    break
                if self._post(batch, deadline):
                    delivered += len(batch)
                else:
                    self.dropped += len(batch)
        except Exception as e:  # noqa: BLE001
            self._debug_once("flush", e)
        return delivered

    def _post(self, batch: list[dict[str, Any]], deadline: float | None) -> bool:
        body = {"repo": self.repo, "sdk_version": SDK_VERSION, "events": batch}
        headers = {"Authorization": "Bearer " + self.key, **_UA}
        self._client = self._client or httpx.Client(timeout=self.timeout_s)
        url = self.url + "/v1/ingest"
        for attempt in (1, 2):
            t = min(max(deadline - time.monotonic(), 0.1) if deadline else 99, self.timeout_s)
            try:
                r = self._client.post(url, json=body, headers=headers, timeout=t)
                if 200 <= r.status_code < 300:  # a redirect (3xx) delivered nothing
                    self.sent += len(batch)
                    return True
                self._debug_once(f"http_{r.status_code}", r.headers.get("location") or r.text[:200])
            except Exception as e:  # noqa: BLE001
                self._debug_once(type(e).__name__, e)
            if attempt == 1:  # retry once after a pause, then drop the batch
                if deadline and time.monotonic() + self.retry_delay_s > deadline:
                    break
                time.sleep(self.retry_delay_s)
        return False

    def _debug_once(self, failure_class: str, detail: Any) -> None:
        if failure_class not in self._logged:
            self._logged.add(failure_class)
            log.debug("kintsua reporter: %s: %s", failure_class, detail)

    def close(self, timeout_s: float = 2.0) -> None:  # atexit: stop, then flush with a cap
        self._stop.set()
        self._wake.set()
        self.flush(timeout_s)


_default: Reporter | None = None
_pool: dict[tuple[str, str], Reporter] = {}
if hasattr(os, "register_at_fork"):  # the parent's thread is dead in a child: start afresh
    os.register_at_fork(after_in_child=lambda: [r._new() for r in (_default, *_pool.values()) if r])


def configure(
    key: str | None = None,
    repo: str | None = None,
    url: str | None = None,
    sample: bool = False,
    flush_interval_s: float = 5.0,
    max_batch: int = 100,
    enabled: bool = True,
) -> Reporter:
    """Set the process-wide reporter; defaults from ``KINTSUA_KEY`` / ``_REPO`` / ``_URL`` env vars."""
    global _default
    env = os.environ
    args = [v or env.get(f"KINTSUA_{n}") for v, n in ((key, "KEY"), (repo, "REPO"), (url, "URL"))]
    opts = {"sample": sample, "flush_interval_s": flush_interval_s, "max_batch": max_batch}
    old, _default = _default, Reporter(*args, enabled=enabled, **opts)
    if old is not None:
        old.close()
    return _default


def get_reporter(repo: str | None = None, key: str | None = None) -> Reporter:
    """The default reporter, or a pooled one when a ``wrap_*`` call brings its own key/repo."""
    base = _default or configure()
    ident = (key or base.key, repo or base.repo)
    if ident == (base.key, base.repo):
        return base
    return _pool.setdefault(ident, Reporter(*ident, base.url, **base.opts))


def flush(timeout_s: float | None = None) -> int:
    """Force a send of everything buffered by every reporter; returns events delivered."""
    return sum(r.flush(timeout_s) for r in (_default, *_pool.values()) if r is not None)
