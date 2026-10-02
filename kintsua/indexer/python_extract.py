"""Python extractor: SDK imports, SDK call chains and raw HTTP calls, via tree-sitter.

Per file, two passes over the syntax tree:

1. **bindings** - ``import stripe`` / ``from slack_sdk import WebClient as WC`` for packages
   in the curated map (``sdk_map``); **instances** - variables (including ``self.x``) assigned
   from a binding's constructor or factory (``client = WebClient(...)``,
   ``async with httpx.AsyncClient(base_url=...) as c``, typed parameters); **constants** -
   module / class level ``NAME = "literal"`` so ``f"{BASE}/pages"`` resolves.
2. **calls** - attribute-call chains rooted at a binding or instance become SDK call sites
   (``symbol = operation = "slack_sdk.WebClient.chat_postMessage"``, ``args_shape.keys`` =
   keyword names); calls on ``requests`` / ``httpx`` / ``aiohttp`` whose URL is a literal,
   an f-string / ``%`` / ``.format`` / ``+`` expression with a literal scheme+host prefix, or
   a relative path on a client with a literal ``base_url`` become HTTP call sites against
   a dependency of kind ``http`` keyed by host (``operation = "POST /v1/charges/{id}"``).

Declared packages (``pyproject.toml``, ``requirements*.txt``) become ``pypi`` dependencies
too, so unknown-but-declared packages show in the map (unwatched).
"""

from __future__ import annotations

import logging
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from tree_sitter import Node

from kintsua.indexer import sdk_map
from kintsua.indexer.schema import (
    CallSiteInfo,
    DependencyInfo,
    ExtractResult,
    ExtractStats,
    IndexBudgetExceeded,
)
from kintsua.indexer.treesitter import (
    is_test_path,
    iter_repo_files,
    line_of,
    node_text,
    parse_source,
    read_text,
    walk,
)

log = logging.getLogger(__name__)

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
HTTP_GENERIC = frozenset({"request", "stream"})
# symbol prefixes whose get/post/... methods are raw HTTP calls
HTTP_MODULE_PREFIXES = frozenset({"requests", "httpx", "aiohttp"})
HTTP_CLIENT_CLASSES = frozenset(
    {
        "requests.Session",
        "requests.sessions.Session",
        "httpx.Client",
        "httpx.AsyncClient",
        "aiohttp.ClientSession",
        "aiohttp.client.ClientSession",
    }
)
# calls the MCP extractor owns (tool-name call sites); not recorded as SDK call sites here
MCP_TOOL_CALL_NAMES = frozenset({"call_tool"})
# factories that return a client: the instance they create is tracked instead
FACTORY_SYMBOLS = frozenset(
    {"boto3.client", "boto3.resource", "boto3.Session", "googleapiclient.discovery.build"}
)
# trailing segments that navigate a result rather than name an operation
# (stripe.Charge.list(...).auto_paging_iter(), service.files().list(...).execute())
RESULT_NAVIGATION = frozenset(
    {
        "execute",
        "auto_paging_iter",
        "then",
        "catch",
        "finally",
        "json",
        "result",
        "to_dict",
        "model_dump",
        "wait",
        "get_result",
        "parse",
    }
)

# Dev / build tooling that is never an external integration: declared but not reported.
TOOLING_PACKAGES = frozenset(
    {
        "pytest",
        "pytest-asyncio",
        "pytest-cov",
        "pytest-mock",
        "pytest-timeout",
        "pytest-xdist",
        "ruff",
        "mypy",
        "black",
        "flake8",
        "isort",
        "pylint",
        "pyright",
        "pre-commit",
        "coverage",
        "pip",
        "setuptools",
        "wheel",
        "build",
        "hatch",
        "hatchling",
        "poetry",
        "poetry-core",
        "tox",
        "nox",
        "twine",
        "sphinx",
        "mkdocs",
        "mkdocs-material",
        "ipython",
        "jupyter",
        "notebook",
        "ipykernel",
        "typing-extensions",
        "types-requests",
        "types-pyyaml",
        "bumpversion",
        "bump2version",
        "uv",
    }
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)
_NUM_RE = re.compile(r"^\d+$")
# cus_1234abcd, pi_3MtwBw...: a short lowercase prefix, an underscore, 8+ alphanumerics with a digit
_PREFIXED_ID_RE = re.compile(r"^[a-z]{1,8}_(?=[0-9A-Za-z]*\d)[0-9A-Za-z]{8,}$")
_FORMAT_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
_PERCENT_RE = re.compile(r"%(\(\w+\))?[-#0 +]*\d*(\.\d+)?[sdifrxXou]")
_URL_RE = re.compile(r"^(https?)://([^/?#]*)(.*)$", re.I | re.S)
_DYN = "\x00"  # marks a dynamic segment inside a URL template

