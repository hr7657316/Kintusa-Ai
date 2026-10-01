"""wrap_http on httpx.Client / AsyncClient (MockTransport) and requests.Session: normalised
tool names, ok/error outcomes, redacted 512-byte excerpts on 4xx/5xx, streaming left alone."""

from __future__ import annotations

import httpx
import pytest
import requests
from requests.adapters import BaseAdapter

from kintsua.sdk import wrap_http
from kintsua.sdk.wrap import SECRET_RE, http_tool_name

SECRET_BODY = (
    b'{"error": "boom", "api_key": "supersecret123456", "token": "abcdefghijkl"} '
    b"sk_live_abcdefghijklmnop Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlc2lnbmF0dXJl"
)


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/fail"):
        return httpx.Response(500, content=SECRET_BODY + b"x" * 1000)
    if request.url.path.endswith("/boom"):
        raise httpx.ConnectError("no route", request=request)
    return httpx.Response(200, json={"ok": True})


def events(rep) -> list[dict]:
    return list(rep._q)


def test_tool_name_normalisation():
    url = "https://API.Vendor.com/v1/users/123/orders/550e8400-e29b-41d4-a716-446655440000?x=1"
    assert http_tool_name("get", url) == "GET api.vendor.com/v1/users/{id}/orders/{id}"
    assert (
        http_tool_name("POST", "https://api.vendor.com:8443/v1/charges")
        == "POST api.vendor.com/v1/charges"
    )
    assert (
        http_tool_name("GET", "https://h/") == "GET h/"
        and http_tool_name("GET", "https://h") == "GET h/"
    )
    assert http_tool_name("GET", "https://h/o/5f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c") == "GET h/o/{id}"
    assert http_tool_name("GET", "https://h/v2/items") == "GET h/v2/items"  # 'v2' is not an id


def test_sync_httpx_client_ok_and_error(reporter):
    client = wrap_http(httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.get("https://api.vendor.com/v1/users/42/orders?page=2").status_code == 200
    r = client.post("https://api.vendor.com/v1/charges/fail", json={"amount": 1})
    assert r.status_code == 500 and r.content.startswith(SECRET_BODY[:10])  # caller still gets it
    ok, err = events(reporter)
    assert ok["tool_name"] == "GET api.vendor.com/v1/users/{id}/orders"
    assert (ok["kind"], ok["outcome"], ok["error_class"], ok["sample"]) == (
        "http",
        "ok",
        None,
        None,
    )
    assert isinstance(ok["latency_ms"], int)
    assert err["tool_name"] == "POST api.vendor.com/v1/charges/fail"
    assert (err["outcome"], err["error_class"]) == ("error", "http_500")
    assert err["sample"]["status"] == 500
    excerpt = err["sample"]["body_excerpt"]
    for secret in ("supersecret123456", "sk_live_", "eyJhbGci", "abcdefghijkl"):
        assert secret not in excerpt
    assert "[REDACTED]" in excerpt and '"error": "boom"' in excerpt
    assert len(excerpt) <= 600  # first 512 bytes, replacements may lengthen it slightly


async def test_async_httpx_client(reporter):
    client = wrap_http(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async with client:
        assert (await client.get("https://api.vendor.com/v1/users/7")).status_code == 200
        r = await client.get("https://api.vendor.com/v1/users/7/fail")
        assert r.status_code == 500 and r.content.startswith(b'{"error"')
    ok, err = events(reporter)
    assert ok["tool_name"] == "GET api.vendor.com/v1/users/{id}" and ok["outcome"] == "ok"
    assert err["error_class"] == "http_500" and "[REDACTED]" in err["sample"]["body_excerpt"]
    assert "supersecret" not in err["sample"]["body_excerpt"]


def test_connection_errors_are_recorded_and_re_raised(reporter):
    client = wrap_http(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.ConnectError):
        client.get("https://api.vendor.com/boom")
    ev = events(reporter)[0]
    assert (ev["outcome"], ev["error_class"], ev["sample"]) == ("error", "ConnectError", None)
    assert ev["tool_name"] == "GET api.vendor.com/boom"


def test_streaming_error_responses_are_not_consumed(reporter):
    client = wrap_http(httpx.Client(transport=httpx.MockTransport(handler)))
    with client.stream("GET", "https://api.vendor.com/v1/fail") as r:
        assert r.status_code == 500
        assert r.read().startswith(b'{"error"')  # the caller can still read the body
    ev = events(reporter)[0]
    assert ev["error_class"] == "http_500" and ev["sample"]["body_excerpt"] == ""


class FakeAdapter(BaseAdapter):
    def send(self, request, **kwargs):
        resp = requests.Response()
        resp.status_code = 404 if request.url.endswith("/missing") else 200
        resp._content = SECRET_BODY if resp.status_code == 404 else b"{}"
        resp.url, resp.request = request.url, request
        return resp

    def close(self):
        pass


def test_requests_session(reporter):
    s = requests.Session()
    s.mount("https://", FakeAdapter())
    assert wrap_http(s) is s
    assert s.get("https://api.vendor.com/v1/users/9").status_code == 200
    assert s.get("https://api.vendor.com/v1/users/9/missing").status_code == 404
    ok, err = events(reporter)
    assert ok["tool_name"] == "GET api.vendor.com/v1/users/{id}" and ok["outcome"] == "ok"
    assert err["tool_name"] == "GET api.vendor.com/v1/users/{id}/missing"
    assert err["error_class"] == "http_404" and err["sample"]["status"] == 404
    assert "supersecret" not in err["sample"]["body_excerpt"]
    assert "[REDACTED]" in err["sample"]["body_excerpt"]


def test_noop_without_a_key():
    c = httpx.Client()
    send = c.send
    assert wrap_http(c) is c and c.send == send and not hasattr(c, "_kintsua_wrapped")


def test_wrapping_twice_records_once(reporter):
    c = wrap_http(wrap_http(httpx.Client(transport=httpx.MockTransport(handler))))
    c.get("https://api.vendor.com/v1/ping")
    assert len(events(reporter)) == 1


def test_redaction_patterns_cover_the_core_list():
    text = (
        "Authorization: Bearer abcdefghijklmnop123 ghp_abcdefghijklmnopqrstuvwxyz0123 "
        "xoxb-1234567890-abcdef AKIAABCDEFGHIJKLMNOP sk-ant-abcdefghijklmnopqrstuvwxyz "
        'password=hunter2hunter2 "api_key": "kkkkkkkkkk" rk_test_abcdefghij'
    )
    out = SECRET_RE.sub("[REDACTED]", text)
    for s in (
        "abcdefghijklmnop123",
        "ghp_",
        "xoxb-",
        "AKIA",
        "sk-ant-",
        "hunter2",
        "kkkkkkkkkk",
        "rk_test_",
    ):
        assert s not in out, (s, out)
    assert SECRET_RE.sub("[REDACTED]", '{"id": 42, "status": "ok"}') == '{"id": 42, "status": "ok"}'
