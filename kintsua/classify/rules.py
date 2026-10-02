"""Rules-first classifier.

Pure Python (pydantic + stdlib only) so the open-source CLI ships it. Rules are applied
in a fixed order; the first matching rule sets severity and category and every later
match only adds ``rule_hits``, ``affected_paths`` and a ``migration_hint``. The order is
the spec's order with one refinement: rules are grouped by severity (breaking, then
degraded, then safe) so a breaking change always wins over a degraded one in the same
event, whatever the op order.

The classifier understands three path dialects:

* OpenAPI JSON pointers: ``/paths/~1v1~1charges/post/parameters/3``,
  ``/paths/~1v1~1charges/post/requestBody/content/application~1json/schema/properties/x``,
  ``/paths/~1v1~1charges/post/responses/200/...``, ``/components/securitySchemes/...``
* MCP pointers: ``/tools/<name>``, ``/tools/<name>/inputSchema/properties/x``,
  ``/tools/<name>/description``, ``/resources/<uri>``, ``/prompts/<name>``
* Package pointers: ``/latest``, ``/versions/<v>``

Operation strings in ``affected_paths`` are ``"POST /v1/charges"`` (``"/v1/charges"`` for a
whole path item whose methods are unknown), ``"tool:<name>"``, ``"resource:<uri>"``,
``"prompt:<name>"``, ``"schema:<Name>"`` for shared component schemas, ``"package:<name>"``
for registry events and ``"*"`` when every operation of the source is affected (a global
security change).

Mute handling is not here; the matcher applies ``.kintsua.yml`` mutes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from kintsua.classify.levenshtein import levenshtein
from kintsua.classify.schema import Category, Classification, Severity
from kintsua.core.events import ChangeEvent, ChangeOp, json_pointer, split_pointer

# --------------------------------------------------------------------------------------
# Rule table (stable identifiers; the order is the priority inside a severity band)
# --------------------------------------------------------------------------------------

RULE_REMOVED_OPERATION = "removed_operation"
RULE_REMOVED_TOOL = "removed_tool"
RULE_REMOVED_RESOURCE = "removed_resource"
RULE_REMOVED_PROMPT = "removed_prompt"
RULE_REMOVED_RESPONSE_FIELD = "removed_response_field"
RULE_REMOVED_REQUEST_FIELD = "removed_request_field"
RULE_REQUIRED_ADDED = "required_added"
RULE_TYPE_CHANGED = "type_changed"
RULE_RENAMED = "renamed"
RULE_ENUM_VALUES_REMOVED = "enum_values_removed"
RULE_AUTH_CHANGED = "auth_changed"
RULE_DEPRECATED = "deprecated"
RULE_MAJOR_BUMP = "major_bump"
RULE_ENUM_VALUES_ADDED = "enum_values_added"
RULE_REQUIRED_REMOVED = "required_removed"
RULE_REQUIRED_ADDED_RESPONSE = "required_added_response"
RULE_ADDED_OPTIONAL = "added_optional"
RULE_VERSION_BUMP = "version_bump"
RULE_ADDED_DESCRIPTION = "added_description"
RULE_METADATA_ONLY = "metadata_only"
RULE_REQUIRED_ADDED_SHARED = "required_added_shared"  # context only (see _evaluate_op)
RULE_TEXT_CHANGED = "text_changed"  # context only: never decides a severity
RULE_TEXT_ONLY = "text_only"  # the placeholder classify_event() returns for the model

RULES: list[tuple[str, Severity | None, Category | None]] = [
    (RULE_REMOVED_OPERATION, "breaking", "removal"),
    (RULE_REMOVED_TOOL, "breaking", "removal"),
    (RULE_REMOVED_RESOURCE, "breaking", "removal"),
    (RULE_REMOVED_PROMPT, "breaking", "removal"),
    (RULE_REMOVED_RESPONSE_FIELD, "breaking", "removal"),
    (RULE_REMOVED_REQUEST_FIELD, "breaking", "removal"),
    (RULE_REQUIRED_ADDED, "breaking", "required_param"),
    (RULE_TYPE_CHANGED, "breaking", "type_change"),
    (RULE_RENAMED, "breaking", "rename"),
    (RULE_ENUM_VALUES_REMOVED, "breaking", "type_change"),
    (RULE_AUTH_CHANGED, "breaking", "auth"),
    (RULE_DEPRECATED, "degraded", "deprecation"),
    (RULE_MAJOR_BUMP, "degraded", "deprecation"),
    (RULE_ENUM_VALUES_ADDED, "degraded", "behaviour"),
    (RULE_REQUIRED_REMOVED, "safe", "behaviour"),
    (RULE_REQUIRED_ADDED_RESPONSE, "safe", "behaviour"),
    (RULE_ADDED_OPTIONAL, "safe", "behaviour"),
    (RULE_VERSION_BUMP, "safe", "behaviour"),
    (RULE_ADDED_DESCRIPTION, "safe", "docs_only"),
    (RULE_METADATA_ONLY, "safe", "docs_only"),
    (RULE_REQUIRED_ADDED_SHARED, None, None),
    (RULE_TEXT_CHANGED, None, None),
]
RULE_INDEX: dict[str, int] = {name: i for i, (name, _, _) in enumerate(RULES)}
RULE_OUTCOME: dict[str, tuple[Severity | None, Category | None]] = {
    name: (sev, cat) for name, sev, cat in RULES
}
RULE_NAMES: list[str] = [name for name, _, _ in RULES]
SEVERITY_RANK: dict[str, int] = {"breaking": 0, "degraded": 1, "safe": 2, "unknown": 3}

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
DOC_KEYS = frozenset(
    {
        "description",
        "summary",
        "title",
        "example",
        "examples",
        "externalDocs",
        "x-codeSamples",
        "x-code-samples",
        "deprecationMessage",
    }
)
SECURITY_TOKENS = frozenset({"security", "securitySchemes", "securityDefinitions"})
TYPE_KEYS = frozenset({"type", "$ref"})
METADATA_ROOTS = frozenset(
    {
        "info",
        "tags",
        "externalDocs",
        "openapi",
        "swagger",
        "serverInfo",
        "capabilities",
        "instructions",
        "protocolVersion",
        "consumes",
        "produces",
        "schemes",
        "x-tagGroups",
    }
)
PACKAGE_ROOTS = frozenset({"latest", "versions", "dist-tags", "releases", "version"})
MCP_COLLECTIONS = {
    "tools": "tool",
    "resources": "resource",
    "resourceTemplates": "resource",
    "prompts": "prompt",
}

Dialect = Literal["openapi", "mcp", "package", "other"]
Side = Literal["request", "response", "shared"] | None


# --------------------------------------------------------------------------------------
# Path parsing
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PathInfo:
    """What a JSON pointer in a change op points at."""

    path: str
    tokens: tuple[str, ...]
    dialect: Dialect
    kind: str  # path_item | operation | parameter | request_body | response | schema | security |
    # deprecated_flag | doc | metadata | tool | resource | prompt | input_schema |
    # output_schema | arguments | version | other
    http_path: str | None = None
    method: str | None = None
    name: str | None = None  # tool / resource / prompt / component schema / security scheme
    side: Side = None
    field: str | None = None  # property or parameter name when the pointer carries it
    index: int | None = None  # list index when the pointer addresses a list element
    status: str | None = None  # response status code

    @property
    def leaf(self) -> str:
        return self.tokens[-1] if self.tokens else ""

    @property
    def parent_tokens(self) -> tuple[str, ...]:
        return self.tokens[:-1]

    def operation(self, display_name: str | None = None) -> str:
        """The consumer-facing operation string ('' when the pointer is not one)."""
        if self.dialect == "openapi":
            if self.kind == "security" and self.http_path is None:
                return "*"
            if self.http_path is not None:
                return f"{self.method.upper()} {self.http_path}" if self.method else self.http_path
            if self.kind == "schema" and self.name:
                return f"schema:{self.name}"
            return ""
        if self.dialect == "mcp":
            if self.name and self.kind in (
                "tool",
                "input_schema",
                "output_schema",
                "doc",
                "metadata",
                "deprecated_flag",
                "other",
            ):
                return (
                    f"tool:{self.name}"
                    if self.tokens and self.tokens[0] == "tools"
                    else _mcp_prefix(self.tokens[0], self.name)
                )
            if self.name and self.kind in ("resource", "prompt", "arguments"):
                return _mcp_prefix(self.tokens[0], self.name)
            return ""
        if self.dialect == "package":
            return f"package:{display_name}" if display_name else "package"
        return ""

    def label(self, display_name: str | None = None) -> str:
        """Human label for summaries ('POST /v1/charges', 'tool get_weather', 'stripe')."""
        if self.dialect == "openapi":
            if self.http_path is not None:
                return (
                    f"{self.method.upper()} {_frag(self.http_path)}"
                    if self.method
                    else f"path {_frag(self.http_path)}"
                )
            if self.kind == "schema" and self.name:
                return f"schema {_frag(self.name)}"
            if self.kind == "security":
                return (
                    f"security scheme {_q(self.name)}"
                    if self.name
                    else "the global security requirements"
                )
            return "the spec"
        if self.dialect == "mcp":
            if self.name:
                noun = MCP_COLLECTIONS.get(self.tokens[0], "tool")
                return f"{noun} {_frag(self.name)}"
            return "the server"
        if self.dialect == "package":
            return _frag(display_name) if display_name else "the package"
        return "the source"


def _mcp_prefix(collection: str, name: str) -> str:
    return f"{MCP_COLLECTIONS.get(collection, 'tool')}:{name}"


def _field_after(tokens: tuple[str, ...], key: str) -> str | None:
    """Token following the *last* occurrence of ``key`` (e.g. ``properties``)."""
    for i in range(len(tokens) - 1, -1, -1):
        if tokens[i] == key:
            return tokens[i + 1] if i + 1 < len(tokens) else None
    return None


def _schema_field(tokens: tuple[str, ...]) -> str | None:
    f = _field_after(tokens, "properties")
    if f is not None:
        return f
    # a header or a named parameter object keyed by name
    return None


def _is_int(tok: str) -> bool:
    return tok.isdigit()


def parse_path(path: str) -> PathInfo:
    """Classify a JSON pointer into dialect / kind / side / names."""
    toks = tuple(split_pointer(path))
    if not toks:
        return PathInfo(path=path, tokens=toks, dialect="other", kind="other")
    root = toks[0]

    # ---- OpenAPI ---------------------------------------------------------------
    if root == "paths" and len(toks) >= 2:
        http_path = toks[1]
        rest = toks[2:]
        if not rest:
            return PathInfo(path, toks, "openapi", "path_item", http_path=http_path)
        head = rest[0]
        if head.lower() in HTTP_METHODS:
            return _parse_operation(path, toks, http_path, head.lower(), rest[1:])
        if head == "parameters":
            return _parse_parameters(path, toks, http_path, None, rest[1:])
        if head in DOC_KEYS:
            return PathInfo(path, toks, "openapi", "doc", http_path=http_path)
        if head in SECURITY_TOKENS:
            return PathInfo(path, toks, "openapi", "security", http_path=http_path)
        return PathInfo(path, toks, "openapi", "other", http_path=http_path)
    if root == "webhooks" and len(toks) >= 2:
        # treat webhooks like path items keyed by name
        name = toks[1]
        rest = toks[2:]
        if rest and rest[0].lower() in HTTP_METHODS:
            return _parse_operation(path, toks, f"webhook:{name}", rest[0].lower(), rest[1:])
        return PathInfo(
            path, toks, "openapi", "path_item" if not rest else "other", http_path=f"webhook:{name}"
        )
    if root == "components" and len(toks) >= 2:
        section = toks[1]
        name = toks[2] if len(toks) >= 3 else None
        if section == "securitySchemes":
            if len(toks) >= 4 and toks[-1] in DOC_KEYS:
                return PathInfo(path, toks, "openapi", "doc", name=name)
            return PathInfo(path, toks, "openapi", "security", name=name)
        if section == "schemas":
            if name is None:
                return PathInfo(path, toks, "openapi", "other")
            return _finish_schema_node(path, toks, "schema", "shared", name=name, offset=3)
        if section in ("parameters", "requestBodies", "headers"):
            if name is None:
                return PathInfo(path, toks, "openapi", "other")
            return _finish_schema_node(
                path,
                toks,
                "parameter" if section != "requestBodies" else "request_body",
                "shared",
                name=name,
                offset=3,
                field_default=name if section != "requestBodies" else None,
            )
        if section == "responses":
            if name is None:
                return PathInfo(path, toks, "openapi", "other")
            return _finish_schema_node(path, toks, "response", "shared", name=name, offset=3)
        return PathInfo(path, toks, "openapi", "other", name=name)
    if root in SECURITY_TOKENS:
        return PathInfo(path, toks, "openapi", "security")
    if root == "servers":
        return PathInfo(path, toks, "openapi", "other")
    if root in ("host", "basePath"):
        return PathInfo(path, toks, "openapi", "other")

    # ---- MCP -------------------------------------------------------------------
    if root in MCP_COLLECTIONS and len(toks) >= 2:
        name = toks[1]
        rest = toks[2:]
        noun = MCP_COLLECTIONS[root]
        if not rest:
            return PathInfo(path, toks, "mcp", noun, name=name)
        head = rest[0]
        if head in ("inputSchema", "input_schema"):
            return _finish_schema_node(path, toks, "input_schema", "request", name=name, offset=3)
        if head in ("outputSchema", "output_schema"):
            return _finish_schema_node(path, toks, "output_schema", "response", name=name, offset=3)
        if head == "arguments":
            return _parse_parameters(
                path, toks, None, None, rest[1:], dialect="mcp", name=name, kind="arguments"
            )
        if head in DOC_KEYS:
            return PathInfo(path, toks, "mcp", "doc", name=name)
        if head == "deprecated":
            return PathInfo(path, toks, "mcp", "deprecated_flag", name=name)
        if head in ("annotations", "mimeType", "uri", "uriTemplate", "size", "icons", "_meta"):
            return PathInfo(path, toks, "mcp", "metadata", name=name)
        return PathInfo(path, toks, "mcp", "other", name=name)

    # ---- packages --------------------------------------------------------------
    if root in PACKAGE_ROOTS:
        return PathInfo(path, toks, "package", "version", name=toks[1] if len(toks) >= 2 else None)

    # ---- spec-level metadata ---------------------------------------------------
    if root in METADATA_ROOTS or root.startswith("x-"):
        return PathInfo(path, toks, "openapi" if root != "serverInfo" else "mcp", "metadata")
    return PathInfo(path, toks, "other", "other")


def _parse_operation(
    path: str, toks: tuple[str, ...], http_path: str, method: str, sub: tuple[str, ...]
) -> PathInfo:
    if not sub:
        return PathInfo(path, toks, "openapi", "operation", http_path=http_path, method=method)
    head = sub[0]
    if head == "parameters":
        return _parse_parameters(path, toks, http_path, method, sub[1:])
    if head == "requestBody":
        return _finish_schema_node(
            path, toks, "request_body", "request", http_path=http_path, method=method, offset=4
        )
    if head == "responses":
        status = sub[1] if len(sub) >= 2 else None
        return _finish_schema_node(
            path,
            toks,
            "response",
            "response",
            http_path=http_path,
            method=method,
            offset=5,
            status=status,
        )
    if head in SECURITY_TOKENS:
        return PathInfo(path, toks, "openapi", "security", http_path=http_path, method=method)
    if head == "deprecated":
        return PathInfo(
            path, toks, "openapi", "deprecated_flag", http_path=http_path, method=method
        )
    if head in DOC_KEYS:
        return PathInfo(path, toks, "openapi", "doc", http_path=http_path, method=method)
    if head.startswith("x-") or head in ("tags", "operationId", "servers", "callbacks"):
        return PathInfo(path, toks, "openapi", "metadata", http_path=http_path, method=method)
    return PathInfo(path, toks, "openapi", "other", http_path=http_path, method=method)


def _parse_parameters(
    path: str,
    toks: tuple[str, ...],
    http_path: str | None,
    method: str | None,
    sub: tuple[str, ...],
    *,
    dialect: Dialect = "openapi",
    name: str | None = None,
    kind: str = "parameter",
) -> PathInfo:
    """``.../parameters/<i or name>[/schema/...]`` (OpenAPI) or ``.../arguments/<i>`` (MCP)."""
    idx: int | None = None
    fld: str | None = None
    if sub:
        if _is_int(sub[0]):
            idx = int(sub[0])
        else:
            fld = sub[0]
    deeper = sub[1:]
    # a parameter's own schema may carry properties (deep objects) - keep the param name
    if deeper and fld is None and _schema_field(deeper):
        fld = _schema_field(deeper)
    base_kind = kind
    if deeper and deeper[-1] == "deprecated":
        base_kind = "deprecated_flag"
    if deeper and deeper[-1] in DOC_KEYS:
        base_kind = "doc"
    return PathInfo(
        path,
        toks,
        dialect,
        base_kind,
        http_path=http_path,
        method=method,
        name=name,
        side="request",
        field=fld,
        index=idx,
    )


def _finish_schema_node(
    path: str,
    toks: tuple[str, ...],
    kind: str,
    side: Side,
    *,
    http_path: str | None = None,
    method: str | None = None,
    name: str | None = None,
    offset: int,
    status: str | None = None,
    field_default: str | None = None,
) -> PathInfo:
    """Pointers below a schema-bearing node: pick up the property name and doc/deprecated leaves."""
    sub = toks[offset:]
    fld = _schema_field(sub) or field_default
    idx = int(toks[-1]) if toks and _is_int(toks[-1]) else None
    k = kind
    if sub and sub[-1] == "deprecated":
        k = "deprecated_flag"
    elif sub and sub[-1] in DOC_KEYS and (len(sub) == 1 or sub[-2] != "properties"):
        k = "doc"
    return PathInfo(
        path,
        toks,
        "openapi" if kind in ("request_body", "response", "schema", "parameter") else "mcp",
        k,
        http_path=http_path,
        method=method,
        name=name,
        side=side,
        field=fld,
        index=idx,
        status=status,
    )


# ---- public helpers --------------------------------------------------------------------


def affected_operation(path: str, *, display_name: str | None = None) -> str:
    """``/paths/~1v1~1charges/post/parameters/3`` -> ``"POST /v1/charges"``;
    ``/tools/get_weather/inputSchema`` -> ``"tool:get_weather"``; ``/latest`` ->
    ``"package:<display_name>"`` (``"package"`` without one). ``""`` when the pointer is not
    something a consumer calls (spec metadata)."""
    return parse_path(path).operation(display_name)


def is_request_side(path: str) -> bool:
    """Parameters, request bodies, MCP ``inputSchema`` and prompt arguments. Shared
    component schemas count as both sides."""
    return parse_path(path).side in ("request", "shared")


def is_response_side(path: str) -> bool:
    """Responses, MCP ``outputSchema``. Shared component schemas count as both sides."""
    return parse_path(path).side in ("response", "shared")


# --------------------------------------------------------------------------------------
# Op normalisation: map generic key-wise diff artefacts onto semantic ops
# --------------------------------------------------------------------------------------


@dataclass
class _Op:
    op: ChangeOp
    info: PathInfo
    origin: int  # index of the source op in the event (for "+N more" counting)


def _parent_path(info: PathInfo) -> str:
    return json_pointer(*info.parent_tokens) if info.parent_tokens else "/"


def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.lower() in ("true", "yes", "1"))


def _normalise_one(op: ChangeOp, origin: int) -> list[_Op]:
    info = parse_path(op.path)
    kind, leaf = op.op, info.leaf

    # security: anything but wording changes is an auth change
    if info.kind == "security" and kind != "auth_changed" and kind != "text_changed":
        return [
            _Op(
                ChangeOp(op="auth_changed", path=op.path, before=op.before, after=op.after),
                info,
                origin,
            )
        ]
    if info.kind == "security" and kind == "text_changed" and leaf not in DOC_KEYS:
        return [
            _Op(
                ChangeOp(op="auth_changed", path=op.path, before=op.before, after=op.after),
                info,
                origin,
            )
        ]

    # deprecated flag flipped by a generic diff
    if info.kind == "deprecated_flag" and kind in ("added", "type_changed", "text_changed"):
        if _truthy(op.after):
            parent = _parent_path(info)
            return [
                _Op(
                    ChangeOp(op="deprecated", path=parent, before=op.before, after=op.after),
                    parse_path(parent),
                    origin,
                )
            ]
        return []
    if info.kind == "deprecated_flag" and kind == "removed":
        return []

    # `required` edits by a generic diff
    if (
        leaf == "required"
        and kind in ("added", "removed", "type_changed", "text_changed")
        and info.side is not None
    ):
        parent = _parent_path(info)
        pinfo = parse_path(parent)
        out: list[_Op] = []
        if isinstance(op.after, bool) or isinstance(op.before, bool):
            if _truthy(op.after):
                out.append(
                    _Op(
                        ChangeOp(
                            op="required_added", path=parent, before=op.before, after=op.after
                        ),
                        pinfo,
                        origin,
                    )
                )
            elif op.after is not None or kind == "removed":
                out.append(
                    _Op(
                        ChangeOp(
                            op="required_removed", path=parent, before=op.before, after=op.after
                        ),
                        pinfo,
                        origin,
                    )
                )
            return out
        before_l = op.before if isinstance(op.before, list) else []
        after_l = op.after if isinstance(op.after, list) else []
        added = [x for x in after_l if x not in before_l]
        removed = [x for x in before_l if x not in after_l]
        if added:
            out.append(
                _Op(
                    ChangeOp(op="required_added", path=op.path, before=before_l, after=after_l),
                    info,
                    origin,
                )
            )
        if removed:
            out.append(
                _Op(
                    ChangeOp(op="required_removed", path=op.path, before=before_l, after=after_l),
                    info,
                    origin,
                )
            )
        return out
    if (
        len(info.tokens) >= 2
        and info.tokens[-2] == "required"
        and _is_int(leaf)
        and info.side is not None
    ):
        parent = _parent_path(info)
        pinfo = parse_path(parent)
        out = []
        if kind in ("added", "text_changed") and isinstance(op.after, str):
            out.append(
                _Op(
                    ChangeOp(op="required_added", path=parent, before=[], after=[op.after]),
                    pinfo,
                    origin,
                )
            )
        if kind in ("removed", "text_changed") and isinstance(op.before, str):
            out.append(
                _Op(
                    ChangeOp(op="required_removed", path=parent, before=[op.before], after=[]),
                    pinfo,
                    origin,
                )
            )
        return out

    # enum edits by a generic diff
    if leaf == "enum" and kind in ("added", "removed"):
        parent = _parent_path(info)
        pinfo = parse_path(parent)
        if kind == "added" and isinstance(op.after, list):
            # a constraint was introduced: the field is now restricted
            return [
                _Op(
                    ChangeOp(op="type_changed", path=parent, before=None, after=op.after),
                    pinfo,
                    origin,
                )
            ]
        if kind == "removed" and isinstance(op.before, list):
            # the constraint was dropped: more values are now possible
            return [
                _Op(
                    ChangeOp(op="enum_changed", path=op.path, before=[], after=op.before),
                    info,
                    origin,
                )
            ]
        return []
    if len(info.tokens) >= 2 and info.tokens[-2] == "enum" and _is_int(leaf):
        parent = _parent_path(info)
        pinfo = parse_path(parent)
        before_l = [op.before] if op.before is not None and kind != "added" else []
        after_l = [op.after] if op.after is not None and kind != "removed" else []
        return [
            _Op(
                ChangeOp(op="enum_changed", path=parent, before=before_l, after=after_l),
                pinfo,
                origin,
            )
        ]

    # `type` / `$ref` edits by a generic diff
    if (
        leaf in TYPE_KEYS
        and kind in ("text_changed", "type_changed", "added", "removed")
        and info.side is not None
    ):
        parent = _parent_path(info)
        pinfo = parse_path(parent)
        return [
            _Op(
                ChangeOp(op="type_changed", path=parent, before=op.before, after=op.after),
                pinfo,
                origin,
            )
        ]

    return [_Op(op, info, origin)]


def normalise_ops(ops: list[ChangeOp]) -> list[_Op]:
    out: list[_Op] = []
    for i, op in enumerate(ops):
        out.extend(_normalise_one(op, i))
    return out


def _post_normalise(ops: list[_Op]) -> list[_Op]:
    """Runs after rename detection (which needs the raw ``added`` ops): a new parameter
    whose definition already says ``required: true`` is a ``required_added``."""
    out: list[_Op] = []
    for o in ops:
        op, info = o.op, o.info
        if (
            op.op == "added"
            and info.kind in ("parameter", "arguments")
            and isinstance(op.after, dict)
            and _truthy(op.after.get("required"))
        ):
            out.append(
                _Op(
                    ChangeOp(op="required_added", path=op.path, before=op.before, after=op.after),
                    info,
                    o.origin,
                )
            )
        else:
            out.append(o)
    return out


# --------------------------------------------------------------------------------------
# Rename detection (removed + added pairs the watcher did not fuse)
# --------------------------------------------------------------------------------------

_RENAMEABLE = {
    "tool",
    "resource",
    "prompt",
    "path_item",
    "parameter",
    "request_body",
    "response",
    "schema",
    "input_schema",
    "output_schema",
    "arguments",
}


def _rename_name(o: _Op) -> str | None:
    """The identifier a rename would change."""
    info, op = o.info, o.op
    if info.kind in ("tool", "resource", "prompt"):
        return info.name
    if info.kind == "path_item":
        return info.http_path
    if info.kind == "schema" and info.field is None:
        return info.name
    if info.field is not None:
        return info.field
    val = op.before if op.op == "removed" else op.after
    if isinstance(val, dict) and isinstance(val.get("name"), str):
        return val["name"]
    return None


def _rename_group(o: _Op) -> tuple[Any, ...]:
    info = o.info
    if info.kind in ("tool", "resource", "prompt"):
        return (info.dialect, info.tokens[0])
    if info.kind == "path_item":
        return (info.dialect, "paths")
    return (info.dialect, info.kind, info.side, info.parent_tokens)


def _is_empty(v: Any) -> bool:
    return v is None or (
        isinstance(v, str | dict | list) and not (v.strip() if isinstance(v, str) else v)
    )


def _property_count(schema: Any) -> int:
    """Properties of an input schema: a full JSON Schema (``properties`` dict) or the
    watcher's ``schema_summary`` (``properties`` list of names)."""
    if not isinstance(schema, dict):
        return 0
    props = schema.get("properties")
    return len(props) if isinstance(props, dict | list) else 0