# --------------------------------------------------------------------------------------
# Declared packages (pyproject.toml / requirements*.txt)
# --------------------------------------------------------------------------------------


@dataclass
class DeclaredPackage:
    name: str  # as written
    canonical: str
    constraint: str | None
    config_file: str
    dev: bool = False


def _parse_requirement(line: str) -> tuple[str, str | None] | None:
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith(("-", "--")):
        return None
    if line.startswith(("http://", "https://", "git+", "file:", ".", "/")):
        return None
    try:
        req = Requirement(line)
    except InvalidRequirement:
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if not m:
            return None
        return m.group(1), None
    spec = str(req.specifier) if req.specifier else None
    if req.url:
        spec = spec or f"@ {req.url}"
    return req.name, spec


def _requirements_files(root: Path) -> list[Path]:
    out = []
    for p, rel, too_large in iter_repo_files(root, (".txt", ".in"), max_bytes=256_000):
        name = p.name.lower()
        if too_large:
            continue
        if name.startswith("requirements") or name.endswith(
            ("requirements.txt", "requirements.in", "-requirements.txt")
        ):
            out.append(p)
    return out


def _read_requirements(path: Path, root: Path, seen: set[Path]) -> list[DeclaredPackage]:
    path = path.resolve()
    if path in seen or not path.exists():
        return []
    seen.add(path)
    text = read_text(path) or ""
    rel = path.relative_to(root).as_posix()
    dev = any(k in path.name.lower() for k in ("dev", "test", "lint", "docs", "ci"))
    out: list[DeclaredPackage] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("-r ", "--requirement ", "-c ", "--constraint ")):
            target = line.split(None, 1)[1].strip()
            nested = (path.parent / target).resolve()
            try:
                nested.relative_to(root.resolve())
            except ValueError:
                continue
            out.extend(_read_requirements(nested, root, seen))
            continue
        parsed = _parse_requirement(line)
        if parsed is None:
            continue
        name, spec = parsed
        out.append(DeclaredPackage(name, canonicalize_name(name), spec, rel, dev))
    return out


def _poetry_constraint(v: Any) -> str | None:
    if isinstance(v, str):
        return v or None
    if isinstance(v, dict):
        ver = v.get("version")
        return str(ver) if ver else None
    if isinstance(v, list) and v:
        return _poetry_constraint(v[0])
    return None


def _read_pyproject(path: Path, root: Path) -> list[DeclaredPackage]:
    rel = path.relative_to(root).as_posix()
    try:
        data = tomllib.loads(read_text(path) or "")
    except (tomllib.TOMLDecodeError, ValueError) as e:
        log.info("pyproject %s unreadable: %s", rel, e)
        return []
    out: list[DeclaredPackage] = []

    def add(items: Any, dev: bool) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):  # dependency-groups include-group entries
                continue
            parsed = _parse_requirement(str(item))
            if parsed:
                name, spec = parsed
                out.append(DeclaredPackage(name, canonicalize_name(name), spec, rel, dev))

    project = data.get("project") or {}
    add(project.get("dependencies"), dev=False)
    for group, items in (project.get("optional-dependencies") or {}).items():
        add(items, dev=_is_dev_group(str(group)))
    for group, items in (data.get("dependency-groups") or {}).items():
        add(items, dev=_is_dev_group(str(group)) or str(group) == "dev")
    poetry = (data.get("tool") or {}).get("poetry") or {}
    for name, v in (poetry.get("dependencies") or {}).items():
        if name.lower() == "python":
            continue
        out.append(DeclaredPackage(name, canonicalize_name(name), _poetry_constraint(v), rel))
    for name, v in (poetry.get("dev-dependencies") or {}).items():
        out.append(
            DeclaredPackage(name, canonicalize_name(name), _poetry_constraint(v), rel, dev=True)
        )
    for group, gdata in (poetry.get("group") or {}).items():
        for name, v in ((gdata or {}).get("dependencies") or {}).items():
            out.append(
                DeclaredPackage(
                    name,
                    canonicalize_name(name),
                    _poetry_constraint(v),
                    rel,
                    dev=_is_dev_group(str(group)),
                )
            )
    return out


def _is_dev_group(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in ("dev", "test", "lint", "doc", "typing", "ci"))


