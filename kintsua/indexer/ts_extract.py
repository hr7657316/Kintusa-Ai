"""TypeScript / JavaScript extractor (``.ts .tsx .js .mjs .cjs .jsx``) with the same shape as
the Python one: ``package.json`` for declared packages (``devDependencies`` flagged),
``import`` / ``require`` for bindings, ``new Stripe(...)`` instance tracking, member-call
chains for SDK call sites (``stripe.charges.create({...})`` -> symbol ``stripe.charges.create``,
``args_shape.keys`` from the object literal) and ``fetch`` / ``axios`` / ``got`` / ``ky`` /
``undici`` raw HTTP with string, template-literal (literal host prefix) or ``const``-resolved
URLs, including ``axios.create({baseURL})`` / ``got.extend({prefixUrl})`` instances.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from kintsua.indexer import sdk_map
from kintsua.indexer.python_extract import join_url, normalise_url
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

_DYN = "\x00"
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
FETCH_PACKAGES = frozenset({"node-fetch", "cross-fetch", "undici"})
HTTP_PACKAGES = frozenset({"axios", "got", "ky", "undici", "node-fetch", "cross-fetch"})
FACTORY_SYMBOLS = frozenset({"axios.create", "got.extend", "ky.create", "ky.extend"})
MCP_TOOL_CALL_NAMES = frozenset({"callTool"})
RESULT_NAVIGATION = frozenset({"then", "catch", "finally", "json", "text", "data"})
SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".mjs", ".cjs", ".jsx")

TOOLING_PACKAGES = frozenset(
    {
        "typescript",
        "eslint",
        "prettier",
        "jest",
        "ts-jest",
        "vitest",
        "mocha",
        "chai",
        "ts-node",
        "tsx",
        "nodemon",
        "husky",
        "lint-staged",
        "rimraf",
        "tsup",
        "esbuild",
        "rollup",
        "vite",
        "webpack",
        "babel",
        "@babel/core",
        "@babel/preset-env",
        "npm-run-all",
        "concurrently",
        "cross-env",
        "dotenv",
        "turbo",
        "nx",
        "lerna",
        "@typescript-eslint/parser",
        "@typescript-eslint/eslint-plugin",
        "eslint-config-prettier",
        "eslint-plugin-import",
    }
)


# --------------------------------------------------------------------------------------
# package.json
# --------------------------------------------------------------------------------------


@dataclass
class DeclaredNpm:
    name: str
    constraint: str | None
    config_file: str
    dev: bool = False


def declared_npm_packages(root: str | Path) -> list[DeclaredNpm]:
    root = Path(root)
    out: list[DeclaredNpm] = []
    for p, rel, too_large in iter_repo_files(root, names=("package.json",), max_bytes=512_000):
        if too_large:
            continue
        try:
            data = json.loads(read_text(p) or "")
        except (json.JSONDecodeError, ValueError) as e:
            log.info("package.json %s unreadable: %s", rel, e)
            continue
        if not isinstance(data, dict):
            continue
        for section, dev in (
            ("dependencies", False),
            ("optionalDependencies", False),
            ("devDependencies", True),
        ):
            deps = data.get(section)
            if not isinstance(deps, dict):
                continue
            for name, spec in deps.items():
                if not isinstance(name, str) or not name.strip():
                    continue
                out.append(
                    DeclaredNpm(
                        name.strip(),
                        str(spec) if isinstance(spec, str) and spec else None,
                        rel,
                        dev,
                    )
                )
    return out


# --------------------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------------------


@dataclass
class _Scope:
    bindings: dict[str, str] = field(default_factory=dict)  # local -> "pkg" | "pkg.Name"
    instances: dict[str, str] = field(default_factory=dict)  # var / this.x -> symbol
    base_urls: dict[str, str] = field(default_factory=dict)
    constants: dict[str, str] = field(default_factory=dict)
    packages: set[str] = field(default_factory=set)  # imported known packages
    suppressed: set[tuple[int, int]] = field(default_factory=set)


_REQUIRE = "\x01require:"


def _string_literal(node: Node | None, src: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        return "".join(
            node_text(c, src) if c.type == "string_fragment" else "" for c in node.children
        )
    if node.type == "template_string" and not any(
        c.type == "template_substitution" for c in node.children
    ):
        return "".join(node_text(c, src) for c in node.children if c.type == "string_fragment")
    return None


def _template(node: Node | None, src: bytes, scope: _Scope, depth: int = 0) -> str | None:
    if node is None or depth > 6:
        return None
    t = node.type
    if t == "string":
        return _string_literal(node, src)
    if t == "template_string":
        parts: list[str] = []
        for c in node.children:
            if c.type == "string_fragment":
                parts.append(node_text(c, src))
            elif c.type == "template_substitution":
                expr = next((x for x in c.children if x.is_named), None)
                inner = _template(expr, src, scope, depth + 1)
                parts.append(inner if inner is not None else _DYN)
        return "".join(parts)
    if t == "parenthesized_expression":
        inner = next((c for c in node.children if c.is_named), None)
        return _template(inner, src, scope, depth + 1)
    if t in ("as_expression", "non_null_expression", "satisfies_expression"):
        return _template(node.children[0] if node.children else None, src, scope, depth + 1)
    if t == "identifier":
        return scope.constants.get(node_text(node, src))
    if t == "member_expression":
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        if obj is not None and prop is not None and obj.type == "this":
            return scope.constants.get(node_text(prop, src))
        return None
    if t == "binary_expression":
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
        if ops in ("??", "||"):
            # process.env.API_URL ?? "https://api.x.com" -> the literal fallback is the host
            lt = _template(left, src, scope, depth + 1)
            return lt if lt is not None else _template(right, src, scope, depth + 1)
        return None
    if t == "new_expression":
        ctor = node.child_by_field_name("constructor")
        if ctor is not None and node_text(ctor, src) == "URL":
            args = _positional_args(node)
            if args:
                a = _template(args[0], src, scope, depth + 1)
                if a is None:
                    return None
                if len(args) > 1:
                    b = _template(args[1], src, scope, depth + 1)
                    if b is not None and not a.lower().startswith(("http://", "https://")):
                        return join_url(b, a)
                return a
        return None
    return None


def _positional_args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    if args is None:
        return []
    return [c for c in args.children if c.is_named and c.type not in ("comment", "spread_element")]


def _object_keys(node: Node | None, src: bytes) -> list[str]:
    keys: list[str] = []
    if node is None or node.type != "object":
        return keys
    for c in node.children:
        if c.type == "pair":
            k = c.child_by_field_name("key")
            if k is None:
                continue
            lit = _string_literal(k, src)
            keys.append(lit if lit is not None else node_text(k, src))
        elif c.type in ("shorthand_property_identifier", "method_definition"):
            name = c.child_by_field_name("name") if c.type == "method_definition" else c
            if name is not None:
                keys.append(node_text(name, src))
    return keys


def _object_value(node: Node | None, key: str, src: bytes) -> Node | None:
    if node is None or node.type != "object":
        return None
    for c in node.children:
        if c.type == "pair":
            k = c.child_by_field_name("key")
            if k is not None and (_string_literal(k, src) or node_text(k, src)) == key:
                return c.child_by_field_name("value")
    return None


def _require_package(node: Node, src: bytes) -> str | None:
    if node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    if fn is None or fn.type != "identifier" or node_text(fn, src) != "require":
        return None
    args = _positional_args(node)
    return _string_literal(args[0], src) if args else None


def _chain(node: Node, src: bytes, through: list[Node]) -> list[str] | None:
    t = node.type
    if t == "identifier":
        return [node_text(node, src)]
    if t == "this":
        return ["this"]
    if t == "member_expression":
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        if obj is None or prop is None:
            return None
        head = _chain(obj, src, through)
        if head is None:
            return None
        return [*head, node_text(prop, src)]
    if t == "call_expression":
        pkg = _require_package(node, src)
        if pkg is not None:
            return [_REQUIRE + pkg]
        fn = node.child_by_field_name("function")
        if fn is None:
            return None
        through.append(node)
        return _chain(fn, src, through)
    if t == "new_expression":
        ctor = node.child_by_field_name("constructor")
        return _chain(ctor, src, through) if ctor is not None else None
    if t in (
        "await_expression",
        "parenthesized_expression",
        "non_null_expression",
        "as_expression",
        "satisfies_expression",
        "type_assertion",
    ):
        inner = next((c for c in node.children if c.is_named and c.type != "type"), None)
        return _chain(inner, src, through) if inner is not None else None
    return None


def _resolve_root(chain: list[str], scope: _Scope) -> tuple[str, list[str]] | None:
    root = chain[0]
    if root.startswith(_REQUIRE):
        pkg = sdk_map.npm_package_root(root[len(_REQUIRE) :])
        if sdk_map.lookup_npm(pkg) is None:
            return None
        return pkg, chain[1:]
    if root == "this" and len(chain) >= 2:
        key = f"this.{chain[1]}"
        if key in scope.instances:
            return scope.instances[key], chain[2:]
        if chain[1] in scope.instances:
            return scope.instances[chain[1]], chain[2:]
        return None
    if root in scope.instances:
        return scope.instances[root], chain[1:]
    if root in scope.bindings:
        return scope.bindings[root], chain[1:]
    return None


def _symbol_for(node: Node, src: bytes, scope: _Scope) -> tuple[str, list[Node]] | None:
    through: list[Node] = []
    chain = _chain(node, src, through)
    if not chain:
        return None
    resolved = _resolve_root(chain, scope)
    if resolved is None:
        return None
    base, rest = resolved
    return ".".join([base, *rest]), through


def _package_of(symbol: str) -> str:
    """``@slack/web-api.WebClient.chat`` -> ``@slack/web-api``; ``stripe.charges`` -> ``stripe``."""
    if symbol.startswith("@"):
        scope, _, rest = symbol.partition("/")
        return scope + "/" + rest.split(".", 1)[0]
    return symbol.split(".", 1)[0]


# --------------------------------------------------------------------------------------
# Pass 1
# --------------------------------------------------------------------------------------


def _package_for_source(source: str) -> str | None:
    if not source or source.startswith((".", "/", "node:", "#", "~")):
        return None
    pkg = sdk_map.npm_package_root(source)
    return pkg if sdk_map.lookup_npm(pkg) is not None else None


def _collect_imports(root: Node, src: bytes, scope: _Scope) -> None:
    for node in walk(root):
        if node.type == "import_statement":
            source = node.child_by_field_name("source")
            pkg = _package_for_source(_string_literal(source, src) or "")
            if pkg is None:
                continue
            scope.packages.add(pkg)
            clause = next((c for c in node.children if c.type == "import_clause"), None)
            if clause is None:
                continue
            for c in clause.children:
                if c.type == "identifier":  # default import
                    scope.bindings[node_text(c, src)] = pkg
                elif c.type == "namespace_import":
                    ident = next((x for x in c.children if x.type == "identifier"), None)
                    if ident is not None:
                        scope.bindings[node_text(ident, src)] = pkg
                elif c.type == "named_imports":
                    for spec in c.children:
                        if spec.type != "import_specifier":
                            continue
                        name = spec.child_by_field_name("name")
                        alias = spec.child_by_field_name("alias")
                        if name is None:
                            continue
                        local = node_text(alias if alias is not None else name, src)
                        scope.bindings[local] = f"{pkg}.{node_text(name, src)}"
        elif node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if name is None or value is None:
                continue
            value = _unwrap(value)
            req = _require_package(value, src)
            if req is None:
                continue
            pkg = _package_for_source(req)
            if pkg is None:
                continue
            scope.packages.add(pkg)
            if name.type == "identifier":
                scope.bindings[node_text(name, src)] = pkg
            elif name.type == "object_pattern":
                for c in name.children:
                    if c.type == "shorthand_property_identifier_pattern":
                        n = node_text(c, src)
                        scope.bindings[n] = f"{pkg}.{n}"
                    elif c.type == "pair_pattern":
                        k = c.child_by_field_name("key")
                        v = c.child_by_field_name("value")
                        if k is not None and v is not None and v.type == "identifier":
                            scope.bindings[node_text(v, src)] = f"{pkg}.{node_text(k, src)}"


def _unwrap(node: Node) -> Node:
    while node.type in (
        "await_expression",
        "parenthesized_expression",
        "non_null_expression",
        "as_expression",
        "satisfies_expression",
    ):
        inner = next((c for c in node.children if c.is_named and c.type != "type"), None)
        if inner is None:
            break
        node = inner
    return node


def _record_instance(key: str, value: Node, src: bytes, scope: _Scope) -> None:
    value = _unwrap(value)
    if value.type not in ("new_expression", "call_expression"):
        return
    resolved = _symbol_for(value, src, scope)
    if resolved is None:
        return
    symbol, _ = resolved
    scope.instances[key] = symbol
    args = _positional_args(value)
    for a in args:
        if a.type == "object":
            for base_key in ("baseURL", "baseUrl", "prefixUrl", "base_url"):
                v = _object_value(a, base_key, src)
                if v is not None:
                    tmpl = _template(v, src, scope)
                    if tmpl:
                        scope.base_urls[key] = tmpl
                    break


def _collect_instances(root: Node, src: bytes, scope: _Scope) -> None:
    for node in walk(root):
        t = node.type
        if t == "variable_declarator":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if name is None or value is None or name.type != "identifier":
                continue
            key = node_text(name, src)
            lit = _template(value, src, scope) if value.type != "identifier" else None
            if lit is not None and _DYN not in lit and value.type != "new_expression":
                scope.constants.setdefault(key, lit)
                continue
            _record_instance(key, value, src, scope)
        elif t == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is None or right is None:
                continue
            if left.type == "member_expression":
                obj = left.child_by_field_name("object")
                prop = left.child_by_field_name("property")
                if obj is None or prop is None or obj.type != "this":
                    continue
                key = f"this.{node_text(prop, src)}"
            elif left.type == "identifier":
                key = node_text(left, src)
            else:
                continue
            lit = _string_literal(right, src)
            if lit is not None:
                scope.constants.setdefault(key.split(".")[-1], lit)
                continue
            _record_instance(key, right, src, scope)
        elif t == "public_field_definition":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if name is None or value is None:
                continue
            key = node_text(name, src)
            lit = _string_literal(value, src)
            if lit is not None:
                scope.constants.setdefault(key, lit)
                continue
            _record_instance(key, value, src, scope)
            if key in scope.instances:
                scope.instances[f"this.{key}"] = scope.instances[key]
                if key in scope.base_urls:
                    scope.base_urls[f"this.{key}"] = scope.base_urls[key]


# --------------------------------------------------------------------------------------
# Pass 2
# --------------------------------------------------------------------------------------


@dataclass
class _FileCtx:
    rel: str
    in_tests: bool
    result: ExtractResult
    declared: dict[str, DeclaredNpm]


def _ensure_dependency(ctx: _FileCtx, package: str) -> DependencyInfo | None:
    entry = sdk_map.lookup_npm(package)
    if entry is None or not entry.watchable:
        return None
    dep = DependencyInfo(
        kind="npm",
        name=package,
        vendor=entry.vendor,
        display_name=entry.display_name,
        meta={"language": "typescript", "sdk_kind": entry.kind},
    )
    decl = ctx.declared.get(package.lower())
    if decl is not None:
        dep.version_constraint = decl.constraint
        dep.meta["config_file"] = decl.config_file
        dep.meta["dev"] = decl.dev
    return ctx.result.add_dependency(dep)


def _args_shape(call: Node, src: bytes) -> dict[str, Any]:
    pos = _positional_args(call)
    obj = next((a for a in pos if a.type == "object"), None)
    return {"keys": _object_keys(obj, src)}


def _http_call(
    symbol: str, call: Node, src: bytes, scope: _Scope, chain_root: str | None
) -> tuple[str | None, str | None, Node | None] | None:
    """(METHOD, url_template, options_object) for fetch / axios / got / ky / undici calls.

    METHOD is None when the options name a ``method`` that is not a literal (``{method}``,
    ``{method: m}``): the call site is then recorded as the bare path (any method)."""
    pos = _positional_args(call)
    pkg = _package_of(symbol)
    tail = symbol[len(pkg) :].lstrip(".")
    segs = tail.split(".") if tail else []
    opts = next((a for a in pos if a.type == "object"), None)

    def method_from(opts_node: Node | None, default: str) -> str | None:
        if "method" not in _object_keys(opts_node, src):
            return default
        m = _string_literal(_object_value(opts_node, "method", src), src)
        return m.upper() if m else None

    if symbol == "fetch" or pkg in FETCH_PACKAGES and segs in ([], ["default"], ["fetch"]):
        url = _template(pos[0], src, scope) if pos else None
        return method_from(opts, "GET"), url, opts
    if pkg == "undici" and segs == ["request"]:
        url = _template(pos[0], src, scope) if pos else None
        return method_from(opts, "GET"), url, opts
    if pkg not in ("axios", "got", "ky"):
        return None
    last = segs[-1] if segs else ""
    base = scope.base_urls.get(chain_root or "")
    if last in HTTP_METHODS:
        url = _template(pos[0], src, scope) if pos else None
        if url is not None and base and not url.lower().startswith(("http://", "https://")):
            url = join_url(base, url)
        return last.upper(), url, opts
    if last in ("", "request", "default") and (pkg != "got" or last != "default"):
        # axios(url, cfg) | axios({url, method, baseURL}) | got(url, {method}) | ky(url, {method})
        url = None
        if pos and pos[0].type != "object":
            url = _template(pos[0], src, scope)
        elif opts is not None:
            url = _template(_object_value(opts, "url", src), src, scope)
            cfg_base = _template(_object_value(opts, "baseURL", src), src, scope)
            if url is not None and cfg_base and not url.lower().startswith(("http://", "https://")):
                url = join_url(cfg_base, url)
        if url is not None and base and not url.lower().startswith(("http://", "https://")):
            url = join_url(base, url)
        return method_from(opts, "GET"), url, opts
    return None


def _record_http(
    ctx: _FileCtx, call: Node, src: bytes, scope: _Scope, symbol: str, chain_root: str | None
) -> bool:
    info = _http_call(symbol, call, src, scope, chain_root)
    if info is None:
        return False
    method, tmpl, opts = info
    if tmpl is None:
        return True
    norm = normalise_url(tmpl)
    if norm is None:
        return True
    host, path = norm
    entry = sdk_map.vendor_for_host(host)
    ctx.result.add_dependency(
        DependencyInfo(
            kind="http",
            name=host,
            vendor=entry.vendor if entry is not None else None,
            display_name=entry.display_name if entry is not None else host,
            meta={"host": host, "language": "typescript"},
        )
    )
    shape: dict[str, Any] = {"keys": _object_keys(opts, src)}
    body = _object_value(opts, "body", src) if opts is not None else None
    if body is None and opts is not None:
        body = _object_value(opts, "data", src) or _object_value(opts, "json", src)
    pos = _positional_args(call)
    if body is None and symbol.rsplit(".", 1)[-1] in ("post", "put", "patch") and len(pos) > 1:
        # axios.post(url, data, config): the second positional argument is the body
        body = pos[1] if pos[1].type == "object" and pos[1] is not opts else None
        if body is not None and opts is not None and body.start_byte == opts.start_byte:
            body = None
    if body is not None and body.type == "object":
        shape["body_keys"] = _object_keys(body, src)
    ctx.result.call_sites.append(
        CallSiteInfo(
            dependency_kind="http",
            dependency_name=host,
            file_path=ctx.rel,
            line=line_of(call),
            symbol=symbol,
            operation=f"{method} {path}" if method else path,
            args_shape=shape,
            in_tests=ctx.in_tests,
        )
    )
    return True


def _strip_navigation(symbol: str, call: Node, through: list[Node]) -> tuple[str, Node]:
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


def _extract_calls(root: Node, src: bytes, scope: _Scope, ctx: _FileCtx) -> None:
    for call in walk(root):
        if call.type != "call_expression":
            continue
        if (call.start_byte, call.end_byte) in scope.suppressed:
            continue
        fn = call.child_by_field_name("function")
        if fn is None:
            continue
        # global fetch()
        if (
            fn.type == "identifier"
            and node_text(fn, src) == "fetch"
            and "fetch" not in scope.bindings
        ):
            _record_http(ctx, call, src, scope, "fetch", None)
            continue
        resolved = _symbol_for(fn, src, scope)
        if resolved is None:
            continue
        symbol, through = resolved
        chain = _chain(fn, src, []) or []
        chain_root = None
        if chain:
            chain_root = chain[0] if chain[0] != "this" else ".".join(chain[:2])
        pkg = _package_of(symbol)
        entry = sdk_map.lookup_npm(pkg)
        if entry is not None and entry.kind == "http-client":
            if _record_http(ctx, call, src, scope, symbol, chain_root):
                for inner in through:
                    scope.suppressed.add((inner.start_byte, inner.end_byte))
            continue
        dep = _ensure_dependency(ctx, pkg)
        if dep is None:
            continue
        if symbol.rsplit(".", 1)[-1] in MCP_TOOL_CALL_NAMES:
            continue
        if symbol in FACTORY_SYMBOLS or symbol == pkg:
            continue
        for inner in through:
            scope.suppressed.add((inner.start_byte, inner.end_byte))
        symbol, args_call = _strip_navigation(symbol, call, through)
        ctx.result.call_sites.append(
            CallSiteInfo(
                dependency_kind="npm",
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


def extract_ts_source(
    source: str | bytes,
    rel_path: str,
    *,
    declared: dict[str, DeclaredNpm] | None = None,
    result: ExtractResult | None = None,
) -> ExtractResult:
    result = result if result is not None else ExtractResult()
    lang = language_for_path(rel_path) or "typescript"
    if lang == "python":
        lang = "typescript"
    tree, src = parse_source(lang, source)
    if tree.root_node.has_error:
        result.stats.record_error(rel_path, "syntax errors (partial extraction)")
    scope = _Scope()
    _collect_imports(tree.root_node, src, scope)
    _collect_instances(tree.root_node, src, scope)
    ctx = _FileCtx(
        rel=rel_path, in_tests=is_test_path(rel_path), result=result, declared=declared or {}
    )
    for pkg in sorted(scope.packages):
        _ensure_dependency(ctx, pkg)
    _extract_calls(tree.root_node, src, scope, ctx)
    result.stats.files_parsed += 1
    return result


def extract_ts(root: str | Path, *, deadline: float | None = None) -> ExtractResult:
    root = Path(root)
    result = ExtractResult()
    declared: dict[str, DeclaredNpm] = {}
    for d in declared_npm_packages(root):
        prev = declared.get(d.name.lower())
        if prev is None or (prev.dev and not d.dev):
            declared[d.name.lower()] = d
    for d in declared.values():
        if d.name.lower() in TOOLING_PACKAGES or d.name.startswith("@types/"):
            continue
        entry = sdk_map.lookup_npm(d.name)
        if entry is not None and not entry.watchable:
            continue
        dep = DependencyInfo(
            kind="npm",
            name=d.name,
            vendor=entry.vendor if entry is not None else None,
            display_name=entry.display_name if entry is not None else d.name,
            version_constraint=d.constraint,
            meta={
                "language": "typescript",
                "config_file": d.config_file,
                "dev": d.dev,
                "declared": True,
            },
        )
        if entry is not None:
            dep.meta["sdk_kind"] = entry.kind
        result.add_dependency(dep)
    for path, rel, too_large in iter_repo_files(root, SUFFIXES):
        if deadline is not None and time.monotonic() > deadline:
            raise IndexBudgetExceeded(f"typescript extraction ran out of budget at {rel}")
        if rel.endswith((".min.js", ".d.ts", ".d.mts", ".d.cts")):
            continue
        result.stats.files_seen += 1
        if too_large:
            result.stats.record_skipped(rel)
            continue
        text = read_text(path)
        if text is None:
            result.stats.record_error(rel, "unreadable or binary")
            continue
        try:
            extract_ts_source(text, rel, declared=declared, result=result)
        except IndexBudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("typescript extraction failed for %s: %s", rel, e)
            result.stats.record_error(rel, f"{type(e).__name__}: {e}")
    return result


__all__ = ["DeclaredNpm", "declared_npm_packages", "extract_ts", "extract_ts_source"]
