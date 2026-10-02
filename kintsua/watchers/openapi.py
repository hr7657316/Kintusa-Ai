"""OpenAPI watcher: fetch a spec (OpenAPI 3.x, Swagger 2.0 or a Google API Discovery
document), normalise it and diff it operation by operation.

Part of the open-source core: pydantic, httpx, pyyaml and stdlib only.

Canonical body::

    {"info": {"title", "version"},
     "paths": {"/v1/charges": {"post": {operationId, summary, description, deprecated,
                                        parameters: [...], requestBody: {...},
                                        responses: {...}, security: [...]}}},
     "components": {"schemas": {...}, "securitySchemes": {...}},
     "security": [...]}

Normalisation: local ``$ref``s inside ``paths`` are dereferenced (structural refs to
``components/parameters|requestBodies|responses|headers`` always; schema refs up to
``meta["deref_depth"]`` levels, default 1, cycle-safe: a ref already on the stack is kept
as its ``$ref`` string); ``x-*`` vendor extensions (except ``x-deprecated``), ``example(s)``,
``externalDocs`` and ``servers`` are dropped; keys are sorted. ``components.schemas`` keeps
its refs so deep changes are reported once, at ``/components/schemas/<name>/...``.

Diff (native walker, always available): ``removed`` / ``added`` operations, parameter and
schema ops from ``diff_json_schema`` (``required_added``, ``type_changed``, ``enum_changed``,
``removed`` ...; a schema edit it does not model, such as a new ``default``, is
``text_changed``), ``auth_changed`` when a security scheme's type, location or scope set or
a ``security`` requirement set changes (wording and order are ``text_changed`` / nothing),
``deprecated``, ``text_changed`` for summary/description-only changes and ``renamed`` when
a removed and an added sibling share an identical distinctive schema or a Levenshtein
distance under 3 (never across HTTP methods, status codes or media types). The response
body is capped at ``meta["max_bytes"]`` and YAML alias bombs are refused; guarded sources
(``meta["_net_guard"]``) go through ``kintsua.core.netguard``. Optionally, when the ``oasdiff``
binary is on PATH and ``KINTSUA_USE_OASDIFF=1``, its changelog ids are mapped into ops and
merged (deduplicated by op+path) with the native result.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any

import httpx
import yaml

from kintsua.core.events import (
    CanonicalDoc,
    ChangeOp,
    json_pointer,
    json_pointer_escape,
    split_pointer,
)
from kintsua.watchers._http import client_kwargs, get_bounded, guard_url, max_bytes_for
from kintsua.watchers.base import BaseWatcher, SourceRef, register, sort_keys
from kintsua.watchers.schemadiff import (
    diff_json_schema,
    levenshtein,
    residual_schema_ops,
    schema_summary,
)

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "kintsua-watcher/0.1 (+https://github.com/hr7657316/Kintusa-Ai)"
DEFAULT_TIMEOUT_S = 60.0
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
DROP_KEYS = frozenset({"example", "examples", "externalDocs", "servers"})
# dict keys under which the *keys* are user data (property names, paths...) and must not be
# treated as vendor extensions / dropped
NAME_MAPS = frozenset(
    {
        "properties",
        "patternProperties",
        "schemas",
        "definitions",
        "$defs",
        "paths",
        "responses",
        "securitySchemes",
        "requestBodies",
        "headers",
        "links",
        "callbacks",
        "content",
        "scopes",
        "mapping",
        "variables",
        "encoding",
        "x-kintsua-params",
    }
)
STRUCTURAL_REF_PREFIXES = (
    "#/components/parameters/",
    "#/components/requestBodies/",
    "#/components/responses/",
    "#/components/headers/",
    "#/parameters/",
    "#/responses/",
)
RENAME_MAX_DISTANCE = 3
# YAML alias-bomb guard (see _check_expansion)
YAML_MAX_EXPANDED_NODES = 20_000_000
YAML_MAX_SHARED_NODES = 1_000_000
YAML_MAX_EXPANSION_RATIO = 10


class SpecFormatError(ValueError):
    """The fetched document is not an OpenAPI / Swagger / Discovery document."""


# --------------------------------------------------------------------------------------
# Fetch + parse
# --------------------------------------------------------------------------------------


def parse_spec_text(text: str) -> dict[str, Any]:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(stripped)
        except json.JSONDecodeError:
            doc = _yaml_load(stripped)
    else:
        doc = _yaml_load(stripped)
    if not isinstance(doc, dict):
        raise SpecFormatError("document is not a JSON/YAML object")
    return doc


def _yaml_load(text: str) -> Any:
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    doc = yaml.load(text, Loader=loader)  # noqa: S506 - SafeLoader variant
    _check_expansion(doc)
    return doc


def _check_expansion(doc: Any) -> None:
    """Refuse YAML whose aliases expand far beyond the document itself (a "billion laughs"
    alias bomb loads small, because aliased nodes are shared, and explodes when the spec is
    canonicalised or serialised). Legit anchors re-use a little; a bomb re-uses enormously."""
    sizes: dict[int, int] = {}
    unique = 0  # nodes as written (each container once, plus its scalar children)

    def size(node: Any) -> int:
        nonlocal unique
        if not isinstance(node, dict | list):
            return 1
        key = id(node)
        if key in sizes:  # an aliased container: its full expanded size again
            return sizes[key]
        children = list(node.values()) if isinstance(node, dict) else node
        unique += 1 + sum(1 for c in children if not isinstance(c, dict | list))
        sizes[key] = 1  # a recursive alias counts once
        total = 1 + sum(size(c) for c in children)
        sizes[key] = total
        return total

    try:
        expanded = size(doc)
    except RecursionError as e:
        raise SpecFormatError("YAML document is nested too deeply") from e
    if expanded > YAML_MAX_EXPANDED_NODES or (
        expanded > YAML_MAX_SHARED_NODES and expanded > YAML_MAX_EXPANSION_RATIO * max(unique, 1)
    ):
        raise SpecFormatError(
            f"YAML aliases expand {unique} nodes to {expanded}; refusing the document"
        )


def detect_format(doc: dict[str, Any], meta: dict[str, Any] | None = None) -> str:
    fmt = str((meta or {}).get("format") or "").lower()
    if fmt in ("discovery", "swagger2", "openapi3", "openapi"):
        return {"openapi": "openapi3"}.get(fmt, fmt)
    if "discoveryVersion" in doc or (doc.get("kind") == "discovery#restDescription"):
        return "discovery"
    if str(doc.get("swagger", "")).startswith("2"):
        return "swagger2"
    if str(doc.get("openapi", "")).startswith("3"):
        return "openapi3"
    if "paths" in doc and "info" in doc:
        return "openapi3"
    raise SpecFormatError(
        "not an OpenAPI 3.x, Swagger 2.0 or Google Discovery document "
        f"(top-level keys: {sorted(doc)[:8]})"
    )


def to_openapi3(doc: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    fmt = detect_format(doc, meta)
    if fmt == "discovery":
        return discovery_to_openapi3(doc)
    if fmt == "swagger2":
        return swagger2_to_openapi3(doc)
    return doc


def canonicalise(doc: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convert, dereference, drop volatile keys and sort -> canonical body."""
    meta = meta or {}
    doc3 = to_openapi3(doc, meta)
    depth = int(meta.get("deref_depth", 1))
    components = doc3.get("components") if isinstance(doc3.get("components"), dict) else {}
    paths = dereference(doc3.get("paths") or {}, doc3, max_schema_depth=depth)
    info = doc3.get("info") if isinstance(doc3.get("info"), dict) else {}
    body = {
        "info": {
            "title": info.get("title"),
            "version": str(info["version"]) if info.get("version") is not None else None,
        },
        "paths": clean(paths, name_map=True),
        "components": {
            "schemas": clean(components.get("schemas") or {}, name_map=True),
            "securitySchemes": clean(components.get("securitySchemes") or {}, name_map=True),
        },
        "security": clean(doc3.get("security") or []),
    }
    return sort_keys(body)