def declared_python_packages(root: str | Path) -> list[DeclaredPackage]:
    """Every package declared in pyproject.toml (PEP 621, poetry, dependency-groups) and
    requirements*.txt / *.in files anywhere in the tree (vendored dirs skipped)."""
    root = Path(root).resolve()
    out: list[DeclaredPackage] = []
    for p, _rel, too_large in iter_repo_files(root, names=("pyproject.toml",), max_bytes=512_000):
        if not too_large:
            out.extend(_read_pyproject(p, root))
    seen: set[Path] = set()
    for p in _requirements_files(root):
        out.extend(_read_requirements(p, root, seen))
    return out


# --------------------------------------------------------------------------------------
# Per-file scope
# --------------------------------------------------------------------------------------


@dataclass
class _Scope:
    bindings: dict[str, str] = field(default_factory=dict)  # local -> dotted import path
    instances: dict[str, str] = field(default_factory=dict)  # var / self.x -> ctor symbol
    base_urls: dict[str, str] = field(default_factory=dict)  # var / self.x -> base url template
    constants: dict[str, str] = field(default_factory=dict)  # NAME -> literal string
    returns: dict[str, str] = field(default_factory=dict)  # local fn -> annotated return symbol
    imported_roots: dict[str, str] = field(default_factory=dict)  # import root -> import root
    suppressed: set[tuple[int, int]] = field(default_factory=set)  # inner call byte ranges


def _string_literal(node: Node, src: bytes) -> str | None:
    """Plain (non f-) string literal value, else None."""
    if node.type == "string":
        if any(c.type == "interpolation" for c in node.children):
            return None
        return "".join(node_text(c, src) for c in node.children if c.type == "string_content")
    if node.type == "concatenated_string":
        parts = [_string_literal(c, src) for c in node.children if c.type == "string"]
        if any(p is None for p in parts):
            return None
        return "".join(p for p in parts if p is not None)
    return None


def _template(node: Node, src: bytes, scope: _Scope, depth: int = 0) -> str | None:
    """Resolve an expression to a URL template where dynamic parts are ``_DYN``.
    Returns None when nothing literal can be known about it."""
    if depth > 6:
        return None
    t = node.type
    if t == "string":
        parts: list[str] = []
        for c in node.children:
            if c.type == "string_content":
                parts.append(node_text(c, src))
            elif c.type == "interpolation":
                expr = next((x for x in c.children if x.is_named), None)
                inner = _template(expr, src, scope, depth + 1) if expr is not None else None
                parts.append(inner if inner is not None else _DYN)
        return "".join(parts)
    if t == "concatenated_string":
        parts = []
        for c in node.children:
            if c.type == "string":
                parts.append(_template(c, src, scope, depth + 1) or _DYN)
        return "".join(parts)
    if t == "parenthesized_expression":
        inner = next((c for c in node.children if c.is_named), None)
        return _template(inner, src, scope, depth + 1) if inner is not None else None
    if t == "identifier":
        return scope.constants.get(node_text(node, src))
    if t == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is not None and attr is not None and node_text(obj, src) in ("self", "cls"):
            return scope.constants.get(node_text(attr, src))
        return None
    if t == "binary_operator":
        op = node.child_by_field_name("operator")
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if op is None or left is None or right is None:
            return None
        ops = node_text(op, src)
        if ops == "+":
            lt = _template(left, src, scope, depth + 1)
            if lt is None:
                return None
            rt = _template(right, src, scope, depth + 1)
            return lt + (rt if rt is not None else _DYN)
        if ops == "%":
            lt = _template(left, src, scope, depth + 1)
            if lt is None:
                return None
            return _PERCENT_RE.sub(_DYN, lt)
        return None
    if t == "call":
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type == "attribute":
            attr = fn.child_by_field_name("attribute")
            obj = fn.child_by_field_name("object")
            if attr is not None and obj is not None and node_text(attr, src) == "format":
                base = _template(obj, src, scope, depth + 1)
                if base is None:
                    return None
                return _FORMAT_PLACEHOLDER_RE.sub(_DYN, base)
        if fn is not None and node_text(fn, src).split(".")[-1] == "urljoin":
            args = _positional_args(node)
            if len(args) >= 2:
                b = _template(args[0], src, scope, depth + 1)
                r = _template(args[1], src, scope, depth + 1)
                if b is not None and r is not None:
                    return join_url(b, r)
        return None
    return None


def _positional_args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    if args is None:
        return []
    return [
        c
        for c in args.children
        if c.is_named
        and c.type not in ("keyword_argument", "list_splat", "dictionary_splat", "comment")
    ]


