"""MCP extractor: config files, framework-level registrations in code, and tool-name call sites.

**Config files.** ``.mcp.json``, ``mcp.json``, ``claude_desktop_config.json``,
``.cursor/mcp.json``, ``.vscode/mcp.json`` (``servers`` key), ``.claude/settings*.json`` and
any JSON / YAML file up to 1 MB with an ``mcpServers`` key. Every server entry becomes a
dependency of kind ``mcp`` whose name is its locator: the URL, or the stdio command line
(``npx -y @modelcontextprotocol/server-github``). Only the *names* of ``env`` / ``headers``
entries are kept. A stdio command that runs an npm (``npx``) or PyPI (``uvx`` / ``pipx run`` /
``python -m``) package also yields a package dependency; ``npx mcp-remote <url>`` resolves to
the remote URL.

**Code registrations.** Python: ``MCPServerStdio`` / ``MCPServerStreamableHttp`` /
``MCPServerSse`` (OpenAI Agents SDK), ``MCPToolset`` + ``StdioServerParameters`` /
``StdioConnectionParams`` / ``SseServerParams`` / ``StreamableHTTPConnectionParams``
(Google ADK), ``MultiServerMCPClient({...})`` (langchain-mcp-adapters), ``mcp_servers=[{"url":
...}]`` (Anthropic SDK), ``streamablehttp_client("url")`` / ``sse_client`` (mcp SDK).
TypeScript: ``new StreamableHTTPClientTransport(new URL("..."))`` / ``SSEClientTransport`` /
``StdioClientTransport({command, args})``, ``new MCPServerStdio({...})`` and friends,
``new MultiServerMCPClient({...})`` and ``mcpServers: {...}`` / ``mcp_servers: [...]``
object literals. Only literal URLs / commands are recorded; dynamic ones are counted.

**Tool-name call sites.** ``session.call_tool("name", {...})`` / ``client.callTool({name,
arguments})`` string literals, ``tool_filter=[...]`` / ``allowed_tool_names=[...]`` lists,
and, given the known tool names per server (``tools_for_locator`` callback), every string
literal in the code that equals one of them.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from tree_sitter import Node

from kintsua.indexer import sdk_map
from kintsua.indexer.schema import (
    CallSiteInfo,
    DependencyInfo,
    ExtractResult,
    IndexBudgetExceeded,
)
from kintsua.indexer.treesitter import (
    is_test_path,
    iter_repo_files,
    language_for_path,
    line_of,
    node_text,
    parse_source,
    read_text,
    walk,
)

log = logging.getLogger(__name__)

ToolsForLocator = Callable[[str], list[str] | None]

CONFIG_FILE_NAMES = frozenset(
    {
        ".mcp.json",
        "mcp.json",
        "mcp_config.json",
        "mcp-config.json",
        "claude_desktop_config.json",
        "mcp_servers.json",
    }
)
CONFIG_FILE_PATHS = frozenset(
    {
        ".cursor/mcp.json",
        ".vscode/mcp.json",
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".gemini/settings.json",
        ".codeium/windsurf/mcp_config.json",
        ".continue/config.json",
    }
)
CONFIG_SUFFIXES = (".json", ".yaml", ".yml")
MAX_CONFIG_BYTES = 1_000_000

# python constructors / factories that register an MCP server
PY_MCP_CALLS = frozenset(
    {
        "MCPServerStdio",
        "MCPServerStreamableHttp",
        "MCPServerStreamableHTTP",
        "MCPServerHTTP",
        "MCPServerHttp",
        "MCPServerSse",
        "MCPServerSSE",
        "MCPToolset",
        "StdioServerParameters",
        "StdioConnectionParams",
        "SseServerParams",
        "SseConnectionParams",
        "StreamableHTTPConnectionParams",
        "StreamableHTTPServerParams",
        "streamablehttp_client",
        "streamable_http_client",
        "sse_client",
        "MCPClient",
        "MCPServerAdapter",
    }
)
PY_MULTI_CLIENT = frozenset({"MultiServerMCPClient"})
TS_MCP_CTORS = frozenset(
    {
        "StreamableHTTPClientTransport",
        "SSEClientTransport",
        "StdioClientTransport",
        "MCPServerStdio",
        "MCPServerStreamableHttp",
        "MCPServerSSE",
        "MCPServerSse",
        "MCPClient",
    }
)
TS_MULTI_CLIENT = frozenset({"MultiServerMCPClient"})
_MCP_NAME_RE = re.compile(
    r"(^MCPServer\w*$)|(^MCPToolset$)|((ServerParameters|ServerParams|ConnectionParams|ClientTransport)$)"
)

TOOL_FILTER_KWS = frozenset({"tool_filter", "allowed_tool_names", "include_tools", "allowed_tools"})
_DYN = "\x00"


# --------------------------------------------------------------------------------------
# Config files
# --------------------------------------------------------------------------------------


@dataclass
class ServerEntry:
    name: str
    url: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    transport: str | None = None
    env_keys: list[str] = field(default_factory=list)
    header_keys: list[str] = field(default_factory=list)
    disabled: bool = False

    @property
    def locator(self) -> str | None:
        if self.url:
            return self.url.strip()
        if self.command:
            return shlex.join([self.command, *self.args])
        return None


def _as_str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return shlex.split(v) if v.strip() else []
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, str | int | float)]
    return []


def _transport_of(entry: dict[str, Any], url: str | None) -> str:
    t = entry.get("type") or entry.get("transport")
    if isinstance(t, str) and t:
        t = t.lower().replace("_", "-")
        if t in ("streamable-http", "streamablehttp", "http"):
            return "http"
        if t == "sse":
            return "sse"
        if t == "stdio":
            return "stdio"
    if url:
        return "sse" if url.rstrip("/").endswith("/sse") else "http"
    return "stdio"


def _entry_from(name: str, raw: Any) -> ServerEntry | None:
    if not isinstance(raw, dict):
        return None
    url = raw.get("url") or raw.get("serverUrl") or raw.get("endpoint") or raw.get("uri")
    command = raw.get("command")
    args = _as_str_list(raw.get("args"))
    if isinstance(command, str) and command.strip() and " " in command.strip() and not args:
        parts = shlex.split(command)
        command, args = parts[0], parts[1:]
    if not (isinstance(url, str) and url.strip()) and not (isinstance(command, str) and command):
        return None
    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
    disabled = bool(raw.get("disabled") or raw.get("enabled") is False)
    return ServerEntry(
        name=str(name),
        url=url.strip() if isinstance(url, str) and url.strip() else None,
        command=command.strip() if isinstance(command, str) else None,
        args=args,
        transport=_transport_of(raw, url if isinstance(url, str) else None),
        env_keys=sorted(str(k) for k in env),
        header_keys=sorted(str(k) for k in headers),
        disabled=disabled,
    )


def _iter_server_maps(data: Any, rel_path: str, depth: int = 0) -> list[tuple[str, Any]]:
    """Find ``mcpServers`` (anywhere) and ``servers`` (``.vscode/mcp.json`` / ``mcp.servers``)."""
    found: list[tuple[str, Any]] = []
    if depth > 6:
        return found
    if isinstance(data, dict):
        for key, value in data.items():
            if (
                key == "mcpServers"
                or key == "servers"
                and (rel_path.endswith(".vscode/mcp.json") or rel_path == "mcp.json")
            ):
                found.extend(_expand(value))
            elif (
                key == "mcp" and isinstance(value, dict) and isinstance(value.get("servers"), dict)
            ):
                found.extend(_expand(value["servers"]))
            else:
                found.extend(_iter_server_maps(value, rel_path, depth + 1))
    elif isinstance(data, list):
        for item in data:
            found.extend(_iter_server_maps(item, rel_path, depth + 1))
    return found


def _expand(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        return [(str(k), v) for k, v in value.items()]
    if isinstance(value, list):
        out = []
        for i, item in enumerate(value):
            if isinstance(item, dict):
                out.append((str(item.get("name") or item.get("id") or f"server{i}"), item))
        return out
    return []


def package_from_command(command: str, args: list[str]) -> tuple[DependencyInfo | None, str | None]:
    """``npx -y @scope/pkg@1.2 --flag`` -> (npm dependency, None);
    ``npx -y mcp-remote https://host/mcp`` -> (npm mcp-remote, remote url);
    ``uvx pkg`` / ``pipx run pkg`` / ``python -m pkg`` -> (pypi dependency, None)."""
    cmd = Path(command).name.lower()
    pos = [a for a in args if not a.startswith("-")]

    def npm(spec: str) -> DependencyInfo:
        name, ver = _split_npm_spec(spec)
        entry = sdk_map.lookup_npm(name)
        return DependencyInfo(
            kind="npm",
            name=name,
            vendor=entry.vendor if entry else None,
            display_name=entry.display_name if entry else name,
            version_constraint=ver,
            meta={"language": "typescript", "mcp_server_package": True},
        )

    def pypi(spec: str) -> DependencyInfo:
        name, ver = _split_pypi_spec(spec)
        entry = sdk_map.lookup_pypi(name)
        return DependencyInfo(
            kind="pypi",
            name=name,
            vendor=entry.vendor if entry else None,
            display_name=entry.display_name if entry else name,
            version_constraint=ver,
            meta={"language": "python", "mcp_server_package": True},
        )

    if cmd in ("npx", "bunx", "pnpx", "yarn", "pnpm", "bun", "npm"):
        rest = list(args)
        if cmd in ("yarn", "pnpm", "bun", "npm"):
            if not rest or rest[0] not in ("dlx", "exec", "x"):
                return None, None
            rest = rest[1:]
        spec = None
        i = 0
        while i < len(rest):
            a = rest[i]
            if a in ("-p", "--package"):
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            spec = a
            break
        if not spec:
            return None, None
        dep = npm(spec)
        if dep.name in ("mcp-remote", "supergateway", "mcp-proxy"):
            remote = next((a for a in pos if a.startswith(("http://", "https://"))), None)
            return dep, remote
        return dep, None
    if cmd in ("uvx", "pipx"):
        rest = list(args)
        if cmd == "pipx":
            if not rest or rest[0] != "run":
                return None, None
            rest = rest[1:]
        i = 0
        while i < len(rest):
            a = rest[i]
            if a in ("--from", "--spec", "--python", "-p", "--with"):
                if a in ("--from", "--spec") and i + 1 < len(rest):
                    return pypi(rest[i + 1]), None
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            return pypi(a), None
        return None, None
    if cmd == "uv":
        if len(args) >= 2 and args[0] == "tool" and args[1] == "run":
            return package_from_command("uvx", args[2:])
        if args and args[0] == "run":
            rest = args[1:]
            if "-m" in rest:
                j = rest.index("-m")
                if j + 1 < len(rest):
                    return pypi(rest[j + 1].replace("_", "-")), None
        return None, None
    if cmd.startswith("python"):
        if "-m" in args:
            j = args.index("-m")
            if j + 1 < len(args):
                # ``python -m foo`` may be a local module: the package name is a guess
                dep = pypi(args[j + 1].split(".")[0].replace("_", "-"))
                dep.meta["inferred"] = True
                return dep, None
        return None, None
    if cmd in ("docker", "node", "deno", "bash", "sh"):
        return None, None
    return None, None


def _split_npm_spec(spec: str) -> tuple[str, str | None]:
    s = spec.strip()
    if s.startswith("@"):
        scope, _, rest = s[1:].partition("/")
        name, _, ver = rest.partition("@")
        return f"@{scope}/{name}", (ver or None)
    name, _, ver = s.partition("@")
    return name, (ver or None)


def _split_pypi_spec(spec: str) -> tuple[str, str | None]:
    s = spec.strip()
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?(.*)$", s)
    if not m:
        return s, None
    name, _, rest = m.group(1), m.group(2), (m.group(3) or "").strip()
    if rest.startswith("@"):
        rest = "==" + rest[1:]
    return name, (rest or None)


def dependencies_from_entry(entry: ServerEntry, config_file: str) -> list[DependencyInfo]:
    locator = entry.locator
    if not locator:
        return []
    meta: dict[str, Any] = {
        "config_file": config_file,
        "config_files": [config_file],
        "server_name": entry.name,
        "transport": entry.transport or "stdio",
    }
    if entry.env_keys:
        meta["env_keys"] = entry.env_keys
    if entry.header_keys:
        meta["header_keys"] = entry.header_keys
    if entry.disabled:
        meta["disabled"] = True
    out: list[DependencyInfo] = []
    if entry.command:
        meta["command"] = entry.command
        meta["args"] = list(entry.args)
        pkg, remote = package_from_command(entry.command, entry.args)
        if pkg is not None:
            pkg.meta["config_file"] = config_file
            pkg.meta["config_files"] = [config_file]
            out.append(pkg)
        if remote:
            locator = remote
            meta["transport"] = "sse" if remote.rstrip("/").endswith("/sse") else "http"
            meta["proxy"] = pkg.name if pkg else entry.command
    out.insert(
        0,
        DependencyInfo(
            kind="mcp",
            name=locator,
            display_name=entry.name,
            vendor=vendor_for_locator(locator),
            meta=meta,
        ),
    )
    return out


def vendor_for_locator(locator: str) -> str | None:
    """``https://mcp.linear.app/sse`` -> ``linear`` (curated hosts first, then the domain)."""
    if locator.startswith(("http://", "https://")):
        host = locator.split("://", 1)[1].split("/", 1)[0].split(":")[0]
        e = sdk_map.vendor_for_host(host)
        if e is not None:
            return e.vendor
        parts = host.lower().split(".")
        if len(parts) >= 2 and parts[0] in ("mcp", "api", "www"):
            return parts[-2]
        return None
    return None


