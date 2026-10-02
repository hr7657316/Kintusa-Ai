"""Live end-to-end smoke test: the real API and worker processes, driven over HTTP the way
GitHub, the CLI and the SDK drive them. No test fakes.

    uv run python scripts/smoke_e2e.py            # needs Postgres (TEST_DATABASE_URL or the dev default)

What it does, on a throwaway database:
 1. migrations, then API (uvicorn) and worker as separate processes
 2. a signed GitHub `installation.created` webhook creates the org and its repos
 3. dev sign-in, then an SDK key from POST /v1/keys
 4. the real `kintsua watch` CLI snapshots a local MCP server and pushes it; then the
    server ships a breaking change and the CLI pushes again (exit 1 = the gate fired)
 5. the worker classifies it with the rules and matches it to the repo: a draft issue
 6. the real SDK wraps a live MCP session (schema changes three times) and an httpx
    client hitting an endpoint that returns 500 with a secret in the body
 7. ingest stores runtime events (secret redacted); the worker discovers the runtime
    dependencies and promotes the undocumented drift to its own change and impact
 8. the public weekly report leaks no customer repo or source; per-repo data deletion works
Exits non-zero on the first failed check. Steps needing a real GitHub App (cloning,
posting issues, fix PRs) are covered by the test suite with in-memory fakes instead.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SERVER = ROOT / "tests" / "fixtures" / "watchers" / "mcp_server.py"
INSTALL_PAYLOAD = ROOT / "tests" / "api" / "fixtures" / "installation_created.json"
MAINT_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:dev@localhost:5432/postgres"
)
WEBHOOK_SECRET = "smoke-webhook-secret"
WORKER_KINDS = "classify_event,match_event,open_issue,match_runtime,weekly_report"

failures = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global failures
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""), flush=True)
    if not cond:
        failures += 1
        raise SystemExit(f"smoke test failed at: {label}")


def wait_for(fn, timeout: float, every: float = 1.0):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(every)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def plain(url: str) -> str:
    return url.replace("+psycopg", "")


def main() -> int:
    db_name = f"kintsua_smoke_{uuid.uuid4().hex[:8]}"
    base_db = MAINT_URL.rsplit("/", 1)[0]
    db_url = f"{base_db}/{db_name}"
    tmp = Path(tempfile.mkdtemp(prefix="kintsua-smoke-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    install = json.loads(INSTALL_PAYLOAD.read_text())
    inst_id = install["installation"]["id"]
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "ENV": "dev",
        "BASE_URL": base,
        "PORT": str(port),
        "LLM_PROVIDER": "fake",
        "GITHUB_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "SESSION_SECRET": "smoke-" + "s" * 40,
        "DEV_LOGIN_INSTALLATION_ID": str(inst_id),
        "DATA_DIR": str(tmp / "data"),
        "AUTO_ISSUE": "false",
        "LOG_LEVEL": "INFO",
    }
    procs: list[subprocess.Popen] = []
    logs = {}

    with psycopg.connect(plain(MAINT_URL), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{db_name}"')
    try:
        print("1. migrations, API and worker")
        r = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        check(r.returncode == 0, "alembic upgrade head", r.stderr.strip().splitlines()[-1])
        for name, cmd in {
            "api": [sys.executable, "-m", "kintsua.api"],
            "worker": [
                sys.executable,
                "-m",
                "kintsua.worker",
                "--no-tick",
                "--kinds",
                WORKER_KINDS,
            ],
        }.items():
            logs[name] = tmp / f"{name}.log"
            procs.append(
                subprocess.Popen(
                    cmd, cwd=ROOT, env=env, stdout=logs[name].open("w"), stderr=subprocess.STDOUT
                )
            )
        http = httpx.Client(base_url=base, timeout=30)
        up = wait_for(lambda: _ok(http, "/healthz"), 60, 0.5)
        check(bool(up), "API /healthz", f"{base}")

        print("2. GitHub installation webhook")
        body = json.dumps(install).encode()
        sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        bad = http.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "installation",
                "X-Hub-Signature-256": "sha256=00",
                "Content-Type": "application/json",
            },
        )
        check(bad.status_code == 401, "unsigned webhook refused", str(bad.status_code))
        r = http.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "installation",
                "X-Hub-Signature-256": sig,
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
        )
        check(r.status_code == 200, "signed installation.created accepted", r.text[:120])
        with psycopg.connect(plain(db_url)) as c:
            repos = dict(c.execute("select full_name, id from repo").fetchall())
            jobs = c.execute("select count(*) from job where kind='index_repo'").fetchone()[0]
        check(
            set(repos) == {r["full_name"] for r in install["repositories"]},
            "org and repos created",
            ", ".join(sorted(repos)),
        )
        check(jobs == len(repos), "index_repo queued per repo (needs a real GitHub App to run)")

        print("3. sign-in and SDK key")
        r = http.get("/auth/dev", follow_redirects=False)
        check(
            r.status_code == 303 and "kintsua_session" in http.cookies, "dev sign-in sets a session"
        )
        r = http.get("/settings")
        check(r.status_code == 200 and "acme/support-agent" in r.text, "settings lists the repos")
        r = http.post("/v1/keys", json={})
        key = r.json().get("key", "")
        check(r.status_code == 200 and key.startswith("kts_"), "SDK key created (shown once)")
        listed = http.get("/v1/keys").json()["keys"][0]
        check(key not in json.dumps(listed), "key list shows only the prefix", listed["prefix"])

        print("4. CLI: snapshot, push, breaking change, push")
        server = f"{sys.executable} {FIXTURE_SERVER}"
        common = [
            "--server",
            server,
            "--name",
            "weather",
            "--dir",
            str(tmp / "kw"),
            "--push",
            "--repo",
            "acme/support-agent",
            "--key",
            key,
            "--url",
            base,
        ]
        cli = [sys.executable, "-m", "kintsua.cli.main", "watch"]
        r = subprocess.run(
            cli + ["snapshot", *common], cwd=tmp, env=env, capture_output=True, text=True
        )
        check(
            r.returncode == 0,
            "kintsua watch snapshot --push",
            r.stdout.strip().splitlines()[-1][:90],
        )
        r = subprocess.run(
            cli + ["diff", *common, "--env", "KINTSUA_MCP_VARIANT=v2"],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
        )
        check(r.returncode == 1, "kintsua watch diff --push exits 1 on the breaking change")
        check("change_event_id" in r.stdout, "server recorded a change event for the push")

        print("5. worker: classify and match")
        support_id = repos["acme/support-agent"]

        def draft():
            ch = http.get(f"/v1/repos/{support_id}/changes").json().get("changes", [])
            return [c for c in ch if c.get("severity") == "breaking"]

        found = wait_for(draft, 60)
        check(
            bool(found),
            "breaking change matched to the repo",
            f"{found[0]['category']}: {found[0]['summary'][:60]}" if found else "",
        )
        check(
            found[0]["status"] == "detected" and found[0]["draft"],
            "held as a draft issue (auto issues off)",
        )
        page = http.get("/settings/acme/support-agent").text
        check(
            "draft" in page.lower() and "Open all drafts" in page, "settings page offers the draft"
        )

        print("6. SDK: live MCP session with schema drift + failing HTTP endpoint")
        sdk_script = tmp / "agent.py"
        sdk_script.write_text(
            textwrap.dedent(f"""
            import asyncio, http.server, os, sys, threading
            import httpx
            import kintsua
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            class H(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(500); self.end_headers()
                    self.wfile.write(b"boom api_key=" + b"sk_li" + b"ve_" + b"a" * 24)
                def log_message(self, *a): pass

            srv = http.server.HTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            client = kintsua.wrap_http(httpx.Client())
            client.get(f"http://127.0.0.1:{{srv.server_port}}/v1/charges/123")

            async def session(variant):
                params = StdioServerParameters(command=sys.executable, args=[{str(FIXTURE_SERVER)!r}],
                                               env={{**os.environ, "KINTSUA_MCP_VARIANT": variant}})
                async with stdio_client(params) as (r, w):
                    async with ClientSession(r, w) as s:
                        s = kintsua.wrap_mcp(s, server="weather-local")
                        await s.initialize()
                        await s.list_tools()
                        await s.call_tool("get_weather", {{"city": "Paris", "units": "metric"}})

            for v in ("v1", "v2", "v1", "v2"):
                asyncio.run(session(v))
            print("flushed", kintsua.flush(10))
        """)
        )
        sdk_env = {
            **env,
            "KINTSUA_KEY": key,
            "KINTSUA_REPO": "acme/billing-bot",
            "KINTSUA_URL": base,
            "KINTSUA_STATE_DIR": str(tmp / "fp"),
        }
        r = subprocess.run(
            [sys.executable, str(sdk_script)],
            cwd=ROOT,
            env=sdk_env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        check(
            r.returncode == 0,
            "agent ran with the SDK wrapped in",
            (r.stdout + r.stderr).strip()[-90:],
        )

        print("7. ingest, runtime discovery and promotion")
        billing_id = repos["acme/billing-bot"]

        def runtime_rows():
            with psycopg.connect(plain(db_url)) as c:
                return c.execute(
                    "select tool_name, outcome, sample from runtime_event where repo_id=%s",
                    (billing_id,),
                ).fetchall()

        rows = wait_for(runtime_rows, 30)
        outcomes = sorted({o for _, o, _ in rows})
        check(
            {"ok", "error", "schema_mismatch"} <= set(outcomes),
            "runtime events stored",
            ", ".join(outcomes),
        )
        mism = sum(1 for _, o, _ in rows if o == "schema_mismatch")
        check(mism >= 3, "schema changes seen at runtime", f"{mism} mismatches")
        http_err = [s for t, o, s in rows if o == "error" and t.startswith("GET ")]
        blob = json.dumps(http_err)
        check(
            bool(http_err) and "aaaaaaaaaaaaaaaaaaaaaaaa" not in blob,
            "HTTP error recorded, secret redacted",
            http_err[0].get("body_excerpt", "")[:40] if http_err else "",
        )

        def promoted():
            ch = http.get(f"/v1/repos/{billing_id}/changes").json().get("changes", [])
            return [c for c in ch if c.get("source_kind") == "observed"]

        obs = wait_for(promoted, 120, 2)
        check(
            bool(obs),
            "undocumented drift promoted to a breaking change",
            obs[0]["summary"][:70] if obs else "",
        )
        deps = http.get(f"/v1/repos/{billing_id}/dependencies").json()
        check(
            deps["totals"]["runtime_discovered"] >= 1,
            "runtime-discovered dependencies on the map",
            f"{deps['totals']['runtime_discovered']} discovered",
        )

        print("8. public report and data deletion")
        md = http.get("/report/weekly", params={"format": "md"}).text
        check(
            "acme" not in md and "weather-local" not in md and "push:" not in md,
            "public weekly report shows no customer repo or source",
        )
        r = http.delete(f"/v1/repos/{billing_id}/data")
        check(r.status_code == 200, "DELETE /v1/repos/{id}/data", json.dumps(r.json())[:90])
        with psycopg.connect(plain(db_url)) as c:
            left = c.execute(
                "select count(*) from runtime_event where repo_id=%s", (billing_id,)
            ).fetchone()[0]
        check(left == 0, "runtime events deleted")

        print("9. process health")
        for p in procs:
            check(p.poll() is None, f"{'api' if p is procs[0] else 'worker'} still running")
        worker_log = logs["worker"].read_text()
        check("Traceback" not in worker_log, "worker log has no tracebacks")
        with psycopg.connect(plain(db_url)) as c:
            failed = c.execute(
                "select kind, last_error from job where status='failed' and kind <> 'index_repo'"
            ).fetchall()
        check(not failed, "no failed jobs", "; ".join(f"{k}: {e[:60]}" for k, e in failed))
        print(f"\nALL CHECKS PASSED  (logs: {tmp})")
        return 0
    except SystemExit as e:
        print(f"\n{e}\nlogs kept in {tmp}")
        for name, path in logs.items():
            print(f"--- tail of {name} log ---")
            print("\n".join(path.read_text().splitlines()[-25:]))
        return 1
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        with psycopg.connect(plain(MAINT_URL), autocommit=True) as c:
            c.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname=%s",
                (db_name,),
            )
            c.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


def _ok(http: httpx.Client, path: str) -> bool:
    try:
        return http.get(path).status_code == 200
    except httpx.HTTPError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