def _distinctive_schema(s: Any) -> bool:
    """True when two identical definitions under different names are evidence of a
    rename. Keys whose value is empty (``None``, ``""``, ``{}``, ``[]``) carry no identity.
    A tool (a definition with ``inputSchema``, or the MCP watcher's summary with a
    ``fingerprint``) needs a description, or an input schema with at least two
    properties: two undocumented no-argument or one-argument tools are not one tool
    renamed."""
    if not isinstance(s, dict):
        return False
    s = {k: v for k, v in s.items() if not _is_empty(v)}
    if not s:
        return False
    if "inputSchema" in s or "fingerprint" in s:
        return isinstance(s.get("description"), str) or _property_count(s.get("inputSchema")) >= 2
    if any(
        k in s
        for k in (
            "properties",
            "enum",
            "items",
            "inputSchema",
            "description",
            "$ref",
            "parameters",
            "requestBody",
            "responses",
        )
    ):
        return True
    return len(s) >= 2


_IDENTITY_KEYS = frozenset({"name", "title", "operationId", "x-name"})


def _strip_identity(v: Any) -> Any:
    """Tool and parameter definitions carry their own name; drop it (top level only) so
    'same definition under a new name' compares equal."""
    if isinstance(v, dict):
        return {k: x for k, x in v.items() if k not in _IDENTITY_KEYS}
    return v