def parse_mcp_config(text: str, rel_path: str) -> list[DependencyInfo]:
    """Parse one config document (JSON or YAML) into mcp + package dependencies."""
    data: Any
    try:
        if rel_path.lower().endswith((".yaml", ".yml")):
            data = yaml.safe_load(text)
        else:
            data = json.loads(_strip_json_comments(text))
    except (ValueError, yaml.YAMLError) as e:
        log.info("mcp config %s unparseable: %s", rel_path, e)
        return []
    out: list[DependencyInfo] = []
    for name, raw in _iter_server_maps(data, rel_path):
        entry = _entry_from(name, raw)
        if entry is None:
            continue
        out.extend(dependencies_from_entry(entry, rel_path))
    return out


def _strip_json_comments(text: str) -> str:
    """Claude / VS Code configs are JSONC: strip // and /* */ comments outside strings and
    trailing commas."""
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _is_config_candidate(rel: str, name: str, text: str) -> bool:
    if name in CONFIG_FILE_NAMES or rel in CONFIG_FILE_PATHS:
        return True
    if rel.startswith(".claude/") and name.startswith("settings") and name.endswith(".json"):
        return True
    return "mcpServers" in text


def scan_config_files(root: str | Path, *, deadline: float | None = None) -> ExtractResult:
    root = Path(root)
    result = ExtractResult()
    for path, rel, too_large in iter_repo_files(root, CONFIG_SUFFIXES, max_bytes=MAX_CONFIG_BYTES):
        if deadline is not None and time.monotonic() > deadline:
            raise IndexBudgetExceeded(f"mcp config scan ran out of budget at {rel}")
        if too_large or path.name == "package.json" or path.name.endswith("lock.json"):
            continue
        text = read_text(path)
        if text is None or not _is_config_candidate(rel, path.name, text):
            continue
        for dep in parse_mcp_config(text, rel):
            result.add_dependency(dep)
    return result