def _keyword_args(call: Node, src: bytes) -> dict[str, Node]:
    args = call.child_by_field_name("arguments")
    out: dict[str, Node] = {}
    if args is None:
        return out
    for c in args.children:
        if c.type == "keyword_argument":
            name = c.child_by_field_name("name")
            value = c.child_by_field_name("value")
            if name is not None and value is not None:
                out[node_text(name, src)] = value
    return out


def _dict_keys(node: Node, src: bytes) -> list[str]:
    keys: list[str] = []
    if node.type != "dictionary":
        return keys
    for c in node.children:
        if c.type == "pair":
            k = c.child_by_field_name("key")
            if k is not None:
                lit = _string_literal(k, src)
                keys.append(lit if lit is not None else node_text(k, src))
    return keys


def _chain(node: Node, src: bytes, scope: _Scope, *, through_calls: list[Node]) -> list[str] | None:
    """``a.b.c`` -> ["a", "b", "c"]; inner calls in object position are passed through and
    collected in ``through_calls``."""
    t = node.type
    if t == "identifier":
        return [node_text(node, src)]
    if t == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is None or attr is None:
            return None
        head = _chain(obj, src, scope, through_calls=through_calls)
        if head is None:
            return None
        return [*head, node_text(attr, src)]
    if t == "call":
        fn = node.child_by_field_name("function")
        if fn is None:
            return None
        through_calls.append(node)
        return _chain(fn, src, scope, through_calls=through_calls)
    if t in ("parenthesized_expression", "await"):
        inner = next((c for c in node.children if c.is_named), None)
        return _chain(inner, src, scope, through_calls=through_calls) if inner else None
    return None


def _resolve_root(chain: list[str], scope: _Scope) -> tuple[str, list[str]] | None:
    """Map the chain's root to a bound symbol: returns (base_symbol, remaining_segments)."""
    root = chain[0]
    if root in ("self", "cls") and len(chain) >= 2:
        key = f"{root}.{chain[1]}"
        if key in scope.instances:
            return scope.instances[key], chain[2:]
        if chain[1] in scope.instances:  # class-level ``client = WebClient(...)``
            return scope.instances[chain[1]], chain[2:]
        return None
    if root in scope.instances:
        return scope.instances[root], chain[1:]
    if root in scope.bindings:
        return scope.bindings[root], chain[1:]
    return None


def _symbol_for_call(call: Node, src: bytes, scope: _Scope) -> tuple[str, list[Node]] | None:
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    through: list[Node] = []
    chain = _chain(fn, src, scope, through_calls=through)
    if not chain:
        return None
    resolved = _resolve_root(chain, scope)
    if resolved is None:
        return None
    base, rest = resolved
    return ".".join([base, *rest]), through


def _target_key(node: Node, src: bytes) -> str | None:
    """Assignment target as an instance key: ``client`` or ``self.client``."""
    if node.type == "identifier":
        return node_text(node, src)
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is not None and attr is not None and obj.type == "identifier":
            o = node_text(obj, src)
            if o in ("self", "cls"):
                return f"{o}.{node_text(attr, src)}"
    return None


def _unwrap(node: Node) -> Node:
    while node.type in ("await", "parenthesized_expression"):
        inner = next((c for c in node.children if c.is_named), None)
        if inner is None:
            break
        node = inner
    return node


# --------------------------------------------------------------------------------------
# URL normalisation
# --------------------------------------------------------------------------------------


def join_url(base: str, rel: str) -> str:
    if rel.startswith(("http://", "https://")):
        return rel
    if not base:
        return rel
    return base.rstrip("/") + "/" + rel.lstrip("/")


def normalise_url(template: str) -> tuple[str, str] | None:
    """``https://API.Stripe.com:443/v1/charges/{id}?x=1`` -> ("api.stripe.com", "/v1/charges/{id}").
    Dynamic markers in the scheme or host make the URL unusable (None)."""
    if not template:
        return None
    m = _URL_RE.match(template)
    if m is None:
        return None
    authority, rest = m.group(2), m.group(3) or ""
    if _DYN in authority:
        return None
    if "@" in authority:  # userinfo is never part of the host
        authority = authority.rsplit("@", 1)[1]
    host, _, port = authority.partition(":")
    host = host.lower().rstrip(".")
    if not host:
        return None
    if port and port not in ("80", "443"):
        host = f"{host}:{port}"
    try:
        path = urlsplit("http://h" + rest).path
    except ValueError:
        path = rest.split("?", 1)[0].split("#", 1)[0]
    return host, normalise_path(path)


