"""Reporter: the ingest payload, batching (interval / max_batch), retry once then drop, never
raising, DEBUG-once logging, atexit-style close with a cap, and a real local HTTP server."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import respx

from kintsua.sdk.report import SDK_VERSION, Reporter, configure, flush, get_reporter

URL = "https://api.test"
INGEST = f"{URL}/v1/ingest"
EVENT_KEYS = {
    "tool_name",
    "kind",
    "schema_fingerprint",
    "outcome",
    "error_class",
    "latency_ms",
    "sample",
    "observed_at",
}


def wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


@respx.mock
def test_batch_payload_matches_the_ingest_contract():
    route = respx.post(INGEST).mock(return_value=httpx.Response(200, json={"accepted": 2}))
    rep = Reporter("kts_abc", "acme/agent", URL, flush_interval_s=3600)
    rep.record(
        "get_weather",
        "mcp",
        "ok",
        schema_fingerprint="f" * 64,
        latency_ms=12.6,
        sample={"arg_keys": ["city"]},
    )
    rep.record(
        "GET api.vendor.com/v1/users/{id}",
        "http",
        "error",
        error_class="http_500",
        sample={"status": 500, "body_excerpt": "x"},
    )
    assert rep.flush() == 2
    assert route.call_count == 1
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer kts_abc"
    assert req.headers["Content-Type"] == "application/json"
    body = json.loads(req.content)
    assert set(body) == {"repo", "sdk_version", "events"}
    assert body["repo"] == "acme/agent" and body["sdk_version"] == SDK_VERSION
    first, second = body["events"]
    assert set(first) == EVENT_KEYS
    assert first["tool_name"] == "get_weather" and first["kind"] == "mcp"
    assert first["latency_ms"] == 12 and first["error_class"] is None
    assert first["schema_fingerprint"] == "f" * 64 and first["sample"] == {"arg_keys": ["city"]}
    assert first["observed_at"].endswith("Z") and "T" in first["observed_at"]
    datetime.fromisoformat(first["observed_at"].replace("Z", "+00:00"))
    assert second["error_class"] == "http_500" and second["schema_fingerprint"] is None
    assert rep.sent == 2 and rep.dropped == 0 and len(rep._q) == 0


@respx.mock
def test_background_thread_flushes_on_the_interval():
    route = respx.post(INGEST).mock(return_value=httpx.Response(200))
    rep = Reporter("k", "r", URL, flush_interval_s=0.05)
    rep.record("t", "mcp", "ok")
    assert wait_for(lambda: route.called)
    assert rep.sent == 1 and len(rep._q) == 0
    assert rep._thread is not None and rep._thread.daemon
    rep.close()


@respx.mock
def test_flushes_early_when_max_batch_is_reached():
    route = respx.post(INGEST).mock(return_value=httpx.Response(200))
    rep = Reporter("k", "r", URL, flush_interval_s=3600, max_batch=3)
    for i in range(3):
        rep.record(f"t{i}", "mcp", "ok")
    assert wait_for(lambda: route.called)
    assert len(json.loads(route.calls[0].request.content)["events"]) == 3
    rep.close()


@respx.mock
def test_batches_are_capped_at_max_batch():
    route = respx.post(INGEST).mock(return_value=httpx.Response(200))
    rep = Reporter("k", "r", URL, flush_interval_s=3600, max_batch=10)
    for i in range(25):
        rep.record(f"t{i}", "mcp", "ok")  # the 10th event wakes the thread, which may post too
    rep.flush()
    assert wait_for(lambda: rep.sent == 25)
    sizes = sorted(len(json.loads(c.request.content)["events"]) for c in route.calls)
    assert sizes == [5, 10, 10] and rep.dropped == 0 and len(rep._q) == 0


@respx.mock
def test_retry_once_then_drop():
    route = respx.post(INGEST).mock(side_effect=[httpx.Response(500), httpx.Response(503)])
    rep = Reporter("k", "r", URL, flush_interval_s=3600)
    rep.retry_delay_s = 0.01
    rep.record("a", "mcp", "ok")
    rep.record("b", "mcp", "ok")
    assert rep.flush() == 0
    assert route.call_count == 2  # one retry, then the batch is dropped
    assert rep.dropped == 2 and rep.sent == 0 and len(rep._q) == 0
    route.mock(return_value=httpx.Response(200))  # the next batch flows again
    rep.record("later", "mcp", "ok")
    assert rep.flush() == 1 and rep.sent == 1


@respx.mock
def test_retry_succeeds_on_the_second_attempt():
    route = respx.post(INGEST).mock(side_effect=[httpx.Response(502), httpx.Response(200)])
    rep = Reporter("k", "r", URL, flush_interval_s=3600)
    rep.retry_delay_s = 0.01
    rep.record("a", "mcp", "ok")
    assert rep.flush() == 1
    assert route.call_count == 2 and rep.dropped == 0


def test_retry_waits_one_second_by_default_then_drops():
    rep = Reporter("k", "r", "http://127.0.0.1:9", flush_interval_s=3600)  # connection refused
    rep.record("t", "mcp", "ok")
    t0 = time.monotonic()
    assert rep.flush() == 0
    assert 0.9 <= time.monotonic() - t0 < 5
    assert rep.dropped == 1


@respx.mock
def test_transport_errors_never_raise_and_log_once_per_class(caplog):
    respx.post(INGEST).mock(side_effect=httpx.ConnectError("boom"))
    rep = Reporter("k", "r", URL, flush_interval_s=3600)
    rep.retry_delay_s = 0.01
    with caplog.at_level(logging.DEBUG, logger="kintsua.sdk"):
        rep.record("t", "mcp", "ok")
        assert rep.flush() == 0
        rep.record("u", "mcp", "ok")
        assert rep.flush() == 0
    hits = [r for r in caplog.records if "ConnectError" in r.getMessage()]
    assert len(hits) == 1 and hits[0].levelno == logging.DEBUG
    assert rep.dropped == 2


def test_record_never_raises_on_bad_input():
    rep = Reporter("k", "r", URL, flush_interval_s=3600)

    class Unprintable:
        def __str__(self):
            raise RuntimeError("no str")

    rep.record(Unprintable(), "mcp", "ok")
    rep.record("ok", "mcp", "ok", latency_ms="not a number")
    assert len(rep._q) == 0


def test_disabled_without_a_key_or_when_opted_out():
    rep = Reporter(None, "r", URL)
    rep.record("t", "mcp", "ok")
    assert not rep.enabled and len(rep._q) == 0 and rep._thread is None
    assert not configure().enabled  # no KINTSUA_KEY in the environment
    assert not configure(key="k", enabled=False).enabled


def test_configure_reads_the_environment(monkeypatch):
    monkeypatch.setenv("KINTSUA_KEY", "kts_env")
    monkeypatch.setenv("KINTSUA_REPO", "acme/x")
    monkeypatch.setenv("KINTSUA_URL", "https://ingest.example/")
    rep = configure(flush_interval_s=3600)
    assert rep.enabled and rep.key == "kts_env" and rep.repo == "acme/x"
    assert rep.url == "https://ingest.example"
    assert get_reporter() is rep and get_reporter(repo="acme/x", key="kts_env") is rep
    other = get_reporter(key="kts_other")
    assert other is not rep and other.enabled and other.repo == "acme/x"
    assert other.flush_interval_s == 3600 and get_reporter(key="kts_other") is other
    rep.close()


@respx.mock
def test_module_flush_sends_from_every_reporter():
    route = respx.post(INGEST).mock(return_value=httpx.Response(200))
    base = configure(key="k1", repo="r", url=URL, flush_interval_s=3600)
    other = get_reporter(key="k2")
    base.record("a", "mcp", "ok")
    other.record("b", "http", "ok")
    assert flush() == 2 and route.call_count == 2
    keys = {c.request.headers["Authorization"] for c in route.calls}
    assert keys == {"Bearer k1", "Bearer k2"}


def test_close_is_capped():
    rep = Reporter("k", "r", "http://127.0.0.1:9", flush_interval_s=3600)
    rep.retry_delay_s = 5.0  # a retry that would blow the cap is skipped
    for i in range(5):
        rep.record(f"t{i}", "mcp", "ok")
    t0 = time.monotonic()
    rep.close(timeout_s=0.5)
    assert time.monotonic() - t0 < 2.0
    assert rep._stop.is_set()
    assert wait_for(lambda: not rep._thread.is_alive(), 2.0)


def test_real_local_http_server_roundtrip():
    received: list[tuple[str, str | None, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
            received.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"accepted": 1}')

        def log_message(self, *args):  # keep the test output quiet
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rep = Reporter(
            "kts_live", "acme/agent", f"http://127.0.0.1:{srv.server_port}", flush_interval_s=0.05
        )
        rep.record("get_weather", "mcp", "ok", latency_ms=3)
        assert wait_for(lambda: received)
        path, auth, body = received[0]
        assert path == "/v1/ingest" and auth == "Bearer kts_live"
        assert body["repo"] == "acme/agent" and body["events"][0]["tool_name"] == "get_weather"
        rep.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_record_overhead_is_under_one_millisecond():
    rep = Reporter("k", "r", URL, flush_interval_s=3600, max_batch=10**6)
    n = 2000
    t0 = time.perf_counter()
    for _ in range(n):
        rep.record("tool", "mcp", "ok", latency_ms=1.5, schema_fingerprint="f" * 64)
    per_call = (time.perf_counter() - t0) / n
    assert per_call < 0.001, per_call
    assert len(rep._q) == n
    rep._q.clear()  # never let the atexit flush post these to the fake URL
    rep.close()
