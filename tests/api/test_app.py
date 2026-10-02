"""App-level behaviour: health check, structured errors, no stack traces, request ids."""

from __future__ import annotations


def test_healthz_pings_the_database(client, settings):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True, "db": True, "env": settings.env}


def test_healthz_reports_a_dead_database(client, monkeypatch):
    def down():
        raise RuntimeError("connection refused")

    monkeypatch.setattr("kintsua.core.db.ping", down)
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json()["ok"] is False and r.json()["db"] is False


def test_errors_are_structured_json_or_html(client):
    r = client.get("/v1/nope")
    assert r.status_code == 404 and r.json() == {"error": "Not Found", "status": 404}
    r = client.get("/nope", headers={"Accept": "text/html"})
    assert (
        r.status_code == 404 and "text/html" in r.headers["content-type"] and "Not found" in r.text
    )
    r = client.post("/report/subscribe", json={"email": 5})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation failed" and body["details"][0]["loc"] == ["email"]
    r = client.post(
        "/report/subscribe", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 400
    # HTMX callers get a fragment
    r = client.post("/report/subscribe", data={"email": "nope"}, headers={"HX-Request": "true"})
    assert r.status_code == 422 and 'class="notice notice-error"' in r.text


def test_unhandled_exceptions_never_leak_internals(app, client):
    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internals: db password hunter2")

    r = client.get("/boom", headers={"Accept": "application/json"})
    assert r.status_code == 500
    assert "hunter2" not in r.text and "Traceback" not in r.text
    assert r.json()["error"].startswith("internal error") and r.headers["X-Request-Id"]
    r = client.get("/boom", headers={"Accept": "text/html"})
    assert r.status_code == 500 and "text/html" in r.headers["content-type"]
    assert "hunter2" not in r.text and "Internal error" in r.text


def test_static_files_and_request_ids(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]
    r = client.get("/", headers={"X-Request-Id": "req-abc"})
    assert r.status_code == 200 and r.headers["X-Request-Id"] == "req-abc"
    assert client.get("/").headers["X-Request-Id"]