# --------------------------------------------------------------------------------------
# Code registrations
# --------------------------------------------------------------------------------------


@dataclass
class Registration:
    locator: str
    transport: str
    line: int
    framework: str
    server_name: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env_keys: list[str] = field(default_factory=list)
    header_keys: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)  # tool_filter / allowed_tool_names
    dynamic_args: bool = False  # trailing args were not literal and were dropped


@dataclass
class _Lang:
    name: str  # "python" | "typescript"
    call_types: tuple[str, ...]
    string_types: tuple[str, ...]
    dict_type: str
    list_types: tuple[str, ...]
    pair_type: str
    kw_type: str | None


_PY = _Lang(
    "python",
    ("call",),
    ("string", "concatenated_string"),
    "dictionary",
    ("list", "tuple"),
    "pair",
    "keyword_argument",
)
_TS = _Lang(
    "typescript",
    ("call_expression", "new_expression"),
    ("string", "template_string"),
    "object",
    ("array",),
    "pair",
    None,
)


def _lit(node: Node | None, src: bytes, lang: _Lang, constants: dict[str, str]) -> str | None:
    if node is None:
        return None
    t = node.type
    if lang.name == "python":
        if t == "string":
            if any(c.type == "interpolation" for c in node.children):
                return None
            return "".join(node_text(c, src) for c in node.children if c.type == "string_content")
        if t == "concatenated_string":
            parts = [_lit(c, src, lang, constants) for c in node.children if c.type == "string"]
            return None if any(p is None for p in parts) else "".join(p or "" for p in parts)
        if t == "identifier":
            return constants.get(node_text(node, src))
        if t == "parenthesized_expression":
            inner = next((c for c in node.children if c.is_named), None)
            return _lit(inner, src, lang, constants)
        return None
    if t == "string":
        return "".join(node_text(c, src) for c in node.children if c.type == "string_fragment")
    if t == "template_string":
        if any(c.type == "template_substitution" for c in node.children):
            return None
        return "".join(node_text(c, src) for c in node.children if c.type == "string_fragment")
    if t == "identifier":
        return constants.get(node_text(node, src))
    if t == "new_expression":  # new URL("...")
        ctor = node.child_by_field_name("constructor")
        args = node.child_by_field_name("arguments")
        if ctor is not None and node_text(ctor, src) == "URL" and args is not None:
            first = next((c for c in args.children if c.is_named), None)
            return _lit(first, src, lang, constants)
        return None
    if t in ("parenthesized_expression", "as_expression", "non_null_expression"):
        inner = next((c for c in node.children if c.is_named and c.type != "type"), None)
        return _lit(inner, src, lang, constants)
    return None