def _same_schema(a: Any, b: Any) -> bool:
    try:
        return json.dumps(_strip_identity(a), sort_keys=True, default=str) == json.dumps(
            _strip_identity(b), sort_keys=True, default=str
        )
    except (TypeError, ValueError):
        return False


def _schema_type(v: Any) -> str | None:
    if isinstance(v, dict):
        if isinstance(v.get("schema"), dict):
            return _schema_type(v["schema"])
        t = v.get("type") or v.get("$ref")
        if isinstance(t, list):
            return "|".join(str(x) for x in t)
        return str(t) if t is not None else None
    if isinstance(v, str):
        return v
    return None


def detect_renames(ops: list[_Op]) -> list[_Op]:
    """Fuse removed+added pairs into synthetic ``renamed`` ops when the names are within
    Levenshtein distance < 3 or the schemas are identical (and distinctive). Same-name
    pairs are moves: they are dropped when nothing else changed, or turned into the
    semantic op that did change (required flip, type change)."""
    removed = [
        o for o in ops if o.op.op == "removed" and o.info.kind in _RENAMEABLE and _rename_name(o)
    ]
    added = [
        o for o in ops if o.op.op == "added" and o.info.kind in _RENAMEABLE and _rename_name(o)
    ]
    if not removed or not added:
        return ops
    # score every candidate pair, then assign by global best score so an exact-schema
    # match is never pre-empted by a weaker name-distance match on another op
    candidates: list[tuple[int, int, int, _Op, _Op]] = []
    for ri, r in enumerate(removed):
        rname = _rename_name(r) or ""
        for ai, a in enumerate(added):
            if _rename_group(a) != _rename_group(r):
                continue
            aname = _rename_name(a) or ""
            score = _rename_score(rname, aname, r.op.before, a.op.after)
            if score is not None:
                candidates.append((score, ri, ai, r, a))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    consumed: set[int] = set()
    replacements: dict[int, list[_Op]] = {}
    for score, _, _, r, a in candidates:
        if id(r) in consumed or id(a) in consumed:
            continue
        consumed.add(id(a))
        consumed.add(id(r))
        if score == -1:
            replacements[id(r)] = _moved_pair(r, a)
        else:
            replacements[id(r)] = [
                _Op(
                    ChangeOp(
                        op="renamed",
                        path=r.op.path,
                        before=_rename_name(r),
                        after=_rename_name(a),
                    ),
                    r.info,
                    r.origin,
                )
            ]
    out: list[_Op] = []
    for o in ops:
        if id(o) in replacements:
            out.extend(replacements[id(o)])
        elif id(o) in consumed:
            continue
        else:
            out.append(o)
    return out