async def fetch_spec(
    url: str, meta: dict[str, Any] | None = None, *, timeout_s: float | None = None
) -> dict[str, Any]:
    """GET + parse. Guarded sources (``meta["_net_guard"]``) have the URL and every
    redirect hop checked by ``kintsua.core.netguard``; the body is capped at
    ``meta["max_bytes"]`` (default 32 MB)."""
    meta = meta or {}
    guard_url(url, meta)
    headers = {"User-Agent": str(meta.get("user_agent") or DEFAULT_USER_AGENT)}
    for k, v in (meta.get("headers") or {}).items():
        headers[str(k)] = os.path.expandvars(str(v))
    timeout = float(timeout_s or meta.get("timeout_s") or DEFAULT_TIMEOUT_S)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout),
        headers=headers,
        **client_kwargs(meta),
    ) as client:
        r = await get_bounded(client, url, max_bytes=max_bytes_for(meta))
        r.raise_for_status()
        return parse_spec_text(r.text)


# --------------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------------


def clean(obj: Any, *, name_map: bool = False) -> Any:
    """Drop vendor extensions (except ``x-deprecated``), examples, externalDocs and servers.
    Keys of name maps (property names, paths, status codes...) are never dropped."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if not name_map:
                if ks in DROP_KEYS:
                    continue
                if ks.startswith("x-") and ks != "x-deprecated":
                    continue
            out[ks] = clean(v, name_map=(ks in NAME_MAPS) if not name_map else False)
        return out
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    return obj


def _resolve_pointer(root: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#"):
        return None
    node: Any = root
    for tok in split_pointer(ref[1:]):
        if isinstance(node, dict) and tok in node:
            node = node[tok]
        elif isinstance(node, list) and tok.isdigit() and int(tok) < len(node):
            node = node[int(tok)]
        else:
            return None
    return node


def dereference(node: Any, root: dict[str, Any], *, max_schema_depth: int = 1) -> Any:
    """Inline local ``$ref``s. Structural refs (parameters, requestBodies, responses, headers)
    are always inlined; schema refs count against ``max_schema_depth``. A ref already on
    the current stack (cycle) or beyond the depth budget stays a ``{"$ref": ...}`` node."""

    def walk(n: Any, depth: int, stack: tuple[str, ...]) -> Any:
        if isinstance(n, dict):
            ref = n.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                structural = ref.startswith(STRUCTURAL_REF_PREFIXES)
                if ref in stack or (not structural and depth >= max_schema_depth):
                    return {k: walk(v, depth, stack) for k, v in n.items()}
                target = _resolve_pointer(root, ref)
                if target is None:
                    return {k: walk(v, depth, stack) for k, v in n.items()}
                resolved = walk(target, depth if structural else depth + 1, (*stack, ref))
                if isinstance(resolved, dict):
                    extra = {k: walk(v, depth, stack) for k, v in n.items() if k != "$ref"}
                    return {**resolved, **extra}
                return resolved
            return {k: walk(v, depth, stack) for k, v in n.items()}
        if isinstance(n, list):
            return [walk(v, depth, stack) for v in n]
        return n

    return walk(node, 0, ())


# --------------------------------------------------------------------------------------
# Swagger 2.0 -> OpenAPI 3-like
# --------------------------------------------------------------------------------------

_SWAGGER_REF_MAP = (
    ("#/definitions/", "#/components/schemas/"),
    ("#/parameters/", "#/components/parameters/"),
    ("#/responses/", "#/components/responses/"),
)
_OAUTH_FLOW_MAP = {
    "implicit": "implicit",
    "password": "password",
    "application": "clientCredentials",
    "accessCode": "authorizationCode",
}
_PARAM_SCHEMA_KEYS = (
    "type",
    "format",
    "items",
    "enum",
    "default",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "maxLength",
    "minLength",
    "pattern",
    "maxItems",
    "minItems",
    "uniqueItems",
    "multipleOf",
)


def _rewrite_refs(node: Any, mapping: tuple[tuple[str, str], ...]) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                for old, new in mapping:
                    if v.startswith(old):
                        v = new + v[len(old) :]
                        break
            out[k] = _rewrite_refs(v, mapping)
        return out
    if isinstance(node, list):
        return [_rewrite_refs(v, mapping) for v in node]
    return node


def _swagger_param(p: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in p:
        return p
    out = {
        k: v for k, v in p.items() if k in ("name", "in", "description", "required", "deprecated")
    }
    if p.get("x-deprecated") is True:
        out["deprecated"] = True
    if "schema" in p:
        out["schema"] = p["schema"]
    else:
        out["schema"] = {k: v for k, v in p.items() if k in _PARAM_SCHEMA_KEYS}
    return out


def _swagger_body(
    params: list[dict[str, Any]], consumes: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    rest: list[dict[str, Any]] = []
    body: dict[str, Any] | None = None
    form_props: dict[str, Any] = {}
    form_required: list[str] = []
    has_file = False
    for p in params:
        loc = p.get("in")
        if loc == "body":
            body = {
                "content": {mt: {"schema": p.get("schema", {})} for mt in consumes},
            }
            if p.get("description"):
                body["description"] = p["description"]
            if p.get("required"):
                body["required"] = True
        elif loc == "formData":
            schema = {k: v for k, v in p.items() if k in _PARAM_SCHEMA_KEYS}
            if p.get("description"):
                schema["description"] = p["description"]
            if schema.get("type") == "file":
                has_file = True
                schema = {"type": "string", "format": "binary"}
            form_props[p["name"]] = schema
            if p.get("required"):
                form_required.append(p["name"])
        else:
            rest.append(_swagger_param(p))
    if form_props:
        mt = "multipart/form-data" if has_file else "application/x-www-form-urlencoded"
        schema: dict[str, Any] = {"type": "object", "properties": form_props}
        if form_required:
            schema["required"] = form_required
        body = {"content": {mt: {"schema": schema}}, "required": bool(form_required)}
    return rest, body


def swagger2_to_openapi3(doc: dict[str, Any]) -> dict[str, Any]:
    doc = _rewrite_refs(doc, _SWAGGER_REF_MAP)
    consumes = list(doc.get("consumes") or ["application/json"])
    produces = list(doc.get("produces") or ["application/json"])
    out: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": doc.get("info") or {},
        "paths": {},
        "components": {},
        "security": doc.get("security") or [],
    }
    comps = out["components"]
    if doc.get("definitions"):
        comps["schemas"] = doc["definitions"]
    if doc.get("parameters"):
        comps["parameters"] = {k: _swagger_param(v) for k, v in doc["parameters"].items()}
    if doc.get("responses"):
        comps["responses"] = {
            k: _swagger_response(v, produces) for k, v in doc["responses"].items()
        }
    if doc.get("securityDefinitions"):
        comps["securitySchemes"] = {
            k: _swagger_security(v) for k, v in doc["securityDefinitions"].items()
        }
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        new_item: dict[str, Any] = {}
        path_params = [
            _swagger_param(p) for p in item.get("parameters") or [] if p.get("in") != "body"
        ]
        if path_params:
            new_item["parameters"] = path_params
        for method, op in item.items():
            if method not in HTTP_METHODS or not isinstance(op, dict):
                continue
            new_op = {
                k: v
                for k, v in op.items()
                if k in ("operationId", "summary", "description", "tags", "deprecated", "security")
            }
            if op.get("x-deprecated") is True:
                new_op["deprecated"] = True
            op_consumes = list(op.get("consumes") or consumes)
            op_produces = list(op.get("produces") or produces)
            params, body = _swagger_body(list(op.get("parameters") or []), op_consumes)
            if not body:
                # a body parameter may live at the path level
                _, body = _swagger_body(
                    [
                        p
                        for p in item.get("parameters") or []
                        if p.get("in") in ("body", "formData")
                    ],
                    op_consumes,
                )
            if params:
                new_op["parameters"] = params
            if body:
                new_op["requestBody"] = body
            new_op["responses"] = {
                str(code): _swagger_response(resp, op_produces)
                for code, resp in (op.get("responses") or {}).items()
            }
            new_item[method] = new_op
        out["paths"][path] = new_item
    return out


def _swagger_response(resp: dict[str, Any], produces: list[str]) -> dict[str, Any]:
    if "$ref" in resp:
        return resp
    out = {k: v for k, v in resp.items() if k in ("description", "headers")}
    if "schema" in resp:
        out["content"] = {mt: {"schema": resp["schema"]} for mt in produces}
    return out


def _swagger_security(sd: dict[str, Any]) -> dict[str, Any]:
    t = sd.get("type")
    if t == "basic":
        return {"type": "http", "scheme": "basic", "description": sd.get("description")}
    if t == "oauth2":
        flow = _OAUTH_FLOW_MAP.get(str(sd.get("flow")), str(sd.get("flow")))
        flow_obj = {k: v for k, v in sd.items() if k in ("authorizationUrl", "tokenUrl", "scopes")}
        return {"type": "oauth2", "flows": {flow: flow_obj}, "description": sd.get("description")}
    return {k: v for k, v in sd.items() if k in ("type", "name", "in", "description")}


# --------------------------------------------------------------------------------------
# Google API Discovery -> OpenAPI 3-like
# --------------------------------------------------------------------------------------

_DISCOVERY_SCHEMA_KEYS = (
    "type",
    "format",
    "enum",
    "default",
    "minimum",
    "maximum",
    "pattern",
    "items",
    "properties",
    "additionalProperties",
    "required",
    "description",
    "deprecated",
    "$ref",
    "readOnly",
)


def _discovery_schema(s: Any) -> Any:
    """Discovery schemas are JSON-Schema-like with bare ``$ref`` names."""
    if isinstance(s, dict):
        out: dict[str, Any] = {}
        for k, v in s.items():
            if k == "$ref" and isinstance(v, str) and not v.startswith("#"):
                out[k] = "#/components/schemas/" + v
            elif k == "properties" and isinstance(v, dict):
                out[k] = {pk: _discovery_schema(pv) for pk, pv in v.items()}
            elif k in ("items", "additionalProperties"):
                out[k] = _discovery_schema(v)
            elif k in _DISCOVERY_SCHEMA_KEYS:
                out[k] = v
        if s.get("repeated"):
            out = {"type": "array", "items": {k: v for k, v in out.items() if k != "description"}}
            if s.get("description"):
                out["description"] = s["description"]
        return out
    if isinstance(s, list):
        return [_discovery_schema(v) for v in s]
    return s


def discovery_to_openapi3(doc: dict[str, Any]) -> dict[str, Any]:
    service_path = str(doc.get("servicePath") or doc.get("basePath") or "")
    out: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": {
            "title": doc.get("title") or doc.get("name"),
            "version": doc.get("version"),
            "description": doc.get("description"),
        },
        "paths": {},
        "components": {"schemas": {}, "securitySchemes": {}},
        "security": [],
    }
    for name, schema in (doc.get("schemas") or {}).items():
        out["components"]["schemas"][name] = _discovery_schema(schema)
    scopes = ((doc.get("auth") or {}).get("oauth2") or {}).get("scopes") or {}
    if scopes:
        out["components"]["securitySchemes"]["oauth2"] = {
            "type": "oauth2",
            "flows": {
                "implicit": {
                    "authorizationUrl": "https://accounts.google.com/o/oauth2/auth",
                    "scopes": {k: (v or {}).get("description", "") for k, v in scopes.items()},
                }
            },
        }

    def add_methods(methods: dict[str, Any]) -> None:
        for m in methods.values():
            if not isinstance(m, dict):
                continue
            raw = str(m.get("flatPath") or m.get("path") or "")
            path = "/" + (service_path + raw).lstrip("/")
            path = re.sub(r"\{\+(\w+)\}", r"{\1}", path)
            http = str(m.get("httpMethod") or "GET").lower()
            op: dict[str, Any] = {"operationId": m.get("id")}
            if m.get("description"):
                op["description"] = m["description"]
            if m.get("deprecated") is True:
                op["deprecated"] = True
            params = m.get("parameters") or {}
            order = [p for p in (m.get("parameterOrder") or []) if p in params]
            order += sorted(p for p in params if p not in order)
            plist: list[dict[str, Any]] = []
            for pname in order:
                p = params[pname]
                location = p.get("location", "query")
                entry: dict[str, Any] = {
                    "name": pname,
                    "in": "path" if location == "path" else "query",
                    "required": bool(p.get("required")) or location == "path",
                    "schema": _discovery_schema({k: v for k, v in p.items() if k != "description"}),
                }
                if p.get("description"):
                    entry["description"] = p["description"]
                if p.get("deprecated") is True:
                    entry["deprecated"] = True
                plist.append(entry)
            if plist:
                op["parameters"] = plist
            if m.get("request"):
                op["requestBody"] = {
                    "required": True,
                    "content": {"application/json": {"schema": _discovery_schema(m["request"])}},
                }
            responses: dict[str, Any] = {}
            if m.get("response"):
                responses["200"] = {
                    "description": "OK",
                    "content": {"application/json": {"schema": _discovery_schema(m["response"])}},
                }
            else:
                responses["200"] = {"description": "OK"}
            op["responses"] = responses
            if m.get("scopes"):
                op["security"] = [{"oauth2": list(m["scopes"])}]
            out["paths"].setdefault(path, {})[http] = op

    def walk(resources: dict[str, Any]) -> None:
        for res in resources.values():
            if not isinstance(res, dict):
                continue
            add_methods(res.get("methods") or {})
            walk(res.get("resources") or {})

    add_methods(doc.get("methods") or {})
    walk(doc.get("resources") or {})
    return out


# --------------------------------------------------------------------------------------
# Native diff
# --------------------------------------------------------------------------------------


def _ops_of(item: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(item, dict):
        return {}
    return {m: item[m] for m in HTTP_METHODS if isinstance(item.get(m), dict)}


def _op_summary(path: str, method: str, op: dict[str, Any]) -> dict[str, Any]:
    out = {"method": method.upper(), "path": path}
    for k in ("operationId", "summary", "deprecated"):
        if op.get(k) is not None:
            out[k] = op[k]
    return out


def _merged_params(item: dict[str, Any], op: dict[str, Any]) -> list[dict[str, Any]]:
    """Operation parameters, then path-level ones not overridden (by name+in)."""
    out: list[dict[str, Any]] = [p for p in op.get("parameters") or [] if isinstance(p, dict)]
    seen = {(p.get("in"), p.get("name")) for p in out}
    for p in item.get("parameters") or []:
        if isinstance(p, dict) and (p.get("in"), p.get("name")) not in seen:
            out.append(p)
    return out


def _param_key(p: dict[str, Any]) -> tuple[str, str]:
    return (str(p.get("in", "")), str(p.get("name", "")))


def _make_resolver(components: dict[str, Any]) -> Any:
    schemas = components.get("schemas") or {}

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            target = schemas.get(ref[len("#/components/schemas/") :])
            if isinstance(target, dict):
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return {**target, **extra}
        return node

    return resolve


def diff_openapi(
    before: dict[str, Any], after: dict[str, Any], *, max_ref_depth: int = 2
) -> list[ChangeOp]:
    ops: list[ChangeOp] = []
    rb = _make_resolver(before.get("components") or {})
    ra = _make_resolver(after.get("components") or {})

    def sdiff(b: Any, a: Any, ptr: str) -> list[ChangeOp]:
        return diff_json_schema(
            b or {}, a or {}, ptr, resolve_before=rb, resolve_after=ra, max_ref_depth=max_ref_depth
        )

    b_paths, a_paths = before.get("paths") or {}, after.get("paths") or {}
    for path in sorted(set(b_paths) | set(a_paths)):
        b_item, a_item = b_paths.get(path) or {}, a_paths.get(path) or {}
        b_ops, a_ops = _ops_of(b_item), _ops_of(a_item)
        if path not in a_paths and not b_ops:
            ops.append(
                ChangeOp(op="removed", path=json_pointer("paths", path), before=b_item, after=None)
            )
            continue
        if path not in b_paths and not a_ops:
            ops.append(
                ChangeOp(op="added", path=json_pointer("paths", path), before=None, after=a_item)
            )
            continue
        for method in sorted(set(b_ops) | set(a_ops)):
            ptr = json_pointer("paths", path, method)
            if method not in a_ops:
                ops.append(
                    ChangeOp(
                        op="removed",
                        path=ptr,
                        before=_op_summary(path, method, b_ops[method]),
                        after=None,
                    )
                )
                continue
            if method not in b_ops:
                ops.append(
                    ChangeOp(
                        op="added",
                        path=ptr,
                        before=None,
                        after=_op_summary(path, method, a_ops[method]),
                    )
                )
                continue
            _diff_operation(b_item, b_ops[method], a_item, a_ops[method], ptr, ops, sdiff)

    # components.schemas (refs kept: deep changes are reported here once)
    b_schemas = (before.get("components") or {}).get("schemas") or {}
    a_schemas = (after.get("components") or {}).get("schemas") or {}
    for name in sorted(set(b_schemas) | set(a_schemas)):
        ptr = json_pointer("components", "schemas", name)
        if name not in a_schemas:
            ops.append(
                ChangeOp(op="removed", path=ptr, before=schema_summary(b_schemas[name]), after=None)
            )
        elif name not in b_schemas:
            ops.append(
                ChangeOp(op="added", path=ptr, before=None, after=schema_summary(a_schemas[name]))
            )
        else:
            ops.extend(diff_json_schema(b_schemas[name], a_schemas[name], ptr, max_ref_depth=0))

    # security
    b_sec = (before.get("components") or {}).get("securitySchemes") or {}
    a_sec = (after.get("components") or {}).get("securitySchemes") or {}
    for name in sorted(set(b_sec) | set(a_sec)):
        b_s, a_s = b_sec.get(name), a_sec.get(name)
        if b_s == a_s:
            continue
        sptr = json_pointer("components", "securitySchemes", name)
        if _auth_shape(b_s) != _auth_shape(a_s):
            ops.append(ChangeOp(op="auth_changed", path=sptr, before=b_s, after=a_s))
            continue
        # same scheme, type, location and scope set: only wording (or scope order) moved
        for k in ("description", "title"):
            b_txt = b_s.get(k) if isinstance(b_s, dict) else None
            a_txt = a_s.get(k) if isinstance(a_s, dict) else None
            if b_txt != a_txt:
                ops.append(
                    ChangeOp(op="text_changed", path=sptr + "/" + k, before=b_txt, after=a_txt)
                )
    if _security_key(before.get("security")) != _security_key(after.get("security")):
        ops.append(
            ChangeOp(
                op="auth_changed",
                path="/security",
                before=before.get("security"),
                after=after.get("security"),
            )
        )
    return detect_renames(ops)


def _diff_operation(
    b_item: dict[str, Any],
    b_op: dict[str, Any],
    a_item: dict[str, Any],
    a_op: dict[str, Any],
    ptr: str,
    ops: list[ChangeOp],
    sdiff: Any,
) -> None:
    if _is_deprecated(a_op) and not _is_deprecated(b_op):
        ops.append(ChangeOp(op="deprecated", path=ptr, before=False, after=True))
    for k in ("summary", "description"):
        if b_op.get(k) != a_op.get(k):
            ops.append(
                ChangeOp(
                    op="text_changed", path=ptr + "/" + k, before=b_op.get(k), after=a_op.get(k)
                )
            )
    if _security_key(b_op.get("security")) != _security_key(a_op.get("security")):
        ops.append(
            ChangeOp(
                op="auth_changed",
                path=ptr + "/security",
                before=b_op.get("security"),
                after=a_op.get("security"),
            )
        )

    # parameters by (in, name); pointers are indexes into the merged list
    b_params = _merged_params(b_item, b_op)
    a_params = _merged_params(a_item, a_op)
    b_index = {_param_key(p): (i, p) for i, p in enumerate(b_params)}
    a_index = {_param_key(p): (i, p) for i, p in enumerate(a_params)}
    for key in sorted(set(b_index) | set(a_index)):
        if key not in a_index:
            i, p = b_index[key]
            ops.append(
                ChangeOp(
                    op="removed", path=f"{ptr}/parameters/{i}", before=_param_summary(p), after=None
                )
            )
            continue
        if key not in b_index:
            i, p = a_index[key]
            pp = f"{ptr}/parameters/{i}"
            ops.append(ChangeOp(op="added", path=pp, before=None, after=_param_summary(p)))
            if p.get("required") is True:
                ops.append(ChangeOp(op="required_added", path=pp, before=None, after=p.get("name")))
            continue
        (_, bp), (i, ap) = b_index[key], a_index[key]
        pp = f"{ptr}/parameters/{i}"
        if ap.get("required") is True and bp.get("required") is not True:
            ops.append(ChangeOp(op="required_added", path=pp, before=None, after=ap.get("name")))
        elif bp.get("required") is True and ap.get("required") is not True:
            ops.append(ChangeOp(op="required_removed", path=pp, before=bp.get("name"), after=None))
        if _is_deprecated(ap) and not _is_deprecated(bp):
            ops.append(ChangeOp(op="deprecated", path=pp, before=False, after=True))
        if bp.get("description") != ap.get("description"):
            ops.append(
                ChangeOp(
                    op="text_changed",
                    path=pp + "/description",
                    before=bp.get("description"),
                    after=ap.get("description"),
                )
            )
        if (bp.get("schema") or {}) != (ap.get("schema") or {}):
            sub = sdiff(bp.get("schema"), ap.get("schema"), pp + "/schema")
            ops.extend(
                sub or residual_schema_ops(bp.get("schema"), ap.get("schema"), pp + "/schema")
            )

    # requestBody
    b_body, a_body = b_op.get("requestBody") or {}, a_op.get("requestBody") or {}
    bptr = ptr + "/requestBody"
    if b_body and not a_body:
        ops.append(ChangeOp(op="removed", path=bptr, before=_body_summary(b_body), after=None))
    elif a_body and not b_body:
        ops.append(ChangeOp(op="added", path=bptr, before=None, after=_body_summary(a_body)))
        if a_body.get("required") is True:
            ops.append(ChangeOp(op="required_added", path=bptr, before=None, after="requestBody"))
    elif b_body and a_body:
        if a_body.get("required") is True and b_body.get("required") is not True:
            ops.append(ChangeOp(op="required_added", path=bptr, before=None, after="requestBody"))
        elif b_body.get("required") is True and a_body.get("required") is not True:
            ops.append(ChangeOp(op="required_removed", path=bptr, before="requestBody", after=None))
        _diff_content(
            b_body.get("content") or {}, a_body.get("content") or {}, bptr + "/content", ops, sdiff
        )

    # responses
    b_resp, a_resp = b_op.get("responses") or {}, a_op.get("responses") or {}
    for code in sorted(set(b_resp) | set(a_resp)):
        rptr = ptr + "/responses/" + json_pointer_escape(str(code))
        if code not in a_resp:
            ops.append(
                ChangeOp(op="removed", path=rptr, before=_body_summary(b_resp[code]), after=None)
            )
        elif code not in b_resp:
            ops.append(
                ChangeOp(op="added", path=rptr, before=None, after=_body_summary(a_resp[code]))
            )
        else:
            br, ar = b_resp[code] or {}, a_resp[code] or {}
            _diff_content(
                br.get("content") or {}, ar.get("content") or {}, rptr + "/content", ops, sdiff
            )


def _diff_content(
    b_content: dict[str, Any], a_content: dict[str, Any], ptr: str, ops: list[ChangeOp], sdiff: Any
) -> None:
    for mt in sorted(set(b_content) | set(a_content)):
        mptr = ptr + "/" + json_pointer_escape(mt) + "/schema"
        b_schema = (b_content.get(mt) or {}).get("schema")
        a_schema = (a_content.get(mt) or {}).get("schema")
        if mt not in a_content:
            ops.append(
                ChangeOp(op="removed", path=mptr, before=schema_summary(b_schema), after=None)
            )
        elif mt not in b_content:
            ops.append(ChangeOp(op="added", path=mptr, before=None, after=schema_summary(a_schema)))
        elif (b_schema or {}) != (a_schema or {}):
            ops.extend(
                sdiff(b_schema, a_schema, mptr) or residual_schema_ops(b_schema, a_schema, mptr)
            )


def _is_deprecated(x: dict[str, Any]) -> bool:
    return x.get("deprecated") is True or x.get("x-deprecated") is True


_AUTH_TEXT_KEYS = frozenset({"description", "title"})


def _auth_shape(scheme: Any) -> Any:
    """What a security scheme means to a caller: everything but its wording, with each
    OAuth flow's ``scopes`` map reduced to the sorted scope names."""
    if not isinstance(scheme, dict):
        return scheme
    out: dict[str, Any] = {}
    for k, v in scheme.items():
        if k in _AUTH_TEXT_KEYS or str(k).startswith("x-"):
            continue
        if k == "flows" and isinstance(v, dict):
            out[k] = {
                name: (
                    {
                        fk: (
                            sorted(map(str, fv)) if fk == "scopes" and isinstance(fv, dict) else fv
                        )
                        for fk, fv in flow.items()
                        if fk not in _AUTH_TEXT_KEYS
                    }
                    if isinstance(flow, dict)
                    else flow
                )
                for name, flow in v.items()
            }
        else:
            out[k] = v
    return out


