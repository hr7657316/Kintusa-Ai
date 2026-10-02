"""``kintsua watch`` over HTTP: --spec (OpenAPI, respx-mocked), --push (the CONTRACTS payload,
respx-mocked), and an MCP server that answers 401 (a local http.server) -> exit 3 + hint."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from kintsua.cli.main import app

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "watchers"
SPEC_URL = "https://api.vendor.test/openapi.json"
PUSH_URL = "https://api.test/v1/watch/push"
runner = CliRunner()
pytestmark = pytest.mark.timeout(120)


def spec(name: str) -> httpx.Response:
    return httpx.Response(200, text=(FIX / name).read_text("utf-8"))


@respx.mock
def test_spec_snapshot_and_diff(tmp_path):
    route = respx.get(SPEC_URL).mock(return_value=spec("openapi3_before.json"))
    d = tmp_path / ".kintsua"
    res = runner.invoke(
        app, ["watch", "snapshot", "--spec", SPEC_URL, "--dir", str(d)], catch_exceptions=False
    )
    assert res.exit_code == 0, res.output
    assert "operations snapshotted" in res.stdout
    lock = json.loads((d / "api.vendor.test.lock.json").read_text())
    assert lock["kind"] == "openapi" and lock["display_name"] == "Pets API"
    assert "GET /v1/pets" in lock["fingerprints"] and lock["version"] == "1.0.0"
    route.mock(return_value=spec("openapi3_after.json"))
    res = runner.invoke(
        app, ["watch", "diff", "--spec", SPEC_URL, "--dir", str(d)], catch_exceptions=False
    )
    assert res.exit_code == 1, res.output
    assert "GET /v1/legacy/export" in res.stdout and "removed_operation" in res.stdout
    assert "Pets API" in res.stdout and "1.0.0 -> 2.0.0" in res.stdout
    row = json.loads((d / "history.jsonl").read_text().splitlines()[0])
    assert row["source_kind"] == "openapi" and row["classification"]["severity"] == "breaking"
    assert "GET /v1/legacy/export" in row["classification"]["affected_paths"]


@respx.mock
def test_spec_fetch_failure_exits_three(tmp_path):
    respx.get(SPEC_URL).mock(return_value=httpx.Response(503, text="down"))
    res = runner.invoke(
        app,
        ["watch", "snapshot", "--spec", SPEC_URL, "--dir", str(tmp_path)],
        catch_exceptions=False,
    )
    assert res.exit_code == 3 and "HTTP 503" in res.stderr


@respx.mock
def test_push_posts_the_contract_payload(tmp_path):
    respx.get(SPEC_URL).mock(return_value=spec("openapi3_before.json"))
    push = respx.post(PUSH_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "change_event_id": "ce-1", "nested": {"x": 1}}
        )
    )
    d = tmp_path / ".kintsua"
    args = [
        "--spec",
        SPEC_URL,
        "--dir",
        str(d),
        "--push",
        "--repo",
        "acme/agent",
        "--key",
        "kts_secret",
        "--url",
        "https://api.test",
    ]
    res = runner.invoke(app, ["watch", "snapshot", *args], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert push.call_count == 1
    assert "pushed Pets API to https://api.test" in res.stdout
    assert "status=ok" in res.stdout and "change_event_id=ce-1" in res.stdout
    req = push.calls[0].request
    assert req.headers["Authorization"] == "Bearer kts_secret"
    body = json.loads(req.content)
    assert set(body) == {"repo", "source", "doc"} and body["repo"] == "acme/agent"
    assert body["source"] == {"kind": "openapi", "locator": SPEC_URL, "display_name": "Pets API"}
    assert set(body["doc"]) == {"version", "body"} and body["doc"]["version"] == "1.0.0"
    assert "paths" in body["doc"]["body"]
    # diff --push sends the fresh document too (unchanged or not)
    res = runner.invoke(app, ["watch", "diff", *args], catch_exceptions=False)
    assert res.exit_code == 0 and push.call_count == 2


@respx.mock
def test_push_failures_are_warnings_not_errors(tmp_path):
    respx.get(SPEC_URL).mock(return_value=spec("openapi3_before.json"))
    push = respx.post(PUSH_URL).mock(return_value=httpx.Response(401, text="bad key"))
    d = tmp_path / ".kintsua"
    base = [
        "watch",
        "snapshot",
        "--spec",
        SPEC_URL,
        "--dir",
        str(d),
        "--push",
        "--url",
        "https://api.test",
    ]
    res = runner.invoke(
        app, [*base, "--repo", "acme/agent", "--key", "nope"], catch_exceptions=False
    )
    assert res.exit_code == 0 and "warning" in res.stderr and "HTTP 401" in res.stderr
    res = runner.invoke(app, base, catch_exceptions=False)  # no key: skipped, still exit 0
    assert res.exit_code == 0 and "--push skipped" in res.stderr and push.call_count == 1
    push.mock(side_effect=httpx.ConnectError("down"))
    res = runner.invoke(app, [*base, "--repo", "acme/agent", "--key", "k"], catch_exceptions=False)
    assert res.exit_code == 0 and "push to https://api.test/v1/watch/push failed" in res.stderr


def test_auth_required_mcp_server_exits_three_with_a_hint(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="mcp"')
            self.end_headers()
            self.wfile.write(b"unauthorized")

        do_GET = do_POST

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        res = runner.invoke(
            app,
            [
                "watch",
                "snapshot",
                "--server",
                f"http://127.0.0.1:{srv.server_port}/mcp",
                "--dir",
                str(tmp_path),
                "--timeout",
                "10",
            ],
            catch_exceptions=False,
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert res.exit_code == 3, res.output
    assert "401" in res.stderr and "--auth-env" in res.stderr and "could not fetch" in res.stderr
    rows = [json.loads(line) for line in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert rows[0]["error"] and "401" in rows[0]["error"]


@respx.mock
def test_auth_env_is_sent_for_a_spec(tmp_path, monkeypatch):
    authed = respx.get(SPEC_URL, headers={"Authorization": "Bearer s3cret"}).mock(
        return_value=spec("openapi3_before.json")
    )
    respx.get(SPEC_URL).mock(return_value=httpx.Response(401, text="who are you"))
    monkeypatch.setenv("SPEC_TOKEN", "s3cret")
    d = tmp_path / ".kintsua"
    args = ["--spec", SPEC_URL, "--auth-env", "SPEC_TOKEN", "--dir", str(d)]
    res = runner.invoke(app, ["watch", "snapshot", *args], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert authed.call_count == 1
    assert "s3cret" not in (d / "api.vendor.test.lock.json").read_text()
    res = runner.invoke(app, ["watch", "diff", *args], catch_exceptions=False)
    assert res.exit_code == 0 and authed.call_count == 2
    monkeypatch.delenv("SPEC_TOKEN")
    res = runner.invoke(app, ["watch", "diff", *args], catch_exceptions=False)
    assert res.exit_code == 3 and "SPEC_TOKEN is not set" in res.stderr


SPEC_V2_URL = "https://api.vendor.test/v2/openapi.json"


@respx.mock
def test_two_sources_on_one_host_never_share_a_lockfile(tmp_path):
    v1 = respx.get(SPEC_URL).mock(return_value=spec("openapi3_before.json"))
    v2 = respx.get(SPEC_V2_URL).mock(return_value=spec("openapi3_after.json"))
    d = tmp_path / ".kintsua"

    def run(cmd: str, url: str, *extra: str):
        argv = ["watch", cmd, "--spec", url, "--dir", str(d), *extra]
        return runner.invoke(app, argv, catch_exceptions=False)

    assert run("snapshot", SPEC_URL).exit_code == 0
    first = (d / "api.vendor.test.lock.json").read_text()
    res = run("diff", SPEC_V2_URL)  # same host, another spec: not compared with the first one
    assert res.exit_code == 4 and "no lockfile" in res.stderr, res.output
    assert run("snapshot", SPEC_V2_URL).exit_code == 0
    assert (d / "api.vendor.test.lock.json").read_text() == first  # not overwritten
    assert len(list(d.glob("*.lock.json"))) == 2
    assert run("diff", SPEC_URL).exit_code == 0 and run("diff", SPEC_V2_URL).exit_code == 0
    v2.mock(return_value=spec("openapi3_before.json"))  # only the second spec changes
    assert run("diff", SPEC_V2_URL).exit_code == 1
    assert run("diff", SPEC_URL).exit_code == 0 and v1.call_count >= 3
    # an explicit --name shared by two sources is a usage error for diff, a warning for snapshot
    assert run("snapshot", SPEC_URL, "--name", "shared").exit_code == 0
    res = run("diff", SPEC_V2_URL, "--name", "shared")
    assert res.exit_code == 2 and f"was written for openapi {SPEC_URL}" in res.stderr
    assert "--name" in res.stderr
    res = run("snapshot", SPEC_V2_URL, "--name", "shared")
    assert res.exit_code == 0 and "overwriting" in res.stderr


@respx.mock
def test_push_redirect_is_a_warning_not_a_success(tmp_path):
    respx.get(SPEC_URL).mock(return_value=spec("openapi3_before.json"))
    push = respx.post("http://api.test/v1/watch/push").mock(
        return_value=httpx.Response(301, headers={"Location": PUSH_URL})
    )
    argv = ["watch", "snapshot", "--spec", SPEC_URL, "--dir", str(tmp_path), "--push"]
    argv += ["--repo", "acme/agent", "--key", "kts_k", "--url", "http://api.test"]
    res = runner.invoke(app, argv, catch_exceptions=False)
    assert res.exit_code == 0 and push.call_count == 1
    assert "pushed" not in res.stdout
    assert "HTTP 301 redirect to https://api.test/v1/watch/push" in res.stderr