def _rename_score(rname: str, aname: str, before: Any, after: Any) -> int | None:
    """-1: same name (a move); 0: identical distinctive schema; 1-2: Levenshtein distance
    under 3 (and under half the longer name, so 1-3 character names never pair on
    distance alone); None: not a rename candidate."""
    if rname == aname:
        return -1
    if _same_schema(before, after) and _distinctive_schema(before):
        return 0
    d = levenshtein(rname, aname, max_distance=2)
    if d <= 2 and d * 2 < max(len(rname), len(aname)):
        return d
    return None


def _moved_pair(r: _Op, a: _Op) -> list[_Op]:
    """Same name removed at one index and added at another: not a rename."""
    before, after = r.op.before, a.op.after
    if _same_schema(before, after):
        return []
    out: list[_Op] = []
    if isinstance(before, dict) and isinstance(after, dict):
        if not _truthy(before.get("required")) and _truthy(after.get("required")):
            out.append(
                _Op(
                    ChangeOp(op="required_added", path=a.op.path, before=before, after=after),
                    a.info,
                    a.origin,
                )
            )
        elif _truthy(before.get("required")) and not _truthy(after.get("required")):
            out.append(
                _Op(
                    ChangeOp(op="required_removed", path=a.op.path, before=before, after=after),
                    a.info,
                    a.origin,
                )
            )
        bt, at = _schema_type(before), _schema_type(after)
        if bt != at and (bt or at):
            out.append(
                _Op(
                    ChangeOp(op="type_changed", path=a.op.path, before=bt, after=at),
                    a.info,
                    a.origin,
                )
            )
        be = _enum_of(before)
        ae = _enum_of(after)
        if be != ae and (be or ae):
            out.append(
                _Op(
                    ChangeOp(op="enum_changed", path=a.op.path, before=be or [], after=ae or []),
                    a.info,
                    a.origin,
                )
            )
    if not out:
        # something else changed (description, example): wording only
        out.append(
            _Op(
                ChangeOp(op="text_changed", path=a.op.path, before=before, after=after),
                a.info,
                a.origin,
            )
        )
    return out