def _list_lits(
    node: Node | None, src: bytes, lang: _Lang, constants: dict[str, str]
) -> list[str] | None:
    """Literal list elements; a non-literal element becomes ``_DYN`` (None when the node
    is not a list at all)."""
    if node is None or node.type not in lang.list_types:
        return None
    out: list[str] = []
    for c in node.children:
        if not c.is_named or c.type == "comment":
            continue
        v = _lit(c, src, lang, constants)
        out.append(v if v is not None else _DYN)
    return out


def _dict_items(node: Node | None, src: bytes, lang: _Lang) -> list[tuple[str, Node]]:
    if node is None or node.type != lang.dict_type:
        return []
    out = []
    for c in node.children:
        if c.type == lang.pair_type:
            k = c.child_by_field_name("key")
            v = c.child_by_field_name("value")
            if k is None or v is None:
                continue
            key = _lit(k, src, lang, {}) if k.type in lang.string_types else node_text(k, src)
            out.append((key or node_text(k, src), v))
        elif lang.name == "typescript" and c.type == "shorthand_property_identifier":
            out.append((node_text(c, src), c))
    return out


def _call_name(call: Node, src: bytes, lang: _Lang) -> str | None:
    fn = call.child_by_field_name("function") or call.child_by_field_name("constructor")
    if fn is None:
        return None
    text = node_text(fn, src)
    return text.split(".")[-1].split("?.")[-1]