def normalise_path(path: str) -> str:
    segs = []
    for seg in path.split("/"):
        if not seg:
            continue
        if _DYN in seg or _is_id_segment(seg):
            segs.append("{id}")
        else:
            segs.append(seg)
    return "/" + "/".join(segs)


def _is_id_segment(seg: str) -> bool:
    return bool(
        _NUM_RE.match(seg)
        or _UUID_RE.match(seg)
        or _HEX_RE.match(seg)
        or _PREFIXED_ID_RE.match(seg)
    )


def _http_method_and_url(
    symbol: str, call: Node, src: bytes
) -> tuple[str | None, Node | None, str | None] | None:
    """For an HTTP-looking symbol return (METHOD, url_node, literal_method_or_None).

    METHOD is None when the call takes a non-literal method (``client.request(method,
    url)``): the call site is then recorded as the bare path, the matcher's any-method form.
    """
    prefix, _, method = symbol.rpartition(".")
    if prefix not in HTTP_MODULE_PREFIXES and prefix not in HTTP_CLIENT_CLASSES:
        return None
    pos = _positional_args(call)
    kws = _keyword_args(call, src)
    if method in HTTP_METHODS:
        url = kws.get("url") or (pos[0] if pos else None)
        return method.upper(), url, method.upper()
    if method in HTTP_GENERIC:
        m_node = kws.get("method") or (pos[0] if pos else None)
        m = _string_literal(m_node, src) if m_node is not None else None
        url = kws.get("url") or (pos[1] if len(pos) > 1 else None)
        return (m.upper() if m else None), url, (m.upper() if m else None)
    return None


# --------------------------------------------------------------------------------------
# Pass 1: bindings, instances, constants
# --------------------------------------------------------------------------------------


def _import_path_for(module: str) -> str | None:
    """Only imports of curated SDKs (any kind, http clients included) create bindings."""
    return module if sdk_map.lookup_python(module) is not None else None


def _collect_imports(root: Node, src: bytes, scope: _Scope) -> None:
    for node in walk(root):
        if node.type == "import_statement":
            for c in node.children:
                if c.type == "dotted_name":
                    module = node_text(c, src)
                    if _import_path_for(module):
                        local = module.split(".")[0]
                        scope.bindings[local] = local
                        scope.imported_roots[module] = sdk_map.python_import_root(module)
                elif c.type == "aliased_import":
                    name = c.child_by_field_name("name")
                    alias = c.child_by_field_name("alias")
                    if name is None or alias is None:
                        continue
                    module = node_text(name, src)
                    if _import_path_for(module):
                        scope.bindings[node_text(alias, src)] = module
                        scope.imported_roots[module] = sdk_map.python_import_root(module)
        elif node.type == "import_from_statement":
            mod = node.child_by_field_name("module_name")
            if mod is None or mod.type != "dotted_name":
                continue  # relative imports are local code
            module = node_text(mod, src)
            if not _import_path_for(module):
                continue
            scope.imported_roots[module] = sdk_map.python_import_root(module)
            for c in node.children:
                if c.type == "dotted_name" and c is not mod:
                    name = node_text(c, src)
                    scope.bindings[name] = f"{module}.{name}"
                elif c.type == "aliased_import":
                    name_n = c.child_by_field_name("name")
                    alias = c.child_by_field_name("alias")
                    if name_n is not None and alias is not None:
                        scope.bindings[node_text(alias, src)] = f"{module}.{node_text(name_n, src)}"
                elif c.type == "wildcard_import":
                    scope.bindings.setdefault("*", module)


def _record_instance(target: str, value: Node, src: bytes, scope: _Scope) -> None:
    value = _unwrap(value)
    if value.type != "call":
        return
    fn = value.child_by_field_name("function")
    if fn is not None and fn.type == "identifier" and node_text(fn, src) in scope.returns:
        scope.instances[target] = scope.returns[node_text(fn, src)]
        return
    resolved = _symbol_for_call(value, src, scope)
    if resolved is None:
        return
    symbol, _through = resolved
    if sdk_map.lookup_python(sdk_map.python_import_root(symbol)) is None:
        return
    kws = _keyword_args(value, src)
    pos = _positional_args(value)
    # boto3.client("s3") -> boto3.s3 : the service name is the useful part of the chain
    if symbol in (
        "boto3.client",
        "boto3.resource",
        "boto3.Session.client",
        "boto3.Session.resource",
    ):
        svc = _string_literal(pos[0], src) if pos else None
        if svc:
            symbol = f"boto3.{svc}"
    scope.instances[target] = symbol
    base = kws.get("base_url")
    if base is not None:
        tmpl = _template(base, src, scope)
        if tmpl:
            scope.base_urls[target] = tmpl


