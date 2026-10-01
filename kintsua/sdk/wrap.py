"""Wrappers that observe MCP sessions and HTTP clients without changing their behaviour.
Best-effort: a failure inside the SDK is logged at DEBUG and the wrapped call proceeds."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kintsua.sdk.report import Reporter, get_reporter

log = logging.getLogger("kintsua.sdk")
_STATE_LOCK = threading.Lock()
_ID_SEG = re.compile(r"\d+|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{24,}", re.I)
# kintsua.core.util.SECRET_PATTERNS, copied (the SDK must not import the hosted core) and
# extended to match JSON-quoted keys ("api_key": "...").
SECRET_RE = re.compile(
    r"(sk|rk|pk)_(live|test)_[0-9a-z]{8,}|bearer\s+[a-z0-9\-_.=]{12,}|gh[pousr]_[0-9a-z]{20,}"
    r"|xox[baprs]-[0-9a-z\-]{10,}|AKIA[0-9A-Z]{16}|sk-ant-[0-9a-z\-_]{20,}|sk-[0-9a-z]{20,}"
    r"|(api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?(bearer\s+)?[^\s\"',}]{6,}"
    r"|eyJ[0-9a-z_\-]{10,}\.[0-9a-z_\-]{10,}\.[0-9a-z_\-]{10,}",
    re.I,
)
Opt = str | None
Done = Callable[[tuple, dict, float, Any, BaseException | None], None]


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
            ops.append(
                {"op": "added" if k in ap else "removed", "path": p, "before": b, "after": a}
            )
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
    tools = getattr(result, "tools", None)
    if tools is None:
        tools = result.get("tools") if isinstance(result, dict) else result
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

    async def awaited(aw: Any, done: Callable[[Any, BaseException | None], None]) -> Any:
        try:
            res = await aw
        except BaseException as e:
            done(None, e)
            raise
        done(res, None)
        return res

    def wrapper(*a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()

        def done(res: Any, exc: BaseException | None) -> None:
            try:
                on_done(a, kw, t0, res, exc)
            except Exception as e:  # noqa: BLE001
                log.debug("kintsua: observer failed: %s", e)

        try:
            res = orig(*a, **kw)
        except BaseException as e:
            done(None, e)
            raise
        if inspect.isawaitable(res):
            return awaited(res, done)
        done(res, None)
        return res

    return wrapper


def _pick_name(a: tuple, kw: dict) -> tuple[Any, Any]:
    return (a[0] if a else kw.get("name")), (a[1] if len(a) > 1 else kw.get("arguments"))


def call_observer(reporter: Reporter, cache: dict, sample: bool, name_of: Any = _pick_name) -> Done:
    """``on_done`` for ``call_tool``: name, latency, ok/error, error class; arg keys if ``sample``."""

    def done(a: tuple, kw: dict, t0: float, res: Any, exc: BaseException | None) -> None:
        name, args = name_of(a, kw)
        flag = res.get("isError") if isinstance(res, dict) else getattr(res, "is_error", None)
        flag = getattr(res, "isError", None) if flag is None else flag
        error = type(exc).__name__ if exc else ("ToolError" if flag else None)
        keys = {"arg_keys": sorted(map(str, args))} if sample and isinstance(args, dict) else None
        fields = {"error_class": error, "schema_fingerprint": cache.get(str(name)), "sample": keys}
        fields["latency_ms"] = (time.perf_counter() - t0) * 1000
        reporter.record(str(name or "?"), "mcp", "error" if error else "ok", **fields)

    return done


def _server_name(res: Any, srv: list[str]) -> None:  # initialize observer: serverInfo.name
    si = res.get("serverInfo") if isinstance(res, dict) else getattr(res, "server_info", None)
    si = getattr(res, "serverInfo", None) if si is None else si
    name = si.get("name") if isinstance(si, dict) else getattr(si, "name", None)
    srv[0] = srv[0] or (f"mcp:{name}" if name else "")


def wrap_mcp(session: Any, repo: Opt = None, key: Opt = None, sample=False, server: Opt = None):
    """Patch ``initialize`` / ``list_tools`` / ``call_tool`` on an MCP client session (mcp 1.x/2.x
    or any sync/async duck type), return the same object; ``server`` names the locator."""
    try:
        rep = get_reporter(repo, key)
        if not rep.enabled or getattr(session, "_kintsua_wrapped", False):
            return session
        cache: dict[str, str] = {}
        srv = [str(server or getattr(session, "url", None) or "")]
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
    """``GET api.vendor.com/v1/users/{id}``: numeric/uuid/long-hex segments -> ``{id}``, no query."""
    u = urlsplit(str(url))
    path = "/".join("{id}" if _ID_SEG.fullmatch(s) else s for s in u.path.split("/"))
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


def _httpx_name(a: tuple, kw: dict) -> tuple[str, str]:
    r = a[0] if a else kw["request"]  # Client.stream() passes request= by keyword
    return r.method, str(r.url)


def wrap_http(client: Any, repo: Opt = None, key: Opt = None) -> Any:
    """Observe an httpx Client/AsyncClient (``send``) or a requests Session (``request``)."""
    try:
        reporter = get_reporter(repo, key)
        if not reporter.enabled or getattr(client, "_kintsua_wrapped", False):
            return client
        if hasattr(client, "event_hooks") and callable(getattr(client, "send", None)):
            client.send = observe(client.send, http_observer(reporter, _httpx_name))
        elif callable(getattr(client, "request", None)):
            by_args = lambda a, kw: (a[0], a[1])  # noqa: E731 - request(method, url, ...)
            client.request = observe(client.request, http_observer(reporter, by_args))
        else:
            return client
        client._kintsua_wrapped = True
    except Exception as e:  # noqa: BLE001
        log.debug("kintsua: wrap_http skipped: %s", e)
    return client