def _call_arg_nodes(call: Node, src: bytes, lang: _Lang) -> tuple[list[Node], dict[str, Node]]:
    args = call.child_by_field_name("arguments")
    pos: list[Node] = []
    kws: dict[str, Node] = {}
    if args is None:
        return pos, kws
    for c in args.children:
        if not c.is_named or c.type == "comment":
            continue
        if lang.kw_type and c.type == lang.kw_type:
            k = c.child_by_field_name("name")
            v = c.child_by_field_name("value")
            if k is not None and v is not None:
                kws[node_text(k, src)] = v
        else:
            pos.append(c)
    return pos, kws


@dataclass
class _Params:
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    transport: str | None = None
    env_keys: list[str] = field(default_factory=list)
    header_keys: list[str] = field(default_factory=list)
    name: str | None = None
    tool_names: list[str] = field(default_factory=list)
    dynamic: bool = False
    dynamic_args: bool = False


def _absorb(
    params: _Params, key: str, value: Node, src: bytes, lang: _Lang, constants: dict[str, str]
) -> None:
    if key in ("url", "serverUrl", "server_url", "endpoint"):
        v = _lit(value, src, lang, constants)
        if v is None:
            params.dynamic = True
        elif params.url is None:
            params.url = v
    elif key == "command":
        v = _lit(value, src, lang, constants)
        if v is None:
            params.dynamic = True
        elif params.command is None:
            params.command = v
    elif key == "args":
        v = _list_lits(value, src, lang, constants)
        if v is None:
            params.dynamic = True
        elif params.args is None:
            if _DYN in v:
                params.dynamic_args = True
                v = v[: v.index(_DYN)]  # keep the literal prefix (the package is in it)
            params.args = v
    elif key in ("transport", "type"):
        v = _lit(value, src, lang, constants)
        if v and params.transport is None:
            params.transport = v
    elif key == "env":
        params.env_keys = sorted(k for k, _ in _dict_items(value, src, lang))
    elif key == "headers":
        params.header_keys = sorted(k for k, _ in _dict_items(value, src, lang))
    elif key == "name":
        v = _lit(value, src, lang, constants)
        if v and params.name is None:
            params.name = v
    elif key in TOOL_FILTER_KWS:
        v = _list_lits(value, src, lang, constants)
        if v:
            params.tool_names.extend(x for x in v if x != _DYN)


def _collect_params(
    node: Node, src: bytes, lang: _Lang, constants: dict[str, str], params: _Params, depth: int = 0
) -> None:
    """Walk a call's arguments: keyword args, dict literals and nested calls."""
    if depth > 5:
        return
    pos, kws = _call_arg_nodes(node, src, lang)
    for k, v in kws.items():
        if k in ("params", "connection_params", "server_params", "connections"):
            _collect_from_value(v, src, lang, constants, params, depth + 1)
        else:
            _absorb(params, k, v, src, lang, constants)
    for p in pos:
        _collect_from_value(p, src, lang, constants, params, depth + 1)


def _collect_from_value(
    v: Node, src: bytes, lang: _Lang, constants: dict[str, str], params: _Params, depth: int
) -> None:
    if v.type == lang.dict_type:
        for k, vv in _dict_items(v, src, lang):
            if k in ("params", "connection_params", "server_params", "serverParams"):
                _collect_from_value(vv, src, lang, constants, params, depth + 1)
            else:
                _absorb(params, k, vv, src, lang, constants)
    elif v.type in lang.call_types:
        name = _call_name(v, src, lang) or ""
        if name in PY_MCP_CALLS or name in TS_MCP_CTORS or _MCP_NAME_RE.search(name):
            _collect_params(v, src, lang, constants, params, depth + 1)
        elif name == "URL":
            lit = _lit(v, src, lang, constants)
            if lit and params.url is None:
                params.url = lit
    elif (
        v.type in lang.string_types and depth <= 1 and params.url is None and params.command is None
    ):
        lit = _lit(v, src, lang, constants)
        if lit and lit.startswith(("http://", "https://")):
            params.url = lit