def _collect_instances(root: Node, src: bytes, scope: _Scope) -> None:
    for node in walk(root):
        t = node.type
        if t == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is None or right is None:
                continue
            key = _target_key(left, src)
            if key is None:
                continue
            lit = _string_literal(right, src)
            if lit is not None:
                # NAME = "literal" at any level; the first sighting wins (module constants
                # come first in file order, which is what f"{BASE}/path" usually refers to)
                scope.constants.setdefault(key.split(".")[-1], lit)
                continue
            if right.type in ("string", "concatenated_string", "binary_operator"):
                # url = f"{BASE}/pages/{page_id}" / BASE + "/x": keep the template
                tmpl = _template(right, src, scope)
                if tmpl is not None:
                    scope.constants.setdefault(key.split(".")[-1], tmpl)
                continue
            _record_instance(key, right, src, scope)
        elif t == "as_pattern":
            value = node.children[0] if node.children else None
            alias = node.child_by_field_name("alias")
            if value is None or alias is None:
                continue
            key = node_text(alias, src).strip()
            if key:
                _record_instance(key, value, src, scope)
        elif t == "function_definition":
            # def make_client() -> WebClient: ...  then  client = make_client()
            name = node.child_by_field_name("name")
            ret = node.child_by_field_name("return_type")
            if name is None or ret is None:
                continue
            inner = next((c for c in ret.children if c.is_named), None)
            if inner is None or inner.type not in ("identifier", "attribute"):
                continue
            chain = _chain(inner, src, scope, through_calls=[])
            if not chain:
                continue
            resolved = _resolve_root(chain, scope)
            if resolved is not None:
                base, rest = resolved
                scope.returns[node_text(name, src)] = ".".join([base, *rest])
        elif t in ("typed_parameter", "typed_default_parameter"):
            ann = node.child_by_field_name("type")
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            if ann is None or name_node is None:
                continue
            inner = next((c for c in ann.children if c.is_named), None)
            if inner is None or inner.type not in ("identifier", "attribute"):
                continue
            chain = _chain(inner, src, scope, through_calls=[])
            if not chain:
                continue
            resolved = _resolve_root(chain, scope)
            if resolved is None:
                continue
            base, rest = resolved
            scope.instances[node_text(name_node, src)] = ".".join([base, *rest])


# --------------------------------------------------------------------------------------
# Pass 2: calls
# --------------------------------------------------------------------------------------


def _strip_result_navigation(
    symbol: str, call: Node, through: list[Node], src: bytes
) -> tuple[str, Node]:
    """``stripe.Charge.list.auto_paging_iter`` composed through an inner call becomes
    ``stripe.Charge.list`` and the args come from that inner call."""
    if not through:
        return symbol, call
    segs = symbol.split(".")
    stripped = 0
    while len(segs) > 2 and segs[-1] in RESULT_NAVIGATION and stripped < len(through):
        segs.pop()
        stripped += 1
    if stripped == 0:
        return symbol, call
    return ".".join(segs), through[stripped - 1]


def _boto3_service_symbol(symbol: str, through: list[Node], src: bytes) -> str:
    """``boto3.client("s3").put_object`` -> ``boto3.s3.put_object``."""
    for prefix in (
        "boto3.client.",
        "boto3.resource.",
        "boto3.Session.client.",
        "boto3.Session.resource.",
    ):
        if symbol.startswith(prefix):
            for inner in reversed(through):
                pos = _positional_args(inner)
                svc = _string_literal(pos[0], src) if pos else None
                if svc:
                    return f"boto3.{svc}." + symbol[len(prefix) :]
    return symbol


def _is_constructor_like(symbol: str) -> bool:
    last = symbol.rsplit(".", 1)[-1]
    return bool(last) and last[0].isupper()


@dataclass
class _FileCtx:
    rel: str
    in_tests: bool
    result: ExtractResult


def _args_shape(call: Node, src: bytes) -> dict[str, Any]:
    kws = _keyword_args(call, src)
    keys = list(kws)
    shape: dict[str, Any] = {"keys": keys}
    pos = _positional_args(call)
    if not keys and len(pos) == 1 and pos[0].type == "dictionary":
        shape["keys"] = _dict_keys(pos[0], src)
    for body_kw in ("json", "data", "params"):
        node = kws.get(body_kw)
        if node is not None and node.type == "dictionary":
            shape.setdefault("body_keys", {})[body_kw] = _dict_keys(node, src)
    return shape