def _enum_of(v: Any) -> list[Any] | None:
    if isinstance(v, dict):
        if isinstance(v.get("enum"), list):
            return list(v["enum"])
        if isinstance(v.get("schema"), dict):
            return _enum_of(v["schema"])
    if isinstance(v, list):
        return list(v)
    return None


# --------------------------------------------------------------------------------------
# Rule evaluation
# --------------------------------------------------------------------------------------


@dataclass
class Hit:
    rule: str
    summary: str
    affected: list[str] = field(default_factory=list)
    hint: str | None = None
    origin: int = -1

    @property
    def severity(self) -> Severity | None:
        return RULE_OUTCOME[self.rule][0]

    @property
    def category(self) -> Category | None:
        return RULE_OUTCOME[self.rule][1]

    @property
    def priority(self) -> int:
        return RULE_INDEX[self.rule]


# Vendor-controlled fragments (names, enum values, defaults, sunset dates, versions) go
# through ``_frag``: one line, no control characters, bounded. The final summary and hint
# are flattened and capped again in ``classify_by_rules`` (they become issue titles, issue
# bodies and weekly-report lines). Markdown escaping is the renderer's job, not ours.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
MAX_FRAGMENT_CHARS = 120
MAX_SUMMARY_CHARS = 500
MAX_HINT_CHARS = 1000
MAX_DEFAULT_CHARS = 200
MAX_VERSION_CHARS = 40