def _registration_from(
    params: _Params, line: int, framework: str, stats: Any
) -> Registration | None:
    if params.url:
        transport = (
            "sse"
            if (params.transport or "").lower() == "sse" or params.url.rstrip("/").endswith("/sse")
            else "http"
        )
        return Registration(
            locator=params.url.strip(),
            transport=transport,
            line=line,
            framework=framework,
            server_name=params.name,
            header_keys=params.header_keys,
            tool_names=list(params.tool_names),
        )
    if params.command:
        args = params.args or []
        return Registration(
            locator=shlex.join([params.command, *args]),
            transport="stdio",
            line=line,
            framework=framework,
            server_name=params.name,
            command=params.command,
            args=args,
            env_keys=params.env_keys,
            tool_names=list(params.tool_names),
            dynamic_args=params.dynamic_args,
        )
    if params.dynamic:
        stats.dynamic_mcp_registrations += 1
    return None


def _constants(root: Node, src: bytes, lang: _Lang) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in walk(root):
        if lang.name == "python" and node.type == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and right is not None and left.type == "identifier":
                v = _lit(right, src, lang, {})
                if v is not None:
                    out.setdefault(node_text(left, src), v)
        elif lang.name == "typescript" and node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if name is not None and value is not None and name.type == "identifier":
                v = _lit(value, src, lang, {})
                if v is not None:
                    out.setdefault(node_text(name, src), v)
    return out


@dataclass
class _ToolCall:
    line: int
    name: str
    keys: list[str]
    symbol: str
    end_line: int = 0


def _code_file(source: str, rel: str, stats: Any) -> tuple[list[Registration], list[_ToolCall]]:
    lang_name = language_for_path(rel)
    if lang_name is None:
        return [], []
    lang = _PY if lang_name == "python" else _TS
    tree, src = parse_source(lang_name, source)
    constants = _constants(tree.root_node, src, lang)
    regs: list[Registration] = []
    tool_calls: list[_ToolCall] = []
    covered: set[tuple[int, int]] = set()  # byte ranges of nested registration calls

    for node in walk(tree.root_node):
        if node.type in lang.call_types:
            if (node.start_byte, node.end_byte) in covered:
                continue
            name = _call_name(node, src, lang) or ""
            if name in PY_MULTI_CLIENT or name in TS_MULTI_CLIENT:
                regs.extend(_multi_client(node, src, lang, constants, stats))
                _cover(node, lang, covered)
                continue
            if name in PY_MCP_CALLS or name in TS_MCP_CTORS or _MCP_NAME_RE.search(name):
                params = _Params()
                _collect_params(node, src, lang, constants, params)
                reg = _registration_from(params, line_of(node), name, stats)
                if reg is not None:
                    regs.append(reg)
                    _cover(node, lang, covered)
                continue
            if name in ("call_tool", "callTool"):
                tc = _tool_call(node, src, lang, constants)
                if tc is not None:
                    tool_calls.append(tc)
                continue
            # Anthropic SDK: mcp_servers=[{"type": "url", "url": ..., "name": ...}]
            _pos, kws = _call_arg_nodes(node, src, lang)
            servers = kws.get("mcp_servers")
            if servers is None and lang.name == "typescript":
                for p in _pos:
                    for k, v in _dict_items(p, src, lang):
                        if k in ("mcp_servers", "mcpServers"):
                            servers = v
            if servers is not None:
                regs.extend(_server_list(servers, src, lang, constants, line_of(node), stats))
        elif lang.name == "typescript" and node.type == "pair":
            k = node.child_by_field_name("key")
            v = node.child_by_field_name("value")
            if k is not None and v is not None and node_text(k, src).strip("\"'") == "mcpServers":
                if (v.start_byte, v.end_byte) not in covered:
                    regs.extend(_server_map(v, src, lang, constants, stats))
                    covered.add((v.start_byte, v.end_byte))
    return regs, tool_calls


def _cover(node: Node, lang: _Lang, covered: set[tuple[int, int]]) -> None:
    for inner in walk(node):
        if inner.type in lang.call_types or inner.type == lang.dict_type:
            covered.add((inner.start_byte, inner.end_byte))