def _pypi_name_for(import_root: str, declared: dict[str, DeclaredPackage]) -> str | None:
    entry = sdk_map.lookup_python(import_root)
    if entry is None:
        return None
    candidates = {canonicalize_name(p): p for p in entry.pypi}
    parts = import_root.split(".")
    for n in range(len(parts), 0, -1):
        cand = canonicalize_name("-".join(parts[:n]))
        if cand in candidates:
            return candidates[cand]
        if cand in declared:
            return declared[cand].name
    # namespace packages (google.cloud.storage, azure.storage.blob): the distribution is
    # the dotted path with dashes
    if len(parts) > 1 and len(entry.pypi) > 1:
        return "-".join(parts)
    return entry.pypi[0] if entry.pypi else None


def _ensure_sdk_dependency(
    ctx: _FileCtx, import_root: str, declared: dict[str, DeclaredPackage]
) -> DependencyInfo | None:
    entry = sdk_map.lookup_python(import_root)
    if entry is None or not entry.watchable:
        return None
    name = _pypi_name_for(import_root, declared)
    if not name:
        return None
    dep = DependencyInfo(
        kind="pypi",
        name=name,
        vendor=entry.vendor,
        display_name=entry.display_name,
        meta={"language": "python", "sdk_kind": entry.kind, "import_root": import_root},
    )
    decl = declared.get(canonicalize_name(name))
    if decl is not None:
        dep.version_constraint = decl.constraint
        dep.meta["config_file"] = decl.config_file
        dep.meta["dev"] = decl.dev
    return ctx.result.add_dependency(dep)


def _record_http(
    ctx: _FileCtx,
    call: Node,
    src: bytes,
    scope: _Scope,
    symbol: str,
    chain_root: str | None,
) -> bool:
    info = _http_method_and_url(symbol, call, src)
    if info is None:
        return False
    method, url_node, _ = info
    if url_node is None:
        return True  # it is an HTTP call, but nothing literal to record
    tmpl = _template(url_node, src, scope)
    if tmpl is None:
        return True
    base = scope.base_urls.get(chain_root or "")
    if base and not tmpl.lower().startswith(("http://", "https://")):
        tmpl = join_url(base, tmpl)
    norm = normalise_url(tmpl)
    if norm is None:
        return True
    host, path = norm
    entry = sdk_map.vendor_for_host(host)
    dep = ctx.result.add_dependency(
        DependencyInfo(
            kind="http",
            name=host,
            vendor=entry.vendor if entry is not None else None,
            display_name=entry.display_name if entry is not None else host,
            meta={"host": host, "language": "python"},
        )
    )
    shape = _args_shape(call, src)
    ctx.result.call_sites.append(
        CallSiteInfo(
            dependency_kind="http",
            dependency_name=dep.name,
            file_path=ctx.rel,
            line=line_of(call),
            symbol=symbol,
            operation=f"{method} {path}" if method else path,
            args_shape=shape,
            in_tests=ctx.in_tests,
        )
    )
    return True


def _extract_calls(
    root: Node, src: bytes, scope: _Scope, ctx: _FileCtx, declared: dict[str, DeclaredPackage]
) -> None:
    for call in walk(root):
        if call.type != "call" or (call.start_byte, call.end_byte) in scope.suppressed:
            continue
        resolved = _symbol_for_call(call, src, scope)
        if resolved is None:
            continue
        symbol, through = resolved
        fn = call.child_by_field_name("function")
        chain_root = None
        if fn is not None:
            chain = _chain(fn, src, scope, through_calls=[])
            if chain:
                chain_root = chain[0] if chain[0] not in ("self", "cls") else ".".join(chain[:2])
        symbol = _boto3_service_symbol(symbol, through, src)
        if _record_http(ctx, call, src, scope, symbol, chain_root):
            for inner in through:
                scope.suppressed.add((inner.start_byte, inner.end_byte))
            continue
        import_root = sdk_map.python_import_root(symbol)
        dep = _ensure_sdk_dependency(ctx, import_root, declared)
        if dep is None or dep.vendor is None:
            continue  # unknown packages get a dependency row, never call sites
        last = symbol.rsplit(".", 1)[-1]
        if last in MCP_TOOL_CALL_NAMES:
            continue
        if _is_constructor_like(symbol):
            continue
        if "." not in symbol:
            continue
        if symbol in FACTORY_SYMBOLS:
            continue
        for inner in through:
            scope.suppressed.add((inner.start_byte, inner.end_byte))
        symbol, args_call = _strip_result_navigation(symbol, call, through, src)
        ctx.result.call_sites.append(
            CallSiteInfo(
                dependency_kind="pypi",
                dependency_name=dep.name,
                file_path=ctx.rel,
                line=line_of(call),
                symbol=symbol,
                operation=symbol,
                args_shape=_args_shape(args_call, src),
                in_tests=ctx.in_tests,
            )
        )