def _security_key(requirements: Any) -> frozenset[Any] | None:
    """A ``security`` list compared without order: the set of alternatives, each the set of
    ``(scheme, sorted scopes)`` pairs. Empty and missing compare equal (as before)."""
    if not requirements or not isinstance(requirements, list):
        return None
    alternatives = set()
    for req in requirements:
        if not isinstance(req, dict):
            alternatives.add(frozenset({("?", repr(req))}))
            continue
        alternatives.add(
            frozenset(
                (str(k), tuple(sorted(str(s) for s in (v or []) if s is not None)))
                for k, v in req.items()
            )
        )
    return frozenset(alternatives)


def _param_summary(p: dict[str, Any]) -> dict[str, Any]:
    out = {k: p.get(k) for k in ("name", "in", "required") if p.get(k) is not None}
    if isinstance(p.get("schema"), dict):
        out["schema"] = schema_summary(p["schema"])
    if p.get("description"):
        out["description"] = str(p["description"])[:200]
    return out


def _body_summary(b: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if b.get("required") is not None:
        out["required"] = b.get("required")
    if b.get("description"):
        out["description"] = str(b["description"])[:200]
    content = b.get("content") or {}
    if content:
        out["content"] = {mt: schema_summary((v or {}).get("schema")) for mt, v in content.items()}
    return out


def _name_of(op: ChangeOp) -> str:
    payload = op.before if op.op == "removed" else op.after
    if isinstance(payload, dict) and isinstance(payload.get("name"), str):
        return payload["name"]
    toks = split_pointer(op.path)
    return toks[-1] if toks else ""


def _shape(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            k: _shape(v)
            for k, v in payload.items()
            if k not in ("description", "title", "name", "summary", "operationId", "fingerprint")
        }
    if isinstance(payload, list):
        return [_shape(v) for v in payload]
    return payload


def _distinctive_shape(shape: Any) -> bool:
    """An identical shape only proves a rename when it is specific: an object, array, enum or
    ``$ref`` schema (or a summary listing properties), not a bare scalar such as
    ``{"in": "query", "required": false, "schema": {"type": "string"}}`` that any two
    optional string parameters share."""
    if not isinstance(shape, dict) or not shape:
        return False
    for node in (shape, shape.get("schema"), shape.get("inputSchema")):
        if isinstance(node, dict) and any(
            k in node for k in ("properties", "enum", "items", "$ref", "oneOf", "anyOf", "allOf")
        ):
            return True
    return False


def _rename_candidate(op: ChangeOp) -> bool:
    """Siblings that can never be renames of each other: HTTP operations on one path (GET
    removed + PUT added is two changes), response status codes and media types."""
    toks = split_pointer(op.path)
    if len(toks) == 3 and toks[0] == "paths" and toks[2] in HTTP_METHODS:
        return False
    if len(toks) >= 2 and toks[-2] in ("responses", "content"):
        return False
    return not (len(toks) >= 3 and toks[-1] == "schema" and toks[-3] == "content")


def detect_renames(ops: list[ChangeOp]) -> list[ChangeOp]:
    """Pair ``removed``/``added`` siblings (same parent pointer) into one ``renamed`` op
    (path = the removed item's pointer, before = old name, after = new name) when they share
    an identical, distinctive shape or their names are within Levenshtein distance < 3 and
    under half the longer name. HTTP methods, status codes and media types never pair."""
    by_parent: dict[str, tuple[list[int], list[int]]] = {}
    for i, op in enumerate(ops):
        if op.op not in ("removed", "added") or not _rename_candidate(op):
            continue
        parent = op.path.rsplit("/", 1)[0]
        bucket = by_parent.setdefault(parent, ([], []))
        bucket[0 if op.op == "removed" else 1].append(i)
    replaced: dict[int, ChangeOp] = {}
    dropped: set[int] = set()
    for removed_idx, added_idx in by_parent.values():
        if not removed_idx or not added_idx:
            continue
        free_added = list(added_idx)
        for ri in removed_idx:
            r = ops[ri]
            r_name = _name_of(r)
            r_shape = _shape(r.before)
            best: tuple[int, int] | None = None
            for ai in free_added:
                a = ops[ai]
                a_name = _name_of(a)
                if not r_name or not a_name or r_name == a_name:
                    continue
                identical = _distinctive_shape(r_shape) and r_shape == _shape(a.after)
                dist = levenshtein(r_name, a_name)
                close = dist < RENAME_MAX_DISTANCE and dist * 2 < max(len(r_name), len(a_name))
                if identical or close:
                    score = 0 if identical else dist
                    if best is None or score < best[0]:
                        best = (score, ai)
            if best is not None:
                ai = best[1]
                free_added.remove(ai)
                replaced[ri] = ChangeOp(
                    op="renamed", path=r.path, before=r_name, after=_name_of(ops[ai])
                )
                dropped.add(ai)
    out: list[ChangeOp] = []
    for i, op in enumerate(ops):
        if i in dropped:
            continue
        out.append(replaced.get(i, op))
    return out


# --------------------------------------------------------------------------------------
# Optional oasdiff
# --------------------------------------------------------------------------------------

OASDIFF_ID_MAP: dict[str, str] = {
    "api-path-removed-without-deprecation": "removed",
    "api-path-removed-before-sunset": "removed",
    "api-removed-without-deprecation": "removed",
    "api-removed-before-sunset": "removed",
    "api-path-added": "added",
    "endpoint-added": "added",
    "api-deprecated": "deprecated",
    "endpoint-deprecated": "deprecated",
    "api-sunset-date-changed-too-small": "deprecated",
    "request-parameter-removed": "removed",
    "new-required-request-parameter": "required_added",
    "request-parameter-became-required": "required_added",
    "request-parameter-became-optional": "required_removed",
    "new-optional-request-parameter": "added",
    "request-parameter-type-changed": "type_changed",
    "request-parameter-type-generalized": "type_changed",
    "request-parameter-enum-value-removed": "enum_changed",
    "request-parameter-enum-value-added": "enum_changed",
    "request-property-removed": "removed",
    "new-required-request-property": "required_added",
    "request-property-became-required": "required_added",
    "request-property-became-optional": "required_removed",
    "new-optional-request-property": "added",
    "request-property-type-changed": "type_changed",
    "request-property-type-generalized": "type_changed",
    "request-property-enum-value-removed": "enum_changed",
    "request-property-enum-value-added": "enum_changed",
    "request-body-became-required": "required_added",
    "request-body-became-optional": "required_removed",
    "added-required-request-body": "required_added",
    "request-body-type-changed": "type_changed",
    "response-property-removed": "removed",
    "response-required-property-removed": "removed",
    "response-optional-property-removed": "removed",
    "response-property-type-changed": "type_changed",
    "response-property-type-generalized": "type_changed",
    "response-property-enum-value-added": "enum_changed",
    "response-property-enum-value-removed": "enum_changed",
    "response-property-became-optional": "required_removed",
    "response-property-became-required": "required_added",
    "response-success-status-removed": "removed",
    "response-non-success-status-removed": "removed",
    "response-media-type-removed": "removed",
    "api-security-removed": "auth_changed",
    "api-security-added": "auth_changed",
    "api-security-scope-added": "auth_changed",
    "api-security-scope-removed": "auth_changed",
    "api-global-security-removed": "auth_changed",
    "api-global-security-added": "auth_changed",
    "api-global-security-scope-added": "auth_changed",
    "api-global-security-scope-removed": "auth_changed",
    "api-security-component-removed": "auth_changed",
    "api-operation-id-removed": "text_changed",
    "api-operation-id-added": "text_changed",
    "api-tag-removed": "text_changed",
    "api-tag-added": "text_changed",
}


def _guess_oasdiff_op(oid: str) -> str:
    if oid in OASDIFF_ID_MAP:
        return OASDIFF_ID_MAP[oid]
    if "security" in oid or "auth" in oid:
        return "auth_changed"
    if "renamed" in oid:
        return "renamed"
    if "deprecat" in oid or "sunset" in oid:
        return "deprecated"
    if "enum" in oid:
        return "enum_changed"
    if "type" in oid or "format" in oid or "pattern" in oid or "max" in oid or "min" in oid:
        return "type_changed"
    if "required" in oid and ("added" in oid or "new" in oid or "became-required" in oid):
        return "required_added"
    if "became-optional" in oid or ("required" in oid and "removed" in oid):
        return "required_removed"
    if "removed" in oid:
        return "removed"
    if "added" in oid or "new" in oid:
        return "added"
    return "text_changed"


def map_oasdiff_changelog(entries: list[dict[str, Any]]) -> list[ChangeOp]:
    """``oasdiff changelog -f json`` entries -> ChangeOps. ``path``+``operation`` become the
    operation pointer; the oasdiff id, level and text are kept in ``after``."""
    ops: list[ChangeOp] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        oid = str(e.get("id") or "")
        kind = _guess_oasdiff_op(oid)
        path = e.get("path")
        method = str(e.get("operation") or "").lower()
        if path and method:
            ptr = json_pointer("paths", str(path), method)
        elif path:
            ptr = json_pointer("paths", str(path))
        else:
            ptr = "/" + json_pointer_escape(str(e.get("source") or e.get("section") or "oasdiff"))
        ops.append(
            ChangeOp(
                op=kind,  # type: ignore[arg-type]
                path=ptr,
                before={"oasdiff": oid} if kind in ("removed", "renamed") else None,
                after={
                    "oasdiff": oid,
                    "level": e.get("level"),
                    "text": str(e.get("text") or "")[:500],
                    "args": e.get("args"),
                },
            )
        )
    return ops


def oasdiff_enabled() -> bool:
    return os.environ.get("KINTSUA_USE_OASDIFF") == "1" and shutil.which("oasdiff") is not None


def run_oasdiff(before: dict[str, Any], after: dict[str, Any]) -> list[ChangeOp] | None:
    """Run ``oasdiff changelog -f json old new`` on two canonical bodies; None on failure."""
    if not oasdiff_enabled():
        return None
    with tempfile.TemporaryDirectory(prefix="kintsua-oasdiff-") as d:
        old = os.path.join(d, "old.json")
        new = os.path.join(d, "new.json")
        for p, body in ((old, before), (new, after)):
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"openapi": "3.0.0", **body}, f)
        try:
            proc = subprocess.run(
                ["oasdiff", "changelog", "-f", "json", old, new],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("oasdiff failed: %s", e)
            return None
        if proc.returncode not in (0, 1) or not proc.stdout.strip():
            log.warning("oasdiff exit %s: %s", proc.returncode, proc.stderr[-500:])
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict):
        data = data.get("changes") or data.get("changelog") or []
    return map_oasdiff_changelog(data if isinstance(data, list) else [])


# --------------------------------------------------------------------------------------
# Watcher
# --------------------------------------------------------------------------------------


@register
class OpenAPIWatcher(BaseWatcher):
    kind = "openapi"

    async def fetch(self, source: SourceRef) -> CanonicalDoc:
        raw = await fetch_spec(source.locator, source.meta)
        body = canonicalise(raw, source.meta)
        version = (body.get("info") or {}).get("version")
        return CanonicalDoc(version=str(version) if version else None, body=body)

    def diff(self, before: CanonicalDoc, after: CanonicalDoc) -> list[ChangeOp]:
        native = diff_openapi(before.body, after.body)
        extra = run_oasdiff(before.body, after.body)
        if not extra:
            return native
        seen = {(op.op, op.path) for op in native}
        merged = list(native)
        for op in extra:
            if (op.op, op.path) not in seen:
                merged.append(op)
                seen.add((op.op, op.path))
        return merged