def _multi_client(
    call: Node, src: bytes, lang: _Lang, constants: dict[str, str], stats: Any
) -> list[Registration]:
    pos, kws = _call_arg_nodes(call, src, lang)
    candidates = list(pos) + [
        v for k, v in kws.items() if k in ("connections", "mcpServers", "servers")
    ]
    out: list[Registration] = []
    for cand in candidates:
        if cand.type != lang.dict_type:
            continue
        items = _dict_items(cand, src, lang)
        # langchain js: new MultiServerMCPClient({ mcpServers: {...}, ... })
        nested = [v for k, v in items if k in ("mcpServers", "servers")]
        if nested:
            for n in nested:
                out.extend(_server_map(n, src, lang, constants, stats))
            continue
        out.extend(_server_map(cand, src, lang, constants, stats))
    return out


def _server_map(
    node: Node, src: bytes, lang: _Lang, constants: dict[str, str], stats: Any
) -> list[Registration]:
    out: list[Registration] = []
    for name, v in _dict_items(node, src, lang):
        if v.type != lang.dict_type:
            continue
        params = _Params(name=name)
        for k, vv in _dict_items(v, src, lang):
            _absorb(params, k, vv, src, lang, constants)
        reg = _registration_from(params, line_of(v), "MultiServerMCPClient", stats)
        if reg is not None:
            out.append(reg)
    return out


def _server_list(
    node: Node, src: bytes, lang: _Lang, constants: dict[str, str], line: int, stats: Any
) -> list[Registration]:
    out: list[Registration] = []
    if node.type not in lang.list_types:
        return out
    for item in node.children:
        if item.type != lang.dict_type:
            continue
        params = _Params()
        for k, vv in _dict_items(item, src, lang):
            _absorb(params, k, vv, src, lang, constants)
        reg = _registration_from(params, line_of(item), "mcp_servers", stats)
        if reg is not None:
            out.append(reg)
    return out


def _tool_call(call: Node, src: bytes, lang: _Lang, constants: dict[str, str]) -> _ToolCall | None:
    pos, kws = _call_arg_nodes(call, src, lang)
    name: str | None = None
    args_node: Node | None = None
    if lang.name == "python":
        name_node = kws.get("name") or (pos[0] if pos else None)
        name = _lit(name_node, src, lang, constants)
        args_node = kws.get("arguments") or (pos[1] if len(pos) > 1 else None)
    else:
        obj = pos[0] if pos and pos[0].type == lang.dict_type else None
        if obj is not None:
            items = dict(_dict_items(obj, src, lang))
            name = _lit(items.get("name"), src, lang, constants)
            args_node = items.get("arguments") or items.get("params")
        elif pos:
            name = _lit(pos[0], src, lang, constants)
            args_node = pos[1] if len(pos) > 1 else None
    if not name:
        return None
    keys = [k for k, _ in _dict_items(args_node, src, lang)] if args_node is not None else []
    return _ToolCall(
        line=line_of(call),
        name=name,
        keys=keys,
        symbol="call_tool",
        end_line=call.end_point[0] + 1,
    )


# --------------------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------------------

CODE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".mts", ".cts")