# --------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------


def extract_python_source(
    source: str | bytes,
    rel_path: str,
    *,
    declared: dict[str, DeclaredPackage] | None = None,
    result: ExtractResult | None = None,
) -> ExtractResult:
    """Extract from one file's source (unit-test friendly)."""
    result = result if result is not None else ExtractResult()
    declared = declared or {}
    tree, src = parse_source("python", source)
    if tree.root_node.has_error:
        result.stats.record_error(rel_path, "syntax errors (partial extraction)")
    scope = _Scope()
    _collect_imports(tree.root_node, src, scope)
    _collect_instances(tree.root_node, src, scope)
    ctx = _FileCtx(rel=rel_path, in_tests=is_test_path(rel_path), result=result)
    # imported SDKs are dependencies even without a call site
    for module in scope.imported_roots:
        _ensure_sdk_dependency(ctx, sdk_map.python_import_root(module), declared)
    _extract_calls(tree.root_node, src, scope, ctx, declared)
    result.stats.files_parsed += 1
    return result


def extract_python(root: str | Path, *, deadline: float | None = None) -> ExtractResult:
    """Walk the tree, parse every ``.py`` (tests included, flagged), return deps + call sites."""
    root = Path(root)
    result = ExtractResult()
    declared_list = declared_python_packages(root)
    declared: dict[str, DeclaredPackage] = {}
    for d in declared_list:
        prev = declared.get(d.canonical)
        if prev is None or (prev.dev and not d.dev):
            declared[d.canonical] = d
    for d in declared.values():
        if d.canonical in TOOLING_PACKAGES or d.canonical.startswith("types-"):
            continue
        entry = sdk_map.lookup_pypi(d.canonical)
        if entry is not None and not entry.watchable:
            continue  # requests / httpx: the host is the dependency
        name = d.name
        if entry is not None:  # spell it the way the curated map (and the seed) does
            name = next((p for p in entry.pypi if canonicalize_name(p) == d.canonical), d.name)
        dep = DependencyInfo(
            kind="pypi",
            name=name,
            vendor=entry.vendor if entry is not None else None,
            display_name=entry.display_name if entry is not None else d.name,
            version_constraint=d.constraint,
            meta={
                "language": "python",
                "config_file": d.config_file,
                "dev": d.dev,
                "declared": True,
            },
        )
        if entry is not None:
            dep.meta["sdk_kind"] = entry.kind
        result.add_dependency(dep)
    for path, rel, too_large in iter_repo_files(root, (".py",)):
        if deadline is not None and time.monotonic() > deadline:
            raise IndexBudgetExceeded(f"python extraction ran out of budget at {rel}")
        result.stats.files_seen += 1
        if too_large:
            result.stats.record_skipped(rel)
            continue
        text = read_text(path)
        if text is None:
            result.stats.record_error(rel, "unreadable or binary")
            continue
        try:
            extract_python_source(text, rel, declared=declared, result=result)
        except IndexBudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001 - one bad file must not sink the index
            log.warning("python extraction failed for %s: %s", rel, e)
            result.stats.record_error(rel, f"{type(e).__name__}: {e}")
    _dedupe_declared_names(result)
    return result


def _dedupe_declared_names(result: ExtractResult) -> None:
    """``slack_sdk`` (import) and ``slack-sdk`` (declared) are one package; keep one row."""
    by_canon: dict[str, DependencyInfo] = {}
    keep: list[DependencyInfo] = []
    rename: dict[str, str] = {}
    for dep in result.dependencies:
        if dep.kind != "pypi":
            keep.append(dep)
            continue
        canon = canonicalize_name(dep.name)
        prev = by_canon.get(canon)
        if prev is None:
            by_canon[canon] = dep
            keep.append(dep)
        else:
            prev.merge_from(dep)
            rename[dep.name] = prev.name
    result.dependencies = keep
    if rename:
        for cs in result.call_sites:
            if cs.dependency_kind == "pypi" and cs.dependency_name in rename:
                cs.dependency_name = rename[cs.dependency_name]


__all__ = [
    "DeclaredPackage",
    "ExtractResult",
    "ExtractStats",
    "declared_python_packages",
    "extract_python",
    "extract_python_source",
    "join_url",
    "normalise_path",
    "normalise_url",
]