def _frag(value: Any, limit: int = MAX_FRAGMENT_CHARS) -> str:
    """``value`` as one bounded line of plain text."""
    s = value if isinstance(value, str) else str(value)
    s = " ".join(_CONTROL_RE.sub(" ", s).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _q(name: Any) -> str:
    return f"'{_frag(name)}'"


def _join_q(names: list[Any], limit: int = 5) -> str:
    shown = [_q(str(n)) for n in names[:limit]]
    extra = len(names) - limit
    return ", ".join(shown) + (f" and {extra} more" if extra > 0 else "")


def _name_of(v: Any, fallback: str | None = None) -> str | None:
    if isinstance(v, str) and v:
        return v
    if isinstance(v, dict):
        for k in ("name", "title", "operationId"):
            if isinstance(v.get(k), str) and v[k]:
                return v[k]
    return fallback


def _field_name(o: _Op) -> str:
    info, op = o.info, o.op
    if info.field:
        return info.field
    for v in (op.after, op.before):
        if isinstance(v, dict) and isinstance(v.get("name"), str):
            return v["name"]
    if info.kind in ("parameter", "arguments") and info.index is not None:
        return f"parameter #{info.index}"
    if info.kind == "request_body":
        return "request body"
    if info.kind == "response" and info.status:
        return f"response {info.status}"
    if info.kind == "schema" and info.name:
        return info.name
    return info.leaf or "value"


def _noun(info: PathInfo) -> str:
    if info.dialect == "mcp":
        return "input" if info.side == "request" else "field"
    if info.kind in ("parameter", "arguments"):
        return "parameter"
    if info.side == "request":
        return "parameter"
    return "field"


def _describe_type(v: Any) -> str:
    if v is None:
        return "unconstrained"
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        if all(isinstance(x, str | int | float | bool) or x is None for x in v):
            return "one of " + _join_q(v, 6)
        return "|".join(_describe_type(x) for x in v)
    if isinstance(v, dict):
        if isinstance(v.get("schema"), dict):
            return _describe_type(v["schema"])
        t = v.get("type")
        if isinstance(t, list):
            t = "|".join(str(x) for x in t)
        if t == "array" and isinstance(v.get("items"), dict):
            return f"array of {_describe_type(v['items'])}"
        if t:
            fmt = v.get("format")
            return f"{t} ({fmt})" if isinstance(fmt, str) else str(t)
        if isinstance(v.get("$ref"), str):
            return v["$ref"].rsplit("/", 1)[-1]
        if isinstance(v.get("enum"), list):
            return "one of " + _join_q(v["enum"], 6)
        if "properties" in v:
            return "object"
        if "oneOf" in v or "anyOf" in v:
            return "union"
        return "object"
    return type(v).__name__


def _required_names(o: _Op) -> list[str]:
    a, b = o.op.after, o.op.before
    if isinstance(a, list) and all(isinstance(x, str) for x in a):
        before = set(b) if isinstance(b, list) else set()
        names = [x for x in a if x not in before]
        return names or list(a)
    if isinstance(a, str) and a and o.info.field is None:
        return [a]
    if isinstance(a, dict) and isinstance(a.get("name"), str):
        return [a["name"]]
    return [_field_name(o)]


def _default_hint(v: Any) -> str | None:
    """A value a fixer could plausibly pass for a new required input."""
    s = v.get("schema", v) if isinstance(v, dict) else None
    if not isinstance(s, dict):
        return None
    for k in ("default", "example"):
        if k in s and s[k] is not None:
            return f"{k} {_frag(json.dumps(s[k], default=str), MAX_DEFAULT_CHARS)}"
    if isinstance(s.get("examples"), list) and s["examples"]:
        return f"example {_frag(json.dumps(s['examples'][0], default=str), MAX_DEFAULT_CHARS)}"
    if isinstance(s.get("enum"), list) and s["enum"]:
        return "one of " + _join_q(s["enum"], 4)
    return None


def _methods_of(v: Any) -> list[str]:
    if isinstance(v, dict):
        return [k.upper() for k in v if isinstance(k, str) and k.lower() in HTTP_METHODS]
    return []


def _hit(rule: str, summary: str, o: _Op, affected: list[str], hint: str | None = None) -> Hit:
    if summary[:1].islower() and o.info.dialect != "package":
        # labels like "tool get_weather" read better capitalised at sentence start
        summary = summary[0].upper() + summary[1:]
    return Hit(
        rule=rule, summary=summary, affected=[p for p in affected if p], hint=hint, origin=o.origin
    )


def _inlined_required(ops: list[_Op]) -> frozenset[str]:
    """Names made required by ``required_added`` ops at operation level (under ``/paths`` or
    ``/webhooks``): what the watcher reports where a component schema is inlined."""
    return frozenset(
        name
        for o in ops
        if o.op.op == "required_added" and o.info.dialect == "openapi" and o.info.http_path
        for name in _required_names(o)
    )


def _evaluate_op(
    o: _Op, display_name: str | None, inlined_required: frozenset[str] = frozenset()
) -> list[Hit]:
    """The hits one (normalised) op produces. ``inlined_required`` is
    ``_inlined_required`` of the whole event."""
    op, info = o.op, o.info
    where = info.operation(display_name)
    label = info.label(display_name)
    kind = op.op

    if kind == "removed":
        if info.kind == "path_item":
            methods = _methods_of(op.before)
            affected = [f"{m} {info.http_path}" for m in methods] or [where]
            tail = f" ({', '.join(methods)})" if methods else ""
            return [
                _hit(
                    RULE_REMOVED_OPERATION,
                    f"Path {_frag(info.http_path)} was removed{tail}",
                    o,
                    affected,
                    f"Remove or replace calls to {_frag(info.http_path)}",
                )
            ]
        if info.kind == "operation":
            return [
                _hit(
                    RULE_REMOVED_OPERATION,
                    f"{label} was removed",
                    o,
                    [where],
                    f"Remove or replace calls to {label}",
                )
            ]
        if info.kind == "tool":
            return [
                _hit(
                    RULE_REMOVED_TOOL,
                    f"Tool {_frag(info.name)} was removed",
                    o,
                    [where],
                    f"Stop calling tool {_frag(info.name)} or switch to a replacement tool",
                )
            ]
        if info.kind == "resource":
            return [
                _hit(
                    RULE_REMOVED_RESOURCE,
                    f"Resource {_frag(info.name)} was removed",
                    o,
                    [where],
                    f"Stop reading resource {_frag(info.name)}",
                )
            ]
        if info.kind == "prompt":
            return [
                _hit(
                    RULE_REMOVED_PROMPT,
                    f"Prompt {_frag(info.name)} was removed",
                    o,
                    [where],
                    f"Stop using prompt {_frag(info.name)}",
                )
            ]
        name = _field_name(o)
        if info.side == "response" or (
            info.side == "shared" and info.kind in ("schema", "response")
        ):
            if info.kind == "response" and info.field is None and info.status:
                return [
                    _hit(
                        RULE_REMOVED_RESPONSE_FIELD,
                        f"Response {info.status} of {label} was removed",
                        o,
                        [where],
                        f"Handle the new response shape of {label}",
                    )
                ]
            origin = (
                f"the response of {label}"
                if info.dialect == "openapi"
                else f"the output of {label}"
            )
            if info.kind == "schema":
                origin = label
            return [
                _hit(
                    RULE_REMOVED_RESPONSE_FIELD,
                    f"Field {_q(name)} was removed from {origin}",
                    o,
                    [where],
                    f"Stop reading {_q(name)} from {origin}",
                )
            ]
        if info.side in ("request", "shared"):
            noun = _noun(info)
            if info.kind == "request_body" and info.field is None:
                return [
                    _hit(
                        RULE_REMOVED_REQUEST_FIELD,
                        f"{label}: the request body was removed",
                        o,
                        [where],
                        f"Stop sending a request body to {label}",
                    )
                ]
            return [
                _hit(
                    RULE_REMOVED_REQUEST_FIELD,
                    f"{noun.capitalize()} {_q(name)} was removed from {label}",
                    o,
                    [where],
                    f"Stop passing {_q(name)} to {label}",
                )
            ]
        if info.kind == "metadata":
            return [
                _hit(
                    RULE_METADATA_ONLY,
                    f"Spec metadata changed ({_frag('/'.join(info.tokens[:2]))})",
                    o,
                    [],
                )
            ]
        if info.kind == "doc":
            return [_hit(RULE_TEXT_CHANGED, f"{label}: description changed", o, [where])]
        return []

    if kind == "required_added":
        names = _required_names(o)
        if info.side == "shared" and info.kind == "schema" and set(names) <= inlined_required:
            # a component schema (components/schemas) is no side by itself: the watcher
            # also reports the change under every operation that inlines it, where it is
            # a request input (breaking) or a response field now always present (safe),
            # and those ops decide. A component used too deep to be inlined has no such
            # op and stays breaking below.
            return [
                _hit(
                    RULE_REQUIRED_ADDED_SHARED,
                    f"{label}: {_join_q(names)} now required",
                    o,
                    [where],
                )
            ]
        response_like = info.side == "response" or (
            info.side == "shared" and info.kind == "response"
        )
        if not response_like and (
            info.side in ("request", "shared")
            or info.kind in ("input_schema", "arguments", "parameter")
        ):
            noun = _noun(info)
            if len(names) == 1:
                summary = f"{label}: required {noun} {_q(names[0])} was added"
            else:
                summary = f"{label}: required {noun}s {_join_q(names)} were added"
            dflt = _default_hint(op.after)
            hint = f"Pass {_join_q(names)} in calls to {label}" + (f" ({dflt})" if dflt else "")
            return [_hit(RULE_REQUIRED_ADDED, summary, o, [where], hint)]
        if response_like:
            return [
                _hit(
                    RULE_REQUIRED_ADDED_RESPONSE,
                    f"{label}: response field {_join_q(names)} is now always present",
                    o,
                    [where],
                )
            ]
        return []

    if kind == "required_removed":
        names: list[str]
        b, a = op.before, op.after
        if isinstance(b, list) and all(isinstance(x, str) for x in b):
            after = set(a) if isinstance(a, list) else set()
            names = [x for x in b if x not in after] or list(b)
        else:
            names = [_field_name(o)]
        if info.side == "response":
            return [
                _hit(
                    RULE_REQUIRED_REMOVED,
                    f"{label}: response field {_join_q(names)} may now be absent",
                    o,
                    [where],
                )
            ]
        return [
            _hit(
                RULE_REQUIRED_REMOVED,
                f"{label}: {_noun(info)} {_join_q(names)} is no longer required",
                o,
                [where],
            )
        ]

    if kind == "type_changed":
        if info.side is not None or info.kind in ("tool", "input_schema", "output_schema"):
            name = _field_name(o)
            bt, at = _frag(_describe_type(op.before)), _frag(_describe_type(op.after))
            noun = _noun(info).capitalize()
            if info.kind in ("input_schema", "output_schema") and info.field is None:
                summary = f"{label}: the {'input' if info.side == 'request' else 'output'} schema changed shape"
            else:
                summary = f"{noun} {_q(name)} on {label} changed type from {bt} to {at}"
            return [
                _hit(
                    RULE_TYPE_CHANGED,
                    summary,
                    o,
                    [where],
                    f"Update callers of {label}: {_q(name)} is now {at} (was {bt})",
                )
            ]
        if info.kind == "metadata":
            return [
                _hit(
                    RULE_METADATA_ONLY,
                    f"Spec metadata changed ({_frag('/'.join(info.tokens[:2]))})",
                    o,
                    [],
                )
            ]
        return []

    if kind == "renamed":
        old = _frag(_name_of(op.before, _rename_name(o) or _field_name(o)) or "?")
        new = _frag(_name_of(op.after, None) or "?")
        if info.kind in ("tool", "resource", "prompt"):
            noun = MCP_COLLECTIONS.get(info.tokens[0], "tool").capitalize()
            return [
                _hit(
                    RULE_RENAMED,
                    f"{noun} {old} was renamed to {new}",
                    o,
                    [where],
                    f"Rename {old} to {new}",
                )
            ]
        if info.kind == "path_item":
            return [
                _hit(
                    RULE_RENAMED,
                    f"Path {old} was renamed to {new}",
                    o,
                    [where],
                    f"Rename {old} to {new} in request URLs",
                )
            ]
        if info.kind == "operation":
            return [
                _hit(
                    RULE_RENAMED,
                    f"{label} was renamed to {new}",
                    o,
                    [where],
                    f"Rename {old} to {new}",
                )
            ]
        noun = _noun(info)
        return [
            _hit(
                RULE_RENAMED,
                f"{label}: {noun} {_q(old)} was renamed to {_q(new)}",
                o,
                [where],
                f"Rename {old} to {new} in calls to {label}",
            )
        ]

    if kind == "enum_changed":
        removed_v, added_v = _enum_delta(op)
        name = _field_name(o)
        hits: list[Hit] = []
        if removed_v:
            hits.append(
                _hit(
                    RULE_ENUM_VALUES_REMOVED,
                    f"{label}: enum value{'s' if len(removed_v) > 1 else ''} {_join_q(removed_v)} {'were' if len(removed_v) > 1 else 'was'} removed from {_q(name)}",
                    o,
                    [where],
                    f"Stop sending {_join_q(removed_v)} for {_q(name)} in calls to {label}",
                )
            )
        if added_v:
            hits.append(
                _hit(
                    RULE_ENUM_VALUES_ADDED,
                    f"{label}: enum value{'s' if len(added_v) > 1 else ''} {_join_q(added_v)} {'were' if len(added_v) > 1 else 'was'} added to {_q(name)}",
                    o,
                    [where],
                    f"Handle the new value{'s' if len(added_v) > 1 else ''} {_join_q(added_v)} for {_q(name)}",
                )
            )
        return hits

    if kind == "auth_changed":
        added = _auth_option_added(info, op, label)
        if added is not None:
            # an extra way to authenticate: existing credentials keep working, so no
            # '*' fan-out to every consumer of the source
            affected = [where] if info.http_path is not None else []
            return [_hit(RULE_ADDED_OPTIONAL, added, o, affected)]
        if info.http_path is not None:
            summary = f"{label}: authentication requirements changed"
        elif info.name:
            summary = f"Security scheme {_q(info.name)} changed"
        else:
            summary = "Global security requirements changed"
        return [
            _hit(
                RULE_AUTH_CHANGED,
                summary,
                o,
                [where],
                f"Review the credentials and scopes used for {label}",
            )
        ]

    if kind == "deprecated":
        if info.dialect == "package":
            # the package watcher flags a changed major at /latest with bare majors
            # ('11' -> '12'), whatever the direction; the event-level rules decide from the
            # full versions (major_bump, or version_bump for a rollback or yank)
            return []
        sunset = _sunset_of(op.after)
        tail = f" (sunset {sunset})" if sunset else ""
        target = label if info.field is None else f"{_noun(info)} {_q(_field_name(o))} of {label}"
        return [
            _hit(
                RULE_DEPRECATED,
                f"{target[0].upper() + target[1:]} is deprecated{tail}",
                o,
                [where],
                f"Migrate away from {target} before it is removed",
            )
        ]

    if kind == "added":
        if info.kind == "path_item":
            methods = _methods_of(op.after)
            affected = [f"{m} {info.http_path}" for m in methods] or [where]
            return [
                _hit(RULE_ADDED_OPTIONAL, f"Path {_frag(info.http_path)} was added", o, affected)
            ]
        if info.kind == "operation":
            return [_hit(RULE_ADDED_OPTIONAL, f"{label} was added", o, [where])]
        if info.kind in ("tool", "resource", "prompt"):
            noun = MCP_COLLECTIONS.get(info.tokens[0], "tool").capitalize()
            return [_hit(RULE_ADDED_OPTIONAL, f"{noun} {_frag(info.name)} was added", o, [where])]
        if info.kind == "doc":
            return [_hit(RULE_ADDED_DESCRIPTION, f"{label}: {info.leaf} was added", o, [where])]
        if info.side in ("request", "shared"):
            name = _field_name(o)
            if info.kind == "request_body" and info.field is None:
                return [
                    _hit(
                        RULE_ADDED_OPTIONAL,
                        f"{label}: an optional request body was added",
                        o,
                        [where],
                    )
                ]
            return [
                _hit(
                    RULE_ADDED_OPTIONAL,
                    f"{label}: optional {_noun(info)} {_q(name)} was added",
                    o,
                    [where],
                )
            ]
        if info.side == "response":
            name = _field_name(o)
            if info.kind == "response" and info.field is None and info.status:
                return [
                    _hit(
                        RULE_ADDED_OPTIONAL,
                        f"{label}: response {info.status} was added",
                        o,
                        [where],
                    )
                ]
            origin = (
                f"the response of {label}"
                if info.dialect == "openapi"
                else f"the output of {label}"
            )
            return [
                _hit(RULE_ADDED_OPTIONAL, f"Field {_q(name)} was added to {origin}", o, [where])
            ]
        if info.kind == "metadata":
            return [
                _hit(
                    RULE_METADATA_ONLY,
                    f"Spec metadata changed ({_frag('/'.join(info.tokens[:2]))})",
                    o,
                    [],
                )
            ]
        if info.kind == "version":
            return []  # event-level rules read the versions
        return []

    if kind == "text_changed":
        if info.kind == "metadata":
            return [
                _hit(
                    RULE_METADATA_ONLY,
                    f"Spec metadata changed ({_frag('/'.join(info.tokens[:2]))})",
                    o,
                    [],
                )
            ]
        if info.kind == "version":
            return []
        what = "description" if info.kind == "doc" else "text"
        return [_hit(RULE_TEXT_CHANGED, f"{label}: {what} changed", o, [where])]

    return []


def _auth_option_added(info: PathInfo, op: ChangeOp, label: str) -> str | None:
    """A summary when an auth op only adds an option existing consumers need not use: a
    new security scheme, or a new alternative security requirement while every existing
    one stays (requirements are OR'd). ``None`` for everything else: a removed or modified
    scheme, a removed alternative, changed scopes, security newly required where there
    was none (an empty or inherited list before)."""
    toks = info.tokens
    if (
        info.http_path is None
        and len(toks) == 3
        and toks[:2] == ("components", "securitySchemes")
        and op.before is None
        and isinstance(op.after, dict)
    ):
        return f"Security scheme {_q(toks[2])} was added"
    alternative = (
        f"{label}: an alternative security requirement was added"
        if info.http_path is not None
        else "An alternative global security requirement was added"
    )
    if toks and toks[-1] == "security":
        b, a = op.before, op.after
        if (
            isinstance(b, list)
            and isinstance(a, list)
            and b
            and len(a) > len(b)
            and all(req in a for req in b)
        ):
            return alternative
        return None
    if (
        len(toks) >= 2
        and toks[-2] == "security"
        and _is_int(toks[-1])
        and int(toks[-1]) >= 1  # index 0 added means the list was empty: auth newly required
        and op.before is None
        and isinstance(op.after, dict)
    ):
        return alternative
    return None


def _enum_delta(op: ChangeOp) -> tuple[list[Any], list[Any]]:
    before = _enum_of(op.before)
    after = _enum_of(op.after)
    if (
        before is None
        and isinstance(op.after, dict)
        and ("removed" in op.after or "added" in op.after)
    ):
        return list(op.after.get("removed") or []), list(op.after.get("added") or [])
    before = before or []
    after = after or []
    removed = [v for v in before if v not in after]
    added = [v for v in after if v not in before]
    return removed, added


def _sunset_of(v: Any) -> str | None:
    if isinstance(v, dict):
        for k in ("x-sunset", "sunset", "x-sunset-date", "removal_date", "x-removal-date"):
            if isinstance(v.get(k), str) and v[k].strip():
                return _frag(v[k], MAX_VERSION_CHARS)
    return None


# ---- event-level rules (package versions) ----------------------------------------------

_VERSION_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _version_tuple(v: str | None) -> tuple[int, int, int] | None:
    if not v:
        return None
    m = _VERSION_RE.match(v)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


def is_major_bump(from_version: str | None, to_version: str | None) -> bool:
    """Semver major bump, with 0.x minor bumps counted as major (caret-range semantics)."""
    a, b = _version_tuple(from_version), _version_tuple(to_version)
    if a is None or b is None:
        return False
    if b[0] != a[0]:
        return b[0] > a[0]
    if a[0] == 0 and b[1] != a[1]:
        return b[1] > a[1]
    return False


def _major_label(v: str | None) -> str:
    t = _version_tuple(v)
    if t is None:
        return _frag(v, MAX_VERSION_CHARS) if v else "?"
    if t[0] == 0:
        return f"0.{t[1]}.x"
    return f"{t[0]}.x"


def _bump_summary(pkg: str, frm: str, to: str, *, major: bool) -> str:
    pkg, frm, to = _frag(pkg), _frag(frm, MAX_VERSION_CHARS), _frag(to, MAX_VERSION_CHARS)
    if major:
        return f"{pkg} {_major_label(frm)} -> {to or '?'}: major version bump"
    a, b = _version_tuple(frm), _version_tuple(to)
    if a is not None and b is not None and b < a:
        return f"{pkg} {frm} -> {to}: latest version moved back (yanked or rolled back)"
    return f"{pkg} {frm or '?'} -> {to or '?'}: new release"


def _bump_hint(pkg: str, frm: str, to: str) -> str:
    pkg, frm, to = _frag(pkg), _frag(frm, MAX_VERSION_CHARS), _frag(to, MAX_VERSION_CHARS)
    return f"Upgrade {pkg} from {frm or 'the previous major'} to {to or 'the new major'} and follow the migration notes for the {_major_label(to)} series"


def _package_versions(event: ChangeEvent) -> tuple[str | None, str | None]:
    """The event's versions, else the full versions from the ops at ``/latest``; the
    watcher's ``deprecated`` op there carries bare majors, so it is the last resort."""
    frm, to = event.from_version, event.to_version
    for op in sorted(event.ops, key=lambda o: o.op == "deprecated"):
        toks = split_pointer(op.path)
        if not toks:
            continue
        if toks[0] in ("latest", "dist-tags", "version"):
            if isinstance(op.before, str) and not frm:
                frm = op.before
            if isinstance(op.after, str) and not to:
                to = op.after
            if isinstance(op.after, dict) and isinstance(op.after.get("version"), str) and not to:
                to = op.after["version"]
        elif toks[0] in ("versions", "releases") and len(toks) >= 2 and op.op == "added" and not to:
            to = toks[1]
    return frm, to


def _evaluate_event(event: ChangeEvent, display_name: str | None, ops: list[_Op]) -> list[Hit]:
    if event.source_kind not in ("pypi", "npm"):
        return []
    frm, to = _package_versions(event)
    if not to:
        return []
    pkg = display_name or "package"
    where = f"package:{display_name}" if display_name else "package"
    if frm and is_major_bump(frm, to):
        return [
            Hit(
                RULE_MAJOR_BUMP,
                _bump_summary(pkg, frm, to, major=True),
                [where],
                _bump_hint(pkg, frm, to),
                origin=-1,
            )
        ]
    if frm != to:
        return [
            Hit(
                RULE_VERSION_BUMP,
                _bump_summary(pkg, frm or "", to, major=False),
                [where],
                None,
                origin=-1,
            )
        ]
    return []


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


def _dedup(items: list[str]) -> list[str]:
    return [x for x in dict.fromkeys(items) if x]


def classify_by_rules(
    event: ChangeEvent, *, display_name: str | None = None
) -> Classification | None:
    """Apply the rules. ``None`` means no rule decided a severity (route to the model)."""
    ops = _post_normalise(detect_renames(normalise_ops(event.ops)))
    inlined = _inlined_required(ops)
    hits: list[Hit] = []
    for o in ops:
        hits.extend(_evaluate_op(o, display_name, inlined))
    hits.extend(_evaluate_event(event, display_name, ops))
    deciding = [h for h in hits if h.severity is not None]
    if not deciding:
        return None
    ordered = sorted(
        enumerate(deciding),
        key=lambda ih: (SEVERITY_RANK[ih[1].severity or "unknown"], ih[1].priority, ih[0]),
    )
    dominant = ordered[0][1]
    origins = {h.origin for h in hits if h.origin >= 0}
    extra = len(origins) - (1 if dominant.origin in origins else 0)
    if dominant.origin < 0 and origins:
        extra = len(origins)
    summary = dominant.summary
    if extra > 0:
        summary += f" (+{extra} more change{'s' if extra != 1 else ''})"
    if (
        display_name
        and not summary.startswith(_frag(display_name))
        and not summary.startswith(f"[{_frag(display_name)}]")
    ):
        summary = f"[{_frag(display_name)}] {summary}"
    summary = _frag(summary, MAX_SUMMARY_CHARS)
    hint = dominant.hint or next((h.hint for _, h in ordered if h.hint), None)
    hint = _frag(hint, MAX_HINT_CHARS) if hint else None
    rule_hits = _dedup([h.rule for _, h in ordered] + [h.rule for h in hits if h.severity is None])
    affected = _dedup([p for h in hits for p in h.affected])
    return Classification(
        severity=dominant.severity or "unknown",
        category=dominant.category or "behaviour",
        summary=summary,
        affected_paths=affected,
        migration_hint=hint,
        rule_hits=rule_hits,
        llm_confidence=None,
    )


def classify_event(event: ChangeEvent, display_name: str | None = None) -> Classification:
    """Rules, else the ``unknown`` placeholder the CLI prints (the server sends those to
    the model instead)."""
    result = classify_by_rules(event, display_name=display_name)
    if result is not None:
        return result
    infos = [parse_path(op.path) for op in event.ops]
    text_ops = [
        (op, info) for op, info in zip(event.ops, infos, strict=True) if op.op == "text_changed"
    ]
    n = len(text_ops)
    if text_ops and all(info.kind == "doc" for _, info in text_ops):
        category: Category = "docs_only"
    else:
        category = "behaviour"
    if n:
        summary = f"{n} text-only change{'s' if n != 1 else ''}; needs review"
    elif event.ops:
        summary = f"{len(event.ops)} unclassified change{'s' if len(event.ops) != 1 else ''}; needs review"
    else:
        summary = "No classifiable changes; needs review"
    if display_name:
        summary = f"[{_frag(display_name)}] {summary}"
    affected = _dedup([info.operation(display_name) for info in infos])
    return Classification(
        severity="unknown",
        category=category,
        summary=summary,
        affected_paths=affected,
        migration_hint=None,
        rule_hits=[RULE_TEXT_ONLY],
        llm_confidence=None,
    )
