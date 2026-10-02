"""Compatibility layer over the official ``mcp`` Python package (1.x and 2.x).

The 2.x client renamed the streamable-HTTP transport (``streamable_http_client``, which
takes an ``httpx2.AsyncClient``) and the server class (``MCPServer`` instead of
``FastMCP``), and switched the Pydantic models to snake_case attributes with camelCase
wire aliases. Everything in this module is written against the *wire* representation
(``model_dump(by_alias=True)``) so the rest of the watcher never cares which version
is installed.

Public pieces:

* ``open_session(locator, meta)`` - async context manager yielding an initialised
  ``ClientSession`` for an ``http(s)://`` locator (streamable HTTP, or SSE when
  ``meta["transport"] == "sse"``) or a stdio command line (``npx -y pkg ...``,
  ``uvx pkg``, ``python -m x``). Honours ``meta["headers"]``, ``meta["auth_env"]``
  (``Authorization: Bearer $VAR``), ``meta["args"]``, ``meta["env"]``, ``meta["cwd"]`` and
  ``meta["timeout_s"]`` (connect timeout, default 60 s). A source the hosted worker marks
  ``meta["_net_guard"]`` (not operator-seeded) must be an http(s) URL to a public address:
  ``kintsua.core.netguard`` checks it before connecting and on every request/redirect.
* ``list_tools_all`` / ``list_resources_all`` / ``list_prompts_all`` - paginate (at most
  ``MAX_LIST_PAGES`` pages / ``MAX_LIST_ITEMS`` items, else ``MCPConnectError``).
* ``to_wire(model)`` - JSON-compatible dict in wire (camelCase) form.
* ``MCPServer`` - the server class for tests/fixtures (``.tool()``, ``.run(transport=...)``).
* ``MCPConnectError`` - one exception type with a readable message for every transport
  failure (the SDK raises ``ExceptionGroup``s from its task groups; we flatten them).

Only base dependencies are imported here (mcp, httpx/httpx2, stdlib) so the open-source
CLI can use it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import shlex
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kintsua.core.netguard import async_request_hook, check_url

log = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_S = 60.0
DEFAULT_READ_TIMEOUT_S = 300.0
DEFAULT_USER_AGENT = "kintsua-watcher/0.1 (+https://github.com/hr7657316/Kintusa-Ai)"
MAX_LIST_PAGES = 200
MAX_LIST_ITEMS = 10_000

# --- transport imports: 2.x first, then 1.x names ---------------------------------------
try:  # mcp >= 2
    from mcp.client.streamable_http import streamable_http_client as _streamable_http_client

    MCP_MAJOR = 2
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.client.streamable_http import (  # type: ignore[no-redef,attr-defined]
        streamablehttp_client as _streamable_http_client,
    )

    MCP_MAJOR = 1

try:
    from mcp.client.sse import sse_client as _sse_client
except ImportError:  # pragma: no cover
    _sse_client = None  # type: ignore[assignment]

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

try:
    import httpx2 as _httpx_for_mcp  # mcp 2.x transports take an httpx2 client
except ImportError:  # pragma: no cover - mcp 1.x uses httpx
    import httpx as _httpx_for_mcp  # type: ignore[no-redef]

try:
    from mcp.types import PaginatedRequestParams as _PaginatedRequestParams
except ImportError:  # pragma: no cover
    _PaginatedRequestParams = None  # type: ignore[assignment,misc]

try:
    from mcp.shared.exceptions import MCPError as _MCPError
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.shared.exceptions import McpError as _MCPError  # type: ignore[no-redef]

MCPError = _MCPError

__all__ = [
    "MCP_MAJOR",
    "MCPConnectError",
    "MCPError",
    "MCPServer",
    "ClientSession",
    "StdioServerParameters",
    "open_session",
    "is_http_locator",
    "parse_stdio_locator",
    "inherited_environment",
    "build_http_headers",
    "list_tools_all",
    "list_resources_all",
    "list_prompts_all",
    "to_wire",
    "flatten_exception",
    "describe_exception",
    "error_code",
    "init_result_of",
]


class MCPConnectError(RuntimeError):
    """Raised when a server cannot be reached, authenticated with, or initialised."""


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def to_wire(model: Any) -> Any:
    """Pydantic model -> JSON-compatible dict in wire (camelCase) form; passthrough otherwise."""
    if model is None:
        return None
    dump = getattr(model, "model_dump", None)
    if dump is None:
        return model
    try:
        return dump(by_alias=True, mode="json", exclude_none=True)
    except TypeError:  # pragma: no cover - very old pydantic
        return dump(by_alias=True, exclude_none=True)


def is_http_locator(locator: str) -> bool:
    return bool(re.match(r"^https?://", locator.strip(), re.IGNORECASE))


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: str, *, env: dict[str, str] | None = None) -> str:
    """Expand ``${VAR}`` references from the process environment (missing -> empty)."""
    src = env if env is not None else os.environ

    def repl(m: re.Match[str]) -> str:
        return src.get(m.group(1), "")

    return _ENV_REF.sub(repl, value)


def build_http_headers(meta: dict[str, Any] | None, *, locator: str = "") -> dict[str, str]:
    """User-Agent + ``meta["headers"]`` (``${VAR}`` expanded) + ``Authorization: Bearer``
    from ``meta["auth_env"]``. Raises ``MCPConnectError`` when ``auth_env`` names a variable
    that is not set, so the failure is explicit instead of a cryptic 401."""
    meta = meta or {}
    headers: dict[str, str] = {"User-Agent": str(meta.get("user_agent") or DEFAULT_USER_AGENT)}
    for k, v in (meta.get("headers") or {}).items():
        headers[str(k)] = expand_env(str(v))
    auth_env = meta.get("auth_env")
    if auth_env:
        token = os.environ.get(str(auth_env), "")
        if not token:
            raise MCPConnectError(
                f"environment variable {auth_env} is not set (required to authenticate "
                f"with {locator or 'this MCP server'})"
            )
        scheme = str(meta.get("auth_scheme") or "Bearer")
        headers["Authorization"] = f"{scheme} {token}".strip()
    return headers


# Environment a stdio server child may inherit on top of the SDK's safe defaults (PATH,
# HOME, ...): what ``npx`` / ``uvx`` need to download a package behind a proxy or a private
# CA, plus locale and temp dirs. Never tokens or cloud credentials.
INHERITED_ENV_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "NODE_OPTIONS",
        "DENO_CERT",
        "GLOBAL_AGENT_HTTPS_PROXY",
        "GLOBAL_AGENT_NO_PROXY",
        "YARN_HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
    }
)
INHERITED_ENV_PREFIXES = ("npm_config_", "NPM_CONFIG_", "UV_", "PIP_")


def inherited_environment() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k in INHERITED_ENV_NAMES or k.startswith(INHERITED_ENV_PREFIXES)
    }


def parse_stdio_locator(locator: str, meta: dict[str, Any] | None = None) -> StdioServerParameters:
    """``"npx -y @scope/pkg --flag"`` (+ ``meta["args"]``/``env``/``cwd``) -> server params."""
    meta = meta or {}
    parts = shlex.split(locator.strip()) if locator.strip() else []
    if not parts:
        raise MCPConnectError("empty stdio locator")
    command, args = parts[0], parts[1:]
    extra_args = meta.get("args") or []
    if isinstance(extra_args, str):
        extra_args = shlex.split(extra_args)
    args = [*args, *[str(a) for a in extra_args]]
    env: dict[str, str] | None = inherited_environment()
    if meta.get("env"):
        env = {**env, **{str(k): expand_env(str(v)) for k, v in dict(meta["env"]).items()}}
    auth_env = meta.get("auth_env")
    if auth_env and not is_http_locator(locator):
        # a stdio server wants its token in its own environment
        token = os.environ.get(str(auth_env))
        if token:
            env = {**(env or {}), str(auth_env): token}
    cwd = meta.get("cwd")
    kw: dict[str, Any] = {"command": command, "args": args, "env": env}
    if cwd:
        kw["cwd"] = str(cwd)
    return StdioServerParameters(**kw)


def flatten_exception(exc: BaseException) -> list[BaseException]:
    """Unwrap (nested) ``ExceptionGroup``s into their leaf exceptions."""
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for sub in exc.exceptions:
            out.extend(flatten_exception(sub))
        return out or [exc]
    return [exc]


def describe_exception(exc: BaseException) -> str:
    leaves = flatten_exception(exc)
    parts: list[str] = []
    for leaf in leaves:
        name = type(leaf).__name__
        msg = str(leaf).strip()
        status = _http_status_of(leaf)
        if status:
            msg = f"HTTP {status}" + (f": {msg}" if msg else "")
        if isinstance(leaf, MCPError):
            data = getattr(leaf, "data", None)
            if data:
                msg = f"{msg} ({data})"
        parts.append(f"{name}: {msg}" if msg else name)
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return "; ".join(seen)[:2000]


def error_code(exc: BaseException) -> int | None:
    """The JSON-RPC error code of an ``MCPError`` (2.x ``.code``, 1.x ``.error.code``)."""
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(getattr(exc, "error", None), "code", None)
    return int(code) if isinstance(code, int) else None


def _http_status_of(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return int(code) if isinstance(code, int) else None


def init_result_of(session: ClientSession) -> Any:
    """The ``InitializeResult`` captured by ``open_session`` (wire dict), or None."""
    return getattr(session, "_kintsua_init_result", None)


# --------------------------------------------------------------------------------------
# Opening sessions
# --------------------------------------------------------------------------------------


def _guard_kwargs(meta: dict[str, Any]) -> dict[str, Any]:
    """Guarded sources: check every request (including each redirect hop) with
    ``kintsua.core.netguard``."""
    if meta.get("_net_guard"):
        return {"event_hooks": {"request": [async_request_hook]}}
    return {}


def _guarded_client_factory(
    headers: dict[str, str] | None = None, timeout: Any = None, auth: Any = None
) -> Any:
    """``httpx_client_factory`` for the SSE transport of a guarded source."""
    kw: dict[str, Any] = {"event_hooks": {"request": [async_request_hook]}}
    if timeout is None:
        timeout = _httpx_for_mcp.Timeout(DEFAULT_CONNECT_TIMEOUT_S, read=DEFAULT_READ_TIMEOUT_S)
    kw["timeout"] = timeout
    if headers is not None:
        kw["headers"] = headers
    if auth is not None:
        kw["auth"] = auth
    return _httpx_for_mcp.AsyncClient(**kw)


@asynccontextmanager
async def _http_streams(locator: str, meta: dict[str, Any]) -> AsyncIterator[tuple[Any, Any]]:
    headers = build_http_headers(meta, locator=locator)
    connect_timeout = float(meta.get("timeout_s") or DEFAULT_CONNECT_TIMEOUT_S)
    read_timeout = float(meta.get("read_timeout_s") or DEFAULT_READ_TIMEOUT_S)
    transport = str(meta.get("transport") or "http").lower()
    if transport == "sse":
        if _sse_client is None:  # pragma: no cover
            raise MCPConnectError("SSE transport is not available in this mcp version")
        sse_kw: dict[str, Any] = {}
        if meta.get("_net_guard"):
            sse_kw["httpx_client_factory"] = _guarded_client_factory
        async with _sse_client(
            locator,
            headers=headers,
            timeout=connect_timeout,
            sse_read_timeout=read_timeout,
            **sse_kw,
        ) as streams:
            yield streams[0], streams[1]
        return
    if MCP_MAJOR >= 2:
        timeout = _httpx_for_mcp.Timeout(connect_timeout, read=read_timeout)
        async with _httpx_for_mcp.AsyncClient(
            headers=headers, timeout=timeout, **_guard_kwargs(meta)
        ) as client:
            async with _streamable_http_client(locator, http_client=client) as streams:
                yield streams[0], streams[1]
    else:  # pragma: no cover - mcp 1.x
        async with _streamable_http_client(
            locator, headers=headers, timeout=connect_timeout, sse_read_timeout=read_timeout
        ) as streams:
            yield streams[0], streams[1]


@asynccontextmanager
async def _stdio_streams(locator: str, meta: dict[str, Any]) -> AsyncIterator[tuple[Any, Any]]:
    params = parse_stdio_locator(locator, meta)
    errlog = sys.stderr
    if meta.get("quiet"):
        errlog = open(os.devnull, "w")  # noqa: SIM115 - closed below
    try:
        async with stdio_client(params, errlog=errlog) as streams:
            yield streams[0], streams[1]
    finally:
        if errlog is not sys.stderr:
            errlog.close()


@asynccontextmanager
async def open_session(
    locator: str,
    meta: dict[str, Any] | None = None,
    *,
    connect_timeout_s: float | None = None,
) -> AsyncIterator[ClientSession]:
    """Connect, ``initialize`` (bounded by the connect timeout) and yield the session.

    The ``InitializeResult`` is available afterwards via ``init_result_of(session)`` as a
    wire-format dict. Any transport/protocol failure is re-raised as ``MCPConnectError``
    with a flattened, readable message.
    """
    meta = dict(meta or {})
    if meta.get("_net_guard"):
        # a guarded (non-operator) source: http(s) to a public address only, never a command
        check_url(locator)
    timeout = float(connect_timeout_s or meta.get("timeout_s") or DEFAULT_CONNECT_TIMEOUT_S)
    read_timeout = float(meta.get("read_timeout_s") or DEFAULT_READ_TIMEOUT_S)
    opener = _http_streams if is_http_locator(locator) else _stdio_streams
    try:
        async with opener(locator, meta) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=read_timeout) as session:
                try:
                    async with asyncio.timeout(timeout):
                        init = await session.initialize()
                except TimeoutError as e:
                    raise MCPConnectError(
                        f"timed out after {timeout:.0f}s initialising MCP server {locator}"
                    ) from e
                session._kintsua_init_result = to_wire(init)  # type: ignore[attr-defined]
                yield session
    except MCPConnectError:
        raise
    except BaseExceptionGroup as eg:
        leaves = flatten_exception(eg)
        # user code inside the ``with`` block may have raised something unrelated
        if any(isinstance(leaf, MCPConnectError) for leaf in leaves):
            raise next(leaf for leaf in leaves if isinstance(leaf, MCPConnectError)) from eg
        if any(
            isinstance(leaf, asyncio.CancelledError | KeyboardInterrupt | SystemExit)
            for leaf in leaves
        ):
            raise
        raise MCPConnectError(
            f"{locator}: {describe_exception(eg)}{await _http_hint(locator, meta)}"
        ) from eg
    except (OSError, ValueError, MCPError, RuntimeError) as e:
        if isinstance(e, MCPConnectError):
            raise
        raise MCPConnectError(
            f"{locator}: {describe_exception(e)}{await _http_hint(locator, meta)}"
        ) from e


async def _http_hint(locator: str, meta: dict[str, Any]) -> str:
    """After a failed HTTP connect, POST a bare ``initialize`` to learn the status code and
    ``WWW-Authenticate`` header: the SDK's "Server returned an error response" hides whether
    the server wants OAuth (401), moved (404) or is down (5xx)."""
    if not is_http_locator(locator):
        return ""
    try:
        headers = build_http_headers(meta, locator=locator)
    except MCPConnectError:
        return ""
    headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "kintsua", "version": "0.1"},
        },
    }
    try:
        async with _httpx_for_mcp.AsyncClient(
            headers=headers, timeout=10.0, **_guard_kwargs(meta)
        ) as client:
            r = await client.post(locator, json=payload)
    except Exception:  # noqa: BLE001 - best effort only
        return ""
    status = int(getattr(r, "status_code", 0) or 0)
    if status < 400:
        return ""
    hint = f" [HTTP {status}"
    www = r.headers.get("www-authenticate") if hasattr(r, "headers") else None
    if www:
        hint += f"; WWW-Authenticate: {str(www)[:200]}"
    if status in (401, 403) and not meta.get("auth_env"):
        hint += "; authentication required: set meta.auth_env to the env var holding a token"
    return hint + "]"


# --------------------------------------------------------------------------------------
# Paginated listings
# --------------------------------------------------------------------------------------


async def _paginate(session: ClientSession, method: str, key: str) -> list[dict[str, Any]]:
    fn = getattr(session, method)
    takes_params = "params" in inspect.signature(fn).parameters
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for page in range(MAX_LIST_PAGES + 1):
        if page == MAX_LIST_PAGES:
            # a server handing out endless fresh cursors: fail rather than store a partial
            # listing (which would read as removed tools on the next good poll)
            raise MCPConnectError(f"{method}: more than {MAX_LIST_PAGES} pages; giving up")
        if cursor is None:
            result = await fn()
        elif takes_params and _PaginatedRequestParams is not None:
            result = await fn(params=_PaginatedRequestParams(cursor=cursor))
        else:  # pragma: no cover - mcp 1.x signature
            result = await fn(cursor=cursor)
        wire = to_wire(result) or {}
        items.extend(wire.get(key) or [])
        if len(items) > MAX_LIST_ITEMS:
            raise MCPConnectError(f"{method}: more than {MAX_LIST_ITEMS} items; giving up")
        cursor = wire.get("nextCursor")
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
    return items


async def list_tools_all(session: ClientSession) -> list[dict[str, Any]]:
    return await _paginate(session, "list_tools", "tools")


async def list_resources_all(session: ClientSession) -> list[dict[str, Any]]:
    return await _paginate(session, "list_resources", "resources")


async def list_resource_templates_all(session: ClientSession) -> list[dict[str, Any]]:
    return await _paginate(session, "list_resource_templates", "resourceTemplates")


async def list_prompts_all(session: ClientSession) -> list[dict[str, Any]]:
    return await _paginate(session, "list_prompts", "prompts")