def extract_mcp(
    root: str | Path,
    *,
    tools_for_locator: ToolsForLocator | None = None,
    deadline: float | None = None,
) -> ExtractResult:
    """Config files + code registrations + tool-name call sites for one repo tree."""
    root = Path(root)
    result = scan_config_files(root, deadline=deadline)
    file_regs: dict[str, list[Registration]] = {}
    file_tool_calls: dict[str, list[_ToolCall]] = {}
    code_files: dict[str, Path] = {}  # rel -> path, re-read only for the literal scan

    for path, rel, too_large in iter_repo_files(root, CODE_SUFFIXES):
        if deadline is not None and time.monotonic() > deadline:
            raise IndexBudgetExceeded(f"mcp code scan ran out of budget at {rel}")
        if too_large or rel.endswith((".min.js", ".d.ts")):
            continue
        text = read_text(path)
        if text is None:
            continue
        try:
            regs, calls = _code_file(text, rel, result.stats)
        except Exception as e:  # noqa: BLE001
            log.warning("mcp code scan failed for %s: %s", rel, e)
            result.stats.record_error(rel, f"{type(e).__name__}: {e}")
            continue
        code_files[rel] = path
        if regs:
            file_regs[rel] = regs
        if calls:
            file_tool_calls[rel] = calls
        for reg in regs:
            _add_registration(result, reg, rel)

    mcp_deps = [d for d in result.dependencies if d.kind == "mcp"]
    known: dict[str, list[str]] = {}
    if tools_for_locator is not None:
        for dep in mcp_deps:
            try:
                names = tools_for_locator(dep.name)
            except Exception as e:  # noqa: BLE001 - a dead server must not fail the index
                log.info("tools_for_locator(%s) failed: %s", dep.name, e)
                names = None
            if names:
                known[dep.name] = sorted({n for n in names if isinstance(n, str) and n})
    by_tool: dict[str, list[str]] = {}
    for locator, names in known.items():
        for n in names:
            by_tool.setdefault(n, []).append(locator)

    recorded: set[tuple[str, int, str, str]] = set()  # (file, line, locator, tool)

    def record(rel: str, line: int, locator: str, tool: str, symbol: str, keys: list[str]) -> None:
        key = (rel, line, locator, tool)
        if key in recorded:
            return
        recorded.add(key)
        result.call_sites.append(
            CallSiteInfo(
                dependency_kind="mcp",
                dependency_name=locator,
                file_path=rel,
                line=line,
                symbol=symbol,
                operation=tool,
                args_shape={"keys": keys},
                in_tests=is_test_path(rel),
            )
        )

    # tool_filter / allowed_tool_names literals belong to their own registration
    for rel, regs in file_regs.items():
        for reg in regs:
            for tool in reg.tool_names:
                record(rel, reg.line, reg.locator, tool, "tool_filter", [])

    # call_tool("name") literals
    for rel, calls in file_tool_calls.items():
        for tc in calls:
            locators = by_tool.get(tc.name)
            if not locators:
                locators = _fallback_locators(rel, file_regs, mcp_deps)
            if not locators:
                result.stats.unattributed_tool_calls += 1
                continue
            for loc in locators:
                record(rel, tc.line, loc, tc.name, tc.symbol, tc.keys)

    # every other string literal equal to a known tool name (not the ones inside a
    # call_tool(...) already recorded, which may span several lines)
    covered_lines: dict[str, set[tuple[int, str]]] = {}
    for rel, calls in file_tool_calls.items():
        for tc in calls:
            for ln in range(tc.line, max(tc.line, tc.end_line) + 1):
                covered_lines.setdefault(rel, set()).add((ln, tc.name))
    if by_tool:
        pattern = re.compile(
            r"""(?<=["'`])("""
            + "|".join(re.escape(n) for n in sorted(by_tool, key=len, reverse=True))
            + r""")(?=["'`])"""
        )
        for rel, path in code_files.items():
            text = read_text(path)
            if text is None:
                continue
            for i, line_text in enumerate(text.splitlines(), start=1):
                if '"' not in line_text and "'" not in line_text and "`" not in line_text:
                    continue
                for m in pattern.finditer(line_text):
                    tool = m.group(1)
                    if (i, tool) in covered_lines.get(rel, ()):
                        continue
                    for loc in by_tool[tool]:
                        if (rel, i, loc, tool) in recorded:
                            continue
                        record(rel, i, loc, tool, "literal", [])
    for dep in mcp_deps:
        if dep.name in known:
            dep.meta["tool_names"] = known[dep.name]
    return result


def _fallback_locators(
    rel: str, file_regs: dict[str, list[Registration]], mcp_deps: list[DependencyInfo]
) -> list[str]:
    same_file = list(dict.fromkeys(r.locator for r in file_regs.get(rel, [])))
    if len(same_file) == 1:
        return same_file
    if not same_file and len(mcp_deps) == 1:
        return [mcp_deps[0].name]
    return []


def _add_registration(result: ExtractResult, reg: Registration, rel: str) -> None:
    meta: dict[str, Any] = {
        "config_file": rel,
        "config_files": [rel],
        "transport": reg.transport,
        "framework": reg.framework,
        "line": reg.line,
    }
    if reg.server_name:
        meta["server_name"] = reg.server_name
    if reg.command:
        meta["command"] = reg.command
        meta["args"] = list(reg.args)
    if reg.env_keys:
        meta["env_keys"] = reg.env_keys
    if reg.header_keys:
        meta["header_keys"] = reg.header_keys
    if reg.dynamic_args:
        meta["dynamic_args"] = True
    result.add_dependency(
        DependencyInfo(
            kind="mcp",
            name=reg.locator,
            display_name=reg.server_name or None,
            vendor=vendor_for_locator(reg.locator),
            meta=meta,
        )
    )
    if reg.command:
        pkg, remote = package_from_command(reg.command, reg.args)
        if pkg is not None:
            pkg.meta["config_file"] = rel
            pkg.meta["config_files"] = [rel]
            result.add_dependency(pkg)


__all__ = [
    "Registration",
    "ServerEntry",
    "dependencies_from_entry",
    "extract_mcp",
    "package_from_command",
    "parse_mcp_config",
    "scan_config_files",
]
