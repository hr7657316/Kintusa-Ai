"""Observe MCP sessions and HTTP clients unchanged; an SDK failure is a DEBUG log, never raised."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import shlex
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kintsua.sdk.report import Opt, Reporter, get_reporter

log = logging.getLogger("kintsua.sdk")
_STATE_LOCK = threading.Lock()
_ID_SEG = re.compile(r"\d+|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{24,}", re.I)
# path segments that carry a credential (webhook / bot tokens): high-entropy, never a word
_TOKEN_SEG = re.compile(r"bot\d+:[\w-]+|(?=[\w.-]*[A-Z\d])[\w.-]{20,}|(?=[A-Z]*\d)[A-Z\d]{8,}")
# A superset of kintsua.core.util.SECRET_PATTERNS, copied (the SDK must not import the core)
SECRET_RE = re.compile(
    r"(sk|rk|pk)_(live|test)_[0-9a-z]{8,}|bearer\s+[\w\-.=]{12,}|gh[pousr]_[0-9a-z]{20,}"
    r"|basic\s+(?=[a-z]*[\d+/=])[\w+/=]{8,}|github_pat_\w{22,}|xox[abeprs]-[0-9a-z\-]{10,}"
    r"|xapp-[0-9a-z\-]{10,}|AKIA[0-9A-Z]{16}|AIza[\w\-]{35}|sk-ant-[\w\-]{20,}|kts_[\w\-]{20,}"
    r"|sk-(proj|svcacct|admin)-[\w\-]{20,}|sk-[0-9a-z]{20,}|npm_[0-9a-z]{36}|pypi-[\w\-]{50,}"
    r"|(api[_-]?key|token|secret|password|private[_-]?key|(proxy-)?authorization)[\"']?\s*[:=]\s*"
    r"[\"']?([a-z]+\s+)?[^\s\"',}]{6,}|eyJ[\w\-]{10,}\.[\w\-]{10,}\.[\w\-]{10,}"
    r"|(?<=://)[^/\s:@]+:[^/\s@]+(?=@)",  # URL userinfo (user:password@)
    re.I,
)
_CACHES: dict[str, dict[str, str]] = {}  # server -> {tool: fingerprint}, shared by its sessions
Done = Callable[[tuple, dict, float, Any, BaseException | None], None]
pick_name = lambda a, kw: (a[0] if a else kw.get("name"), (a[1:] or [kw.get("arguments")])[0])  # noqa: E731


def fingerprint(input_schema: Any) -> str:
    """SHA-256 of the canonical JSON of an ``inputSchema`` (== watchers.mcp.tool_fingerprint)."""
    s = json.dumps(input_schema or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def schema_diff(before: Any, after: Any, ptr: str = "") -> list[dict[str, Any]]:
    """Small JSON-Schema diff at ``<ptr>/properties/<name>`` (the watcher's pointer dialect)."""
    b_, a_ = (x if isinstance(x, dict) else {} for x in (before, after))
    bp, ap = b_.get("properties") or {}, a_.get("properties") or {}
    ops: list[dict[str, Any]] = []
    for k in sorted(set(bp) | set(ap)):
        p, b, a = f"{ptr}/properties/{k}", bp.get(k), ap.get(k)
        if k not in ap or k not in bp:
            op = "added" if k in ap else "removed"
            ops.append({"op": op, "path": p, "before": b, "after": a})
        elif isinstance(b, dict) and isinstance(a, dict):
            for key, op in (("type", "type_changed"), ("enum", "enum_changed")):
                if b.get(key) != a.get(key):
                    ops.append({"op": op, "path": p, "before": b.get(key), "after": a.get(key)})
            ops.extend(schema_diff(b, a, p))
    br, ar = set(b_.get("required") or []), set(a_.get("required") or [])
    for k in sorted(ar - br):
        ops.append({"op": "required_added", "path": f"{ptr}/properties/{k}", "after": k})
    for k in sorted(br - ar):
        ops.append({"op": "required_removed", "path": f"{ptr}/properties/{k}", "before": k})
    return ops


def tools_of(result: Any) -> list[tuple[str, dict[str, Any]]]:
    """``[(name, inputSchema)]`` from a ListToolsResult (mcp 1.x/2.x), Tool models or dicts."""
    tools = result.get("tools") if isinstance(result, dict) else getattr(result, "tools", result)
    out: list[tuple[str, dict[str, Any]]] = []
    for t in tools or []:
        t = getattr(t, "_mcp_tool", t)  # google-adk MCPTool keeps the wire tool here
        if hasattr(t, "model_dump"):
            t = t.model_dump(by_alias=True, mode="json", exclude_none=True)
        elif not isinstance(t, dict):
            t = {"name": getattr(t, "name", None), "inputSchema": getattr(t, "inputSchema", None)}
        if t.get("name"):
            out.append((str(t["name"]), t.get("inputSchema") or t.get("input_schema") or {}))
    return out


def check_tools(reporter: Reporter, cache: dict[str, str], result: Any, server: str) -> None:
    """Fingerprint the listed tools against the local store; a moved one emits ``schema_mismatch``."""
    base = Path(os.environ.get("KINTSUA_STATE_DIR") or Path.home() / ".kintsua" / "fingerprints")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", reporter.repo).strip("-.") or "default"
    path = base / f"{slug}.json"
    with _STATE_LOCK:
        try:
            state = json.loads(path.read_text("utf-8"))
        except Exception:  # noqa: BLE001 - missing or corrupt: start fresh
            state = {}
        changed = False
        for name, schema in tools_of(result):
            fp = cache[name] = fingerprint(schema)
            prev = state.get(f"{server}::{name}")
            if prev is not None and prev.get("fingerprint") != fp:
                diff = schema_diff(prev.get("schema"), schema, f"/tools/{name}/inputSchema")
                smp = {"diff": diff, "server": server or None}
                reporter.record(name, "mcp", "schema_mismatch", schema_fingerprint=fp, sample=smp)
            if prev is None or prev.get("fingerprint") != fp:
                state[f"{server}::{name}"], changed = {"fingerprint": fp, "schema": schema}, True
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.with_suffix(".tmp").write_text(json.dumps(state, sort_keys=True), "utf-8")
            os.replace(path.with_suffix(".tmp"), path)


def observe(orig: Callable[..., Any], on_done: Done) -> Callable[..., Any]:
    """Run ``on_done(args, kwargs, t0, result, exc)`` after a sync/async call; it never raises."""

    def done(a: tuple, kw: dict, t0: float, res: Any, exc: BaseException | None) -> None:
        try:
            on_done(a, kw, t0, res, exc)
        except Exception as e:  # noqa: BLE001
            log.debug("kintsua: observer failed: %s", e)

    async def awaited(aw: Any, *ctx: Any) -> Any:  # ctx: (args, kwargs, t0)
        try:
            res = await aw
        except BaseException as e:
            done(*ctx, None, e)
            raise
        done(*ctx, res, None)
        return res

    def wrapper(*a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            res = orig(*a, **kw)
        except BaseException as e:
            done(a, kw, t0, None, e)
            raise
        if inspect.isawaitable(res):  # a coroutine is observed, other awaitables keep their type
            return awaited(res, a, kw, t0) if inspect.iscoroutine(res) else res
        done(a, kw, t0, res, None)
        return res

    return wrapper


def call_observer(reporter: Reporter, cache: Any, sample: bool, name_of: Any = pick_name) -> Done:
    """``on_done`` for ``call_tool``: name, latency, outcome, error class, ``sample`` arg keys."""

    def done(a: tuple, kw: dict, t0: float, res: Any, exc: BaseException | None) -> None:
        name, args = name_of(a, kw)
        flag = res.get("isError") if isinstance(res, dict) else getattr(res, "is_error", None)
        flag = getattr(res, "isError", None) if flag is None else flag
        error = type(exc).__name__ if exc else ("ToolError" if flag else None)
        keys = {"arg_keys": sorted(map(str, args))} if sample and isinstance(args, dict) else None
        fps = cache(a) if callable(cache) else cache  # cache(args): the called server's dict
        fields = {"error_class": error, "schema_fingerprint": fps.get(str(name)), "sample": keys}
        fields["latency_ms"] = (time.perf_counter() - t0) * 1000
        reporter.record(str(name or "?"), "mcp", "error" if error else "ok", **fields)

    return done


def _server_name(res: Any, srv: list[str]) -> None:  # initialize observer: serverInfo.name
    si = res.get("serverInfo") if isinstance(res, dict) else getattr(res, "server_info", None)
    si = getattr(res, "serverInfo", None) if si is None else si
    name = si.get("name") if isinstance(si, dict) else getattr(si, "name", None)
    srv[0] = srv[0] or (f"mcp:{name}" if name else "")


def server_id(params: Any, label: str = "") -> str:
    """``sample.server``: URL (no userinfo/query) or command line in ``params``, else ``label``."""
    get = params.get if isinstance(params, dict) else lambda k: getattr(params, k, None)
    url, cmd = str(get("url") or ""), get("command")
    if "://" in url:  # parsed by hand: never raises, whatever the framework holds
        scheme, _, rest = url.partition("://")
        host, slash, path = re.split(r"[?#]", rest)[0].partition("/")
        return f"{scheme}://{host.rpartition('@')[2]}{slash}{path}"
    line = shlex.join([str(cmd), *map(str, get("args") or [])]) if cmd else label
    return SECRET_RE.sub("[REDACTED]", line)


def wrap_mcp(session: Any, repo: Opt = None, key: Opt = None, sample=False, server: Any = None):
    """Observe initialize / list_tools / call_tool of an MCP session; ``server``: str or params."""
    try:
        rep = get_reporter(repo, key)
        if not rep.enabled or getattr(session, "_kintsua_wrapped", False):
            return session
        p = server if isinstance(server, dict) else {"url": server or getattr(session, "url", None)}
        srv = [server_id(p, server if isinstance(server, str) else "")]
        cache = _CACHES.setdefault(srv[0], {}) if srv[0] else {}
        observers: dict[str, Done] = {
            "initialize": lambda a, kw, t0, res, exc: _server_name(res, srv),
            "list_tools": lambda a, kw, t0, res, exc: exc or check_tools(rep, cache, res, srv[0]),
            "call_tool": call_observer(rep, cache, sample or rep.sample),
        }
        for attr, on_done in observers.items():
            if callable(getattr(session, attr, None)):
                setattr(session, attr, observe(getattr(session, attr), on_done))
        session._kintsua_wrapped = True
    except Exception as e:  # noqa: BLE001
        log.debug("kintsua: wrap_mcp skipped: %s", e)
    return session


def http_tool_name(method: str, url: str) -> str:
    """``GET api.vendor.com/v1/users/{id}``: ids -> ``{id}``, credentials -> ``{token}``, no query."""
    u = urlsplit(str(url))
    seg = lambda s: "{id}" if _ID_SEG.fullmatch(s) else "{token}" if _TOKEN_SEG.fullmatch(s) else s  # noqa: E731
    path = "/".join(map(seg, u.path.split("/")))
    return f"{str(method).upper()} {(u.hostname or '').lower()}{path or '/'}"


def http_observer(reporter: Reporter, name_of: Any) -> Done:
    """``on_done`` for an HTTP send: ok below 400, else ``http_<status>`` + a redacted excerpt."""

    def done(a: tuple, kw: dict, t0: float, res: Any, exc: BaseException | None) -> None:
        method, url = name_of(a, kw)
        status, error, sample = getattr(res, "status_code", None), None, None
        if exc is not None:
            error = type(exc).__name__
        elif isinstance(status, int) and status >= 400:
            try:
                body = b"" if kw.get("stream") else bytes(res.content)
            except Exception:  # noqa: BLE001 - streaming response not read: no excerpt
                body = b""
            excerpt = SECRET_RE.sub("[REDACTED]", body[:512].decode("utf-8", "replace"))
            error, sample = f"http_{status}", {"status": status, "body_excerpt": excerpt}
        ms = (time.perf_counter() - t0) * 1000
        name, outcome = http_tool_name(method, url), "error" if error else "ok"
        reporter.record(name, "http", outcome, error_class=error, latency_ms=ms, sample=sample)

    return done


def wrap_http(client: Any, repo: Opt = None, key: Opt = None) -> Any:
    """Observe an httpx Client/AsyncClient (``send``) or a requests Session (``request``) only."""
    try:
        reporter = get_reporter(repo, key)
        if not reporter.enabled or getattr(client, "_kintsua_wrapped", False):
            return client
        if hasattr(client, "event_hooks") and callable(getattr(client, "send", None)):
            by_req = lambda a, kw: ((r := a[0] if a else kw["request"]).method, str(r.url))  # noqa: E731
            client.send = observe(client.send, http_observer(reporter, by_req))
        elif hasattr(client, "mount") and hasattr(client, "adapters"):  # requests.Session
            by_args = lambda a, kw: (a[0], a[1])  # noqa: E731 - request(method, url, ...)
            client.request = observe(client.request, http_observer(reporter, by_args))
        else:
            return client
        client._kintsua_wrapped = True
    except Exception as e:  # noqa: BLE001
        log.debug("kintsua: wrap_http skipped: %s", e)
    return client
