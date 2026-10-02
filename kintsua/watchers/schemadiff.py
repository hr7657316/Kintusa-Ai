"""Semantic JSON Schema diff shared by the MCP and OpenAPI watchers.

``diff_json_schema(before, after, base_pointer)`` walks two schemas in parallel and emits
one ``ChangeOp`` per atomic change, with JSON-pointer paths under ``base_pointer``:

* ``required_added`` / ``required_removed`` - a property name entered / left ``required``
  (path points at the property, e.g. ``.../properties/units``; ``after``/``before`` is the name)
* ``type_changed`` - ``type`` (also given as a list), ``format``, ``$ref`` or ``nullable``
  changed; ``before``/``after`` are compact descriptors such as ``{"type": "string"}``.
  Validation keywords (``minimum``, ``pattern``, ``maxLength`` ...) also count as a type
  change at ``<property>/<keyword>`` because the accepted value space changed.
* ``enum_changed`` - ``enum`` (or ``const``) changed; ``before``/``after`` are the full lists
* ``removed`` / ``added`` - a property, ``$defs`` entry or ``oneOf``/``anyOf``/``allOf`` branch
* ``text_changed`` - only ``description`` or ``title`` changed (path ends in that key)
* ``deprecated`` - ``deprecated: true`` (or ``x-deprecated``) newly set

``$ref`` nodes are compared as strings unless resolvers are supplied (the OpenAPI watcher
passes one per side, backed by that document's ``components``); resolution is cycle-safe
(a ref pair already on the current path stops the walk) and bounded by ``max_ref_depth``.

``residual_schema_ops(before, after, base_pointer)`` covers what the walk does not model
(``default``, ``readOnly``, ``examples`` ...): nothing when the schemas are equivalent up to
the order of ``required`` / ``enum`` / ``type`` lists (``schemas_equivalent``), else
``text_changed`` ops, never a breaking op.

Pure Python, pydantic only, so the open-source CLI can use it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kintsua.core.events import ChangeOp, canonical_json, json_pointer_escape

Resolver = Callable[[dict[str, Any]], dict[str, Any]]

TYPE_KEYS = ("type", "format", "$ref", "nullable")
TEXT_KEYS = ("description", "title")
CONSTRAINT_KEYS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
)
COMBINATORS = ("oneOf", "anyOf", "allOf")
DEFS_KEYS = ("$defs", "definitions")
MAX_DEPTH = 64


class _Ctx:
    __slots__ = ("ops", "resolve_before", "resolve_after", "max_ref_depth")

    def __init__(
        self,
        resolve_before: Resolver | None,
        resolve_after: Resolver | None,
        max_ref_depth: int,
    ) -> None:
        self.ops: list[ChangeOp] = []
        self.resolve_before = resolve_before
        self.resolve_after = resolve_after
        self.max_ref_depth = max_ref_depth


def diff_json_schema(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    base_pointer: str = "",
    *,
    resolve: Resolver | None = None,
    resolve_before: Resolver | None = None,
    resolve_after: Resolver | None = None,
    max_ref_depth: int = 2,
) -> list[ChangeOp]:
    """Diff two JSON Schemas.

    ``resolve`` (or the per-side ``resolve_before`` / ``resolve_after``) dereferences a
    ``{"$ref": ...}`` node to its target schema; when both sides carry a ref it is followed
    up to ``max_ref_depth`` levels deep and never twice on the same path (cycle-safe).
    """
    ctx = _Ctx(resolve_before or resolve, resolve_after or resolve, max_ref_depth)
    _walk(before or {}, after or {}, base_pointer.rstrip("/"), ctx, (), 0)
    return ctx.ops


def _join(ptr: str, *tokens: str | int) -> str:
    return ptr + "".join("/" + json_pointer_escape(str(t)) for t in tokens)


def _as_list(x: Any) -> list[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return list(x)
    return [x]


def _type_descriptor(s: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    t = s.get("type")
    if isinstance(t, list):
        out["type"] = sorted(str(v) for v in t)
    elif t is not None:
        out["type"] = t
    for k in ("format", "$ref", "nullable"):
        if k in s:
            out[k] = s[k]
    if "items" in s and isinstance(s["items"], dict):
        inner = _type_descriptor(s["items"])
        if inner:
            out["items"] = inner
    return out


def _effective_type(s: dict[str, Any]) -> tuple[Any, ...]:
    """What the differ compares for ``type_changed``: type (sorted when a list) + nullable
    + format + $ref. ``["string", "null"]`` and ``{"type": "string", "nullable": true}`` are
    both normalised to ("null", "string")."""
    t = s.get("type")
    types: list[str] = []
    if isinstance(t, list):
        types = sorted(str(v) for v in t)
    elif t is not None:
        types = [str(t)]
    if s.get("nullable") is True and "null" not in types:
        types = sorted([*types, "null"])
    return (tuple(types), s.get("format"), s.get("$ref"))


def _is_deprecated(s: dict[str, Any]) -> bool:
    return s.get("deprecated") is True or s.get("x-deprecated") is True


def _type_op(ptr: str, b: Any, a: Any) -> ChangeOp:
    before = _type_descriptor(b) if isinstance(b, dict) else b
    after = _type_descriptor(a) if isinstance(a, dict) else a
    return ChangeOp(op="type_changed", path=ptr or "/", before=before, after=after)


def _walk(b: Any, a: Any, ptr: str, ctx: _Ctx, stack: tuple[str, ...], depth: int) -> None:
    if depth > MAX_DEPTH:
        return
    if isinstance(b, bool) or isinstance(a, bool):
        # ``true``/``false`` schemas (e.g. additionalProperties)
        if b != a:
            ctx.ops.append(ChangeOp(op="type_changed", path=ptr or "/", before=b, after=a))
        return
    if not isinstance(b, dict) or not isinstance(a, dict):
        if b != a:
            ctx.ops.append(ChangeOp(op="type_changed", path=ptr or "/", before=b, after=a))
        return

    # --- $ref handling -----------------------------------------------------------------
    b_ref, a_ref = b.get("$ref"), a.get("$ref")
    can_resolve = (
        (b_ref or a_ref)
        and ctx.resolve_before is not None
        and ctx.resolve_after is not None
        and len(stack) < ctx.max_ref_depth
    )
    if can_resolve:
        key = f"{b_ref}|{a_ref}"
        if key in stack:
            return  # cycle: this pair is already being compared higher up
        stack = (*stack, key)
        rb = ctx.resolve_before(b) if b_ref else b
        ra = ctx.resolve_after(a) if a_ref else a
        if b_ref != a_ref:
            # different targets: report the ref change, then compare the resolved shapes
            ctx.ops.append(_type_op(ptr, b, a))
            _walk_body(rb, ra, ptr, ctx, stack, depth, skip_type=True)
            return
        _walk_body(rb, ra, ptr, ctx, stack, depth, skip_type=False)
        return
    _walk_body(b, a, ptr, ctx, stack, depth, skip_type=False)


def _walk_body(
    b: dict[str, Any],
    a: dict[str, Any],
    ptr: str,
    ctx: _Ctx,
    stack: tuple[str, ...],
    depth: int,
    *,
    skip_type: bool,
) -> None:
    ops = ctx.ops
    # deprecated ---------------------------------------------------------------------
    if _is_deprecated(a) and not _is_deprecated(b):
        ops.append(ChangeOp(op="deprecated", path=ptr or "/", before=False, after=True))

    # type / format / $ref ------------------------------------------------------------
    if not skip_type and _effective_type(b) != _effective_type(a):
        ops.append(_type_op(ptr, b, a))

    # enum / const ---------------------------------------------------------------------
    b_enum = _enum_of(b)
    a_enum = _enum_of(a)
    if b_enum != a_enum and not (b_enum is None and a_enum is None):
        if b_enum is None or a_enum is None or sorted(map(str, b_enum)) != sorted(map(str, a_enum)):
            ops.append(ChangeOp(op="enum_changed", path=ptr or "/", before=b_enum, after=a_enum))

    # constraints ------------------------------------------------------------------------
    for k in CONSTRAINT_KEYS:
        if b.get(k) != a.get(k):
            ops.append(
                ChangeOp(op="type_changed", path=_join(ptr, k), before=b.get(k), after=a.get(k))
            )

    # description / title ----------------------------------------------------------------
    for k in TEXT_KEYS:
        if b.get(k) != a.get(k):
            ops.append(
                ChangeOp(op="text_changed", path=_join(ptr, k), before=b.get(k), after=a.get(k))
            )

    # properties -------------------------------------------------------------------------
    b_props = b.get("properties") if isinstance(b.get("properties"), dict) else {}
    a_props = a.get("properties") if isinstance(a.get("properties"), dict) else {}
    for name in sorted(set(b_props) | set(a_props)):
        p = _join(ptr, "properties", name)
        if name not in a_props:
            ops.append(ChangeOp(op="removed", path=p, before=b_props[name], after=None))
        elif name not in b_props:
            ops.append(ChangeOp(op="added", path=p, before=None, after=a_props[name]))
        else:
            _walk(b_props[name], a_props[name], p, ctx, stack, depth + 1)

    # required ---------------------------------------------------------------------------
    b_req = {str(x) for x in _as_list(b.get("required")) if isinstance(x, str)}
    a_req = {str(x) for x in _as_list(a.get("required")) if isinstance(x, str)}
    for name in sorted(a_req - b_req):
        ops.append(
            ChangeOp(
                op="required_added", path=_join(ptr, "properties", name), before=None, after=name
            )
        )
    for name in sorted(b_req - a_req):
        if name in b_props and name not in a_props:
            continue  # the property itself was removed; ``removed`` already says so
        ops.append(
            ChangeOp(
                op="required_removed", path=_join(ptr, "properties", name), before=name, after=None
            )
        )

    # items ------------------------------------------------------------------------------
    b_items, a_items = b.get("items"), a.get("items")
    if isinstance(b_items, list) or isinstance(a_items, list):
        _walk_list(_as_list(b_items), _as_list(a_items), _join(ptr, "items"), ctx, stack, depth)
    elif b_items is not None or a_items is not None:
        if b_items is None:
            ops.append(ChangeOp(op="added", path=_join(ptr, "items"), before=None, after=a_items))
        elif a_items is None:
            ops.append(ChangeOp(op="removed", path=_join(ptr, "items"), before=b_items, after=None))
        else:
            _walk(b_items, a_items, _join(ptr, "items"), ctx, stack, depth + 1)

    # additionalProperties ----------------------------------------------------------------
    b_ap, a_ap = b.get("additionalProperties"), a.get("additionalProperties")
    if b_ap != a_ap:
        if isinstance(b_ap, dict) and isinstance(a_ap, dict):
            _walk(b_ap, a_ap, _join(ptr, "additionalProperties"), ctx, stack, depth + 1)
        else:
            ops.append(
                ChangeOp(
                    op="type_changed",
                    path=_join(ptr, "additionalProperties"),
                    before=b_ap,
                    after=a_ap,
                )
            )

    # combinators by index ----------------------------------------------------------------
    for comb in COMBINATORS:
        if comb in b or comb in a:
            _walk_list(
                _as_list(b.get(comb)), _as_list(a.get(comb)), _join(ptr, comb), ctx, stack, depth
            )

    # $defs / definitions ------------------------------------------------------------------
    for dk in DEFS_KEYS:
        b_defs = b.get(dk) if isinstance(b.get(dk), dict) else {}
        a_defs = a.get(dk) if isinstance(a.get(dk), dict) else {}
        for name in sorted(set(b_defs) | set(a_defs)):
            p = _join(ptr, dk, name)
            if name not in a_defs:
                ops.append(ChangeOp(op="removed", path=p, before=b_defs[name], after=None))
            elif name not in b_defs:
                ops.append(ChangeOp(op="added", path=p, before=None, after=a_defs[name]))
            else:
                _walk(b_defs[name], a_defs[name], p, ctx, stack, depth + 1)


def _enum_of(s: dict[str, Any]) -> list[Any] | None:
    if "enum" in s:
        return _as_list(s.get("enum"))
    if "const" in s:
        return _as_list(s["const"])
    return None


def _walk_list(
    b: list[Any], a: list[Any], ptr: str, ctx: _Ctx, stack: tuple[str, ...], depth: int
) -> None:
    n = max(len(b), len(a))
    for i in range(n):
        p = _join(ptr, i)
        if i >= len(b):
            ctx.ops.append(ChangeOp(op="added", path=p, before=None, after=a[i]))
        elif i >= len(a):
            ctx.ops.append(ChangeOp(op="removed", path=p, before=b[i], after=None))
        else:
            _walk(b[i], a[i], p, ctx, stack, depth + 1)


ORDER_FREE_KEYS = ("required", "enum", "type")
RESIDUAL_MAX_OPS = 20


def _canonical_schema(node: Any) -> Any:
    """The schema with ``required`` / ``enum`` / ``type`` lists sorted (their order carries
    no meaning), recursively."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in ORDER_FREE_KEYS and isinstance(v, list):
                out[k] = sorted((_canonical_schema(x) for x in v), key=canonical_json)
            else:
                out[k] = _canonical_schema(v)
        return out
    if isinstance(node, list):
        return [_canonical_schema(v) for v in node]
    return node


def schemas_equivalent(before: Any, after: Any) -> bool:
    """Equal up to the order of ``required`` / ``enum`` / ``type`` lists."""
    return _canonical_schema(before or {}) == _canonical_schema(after or {})


def residual_schema_ops(before: Any, after: Any, base_pointer: str) -> list[ChangeOp]:
    """Ops for two schemas that differ although ``diff_json_schema`` found nothing (a changed
    ``default``, ``readOnly``, ``examples``, a reordered ``required`` list ...).

    Equivalent schemas yield nothing. Otherwise every difference becomes a ``text_changed``
    op at its own pointer (one op at ``base_pointer`` when there are many): never a breaking
    op, so the classifier routes it to the model instead of calling it a type change."""
    from kintsua.watchers.base import generic_json_diff

    b, a = _canonical_schema(before or {}), _canonical_schema(after or {})
    if b == a:
        return []
    ptr = base_pointer.rstrip("/")
    ops = generic_json_diff(b, a)
    if not ops or len(ops) > RESIDUAL_MAX_OPS:
        return [
            ChangeOp(
                op="text_changed",
                path=ptr or "/",
                before=schema_summary(before),
                after=schema_summary(after),
            )
        ]
    return [
        ChangeOp(
            op="text_changed",
            path=(ptr + op.path) if op.path != "/" else (ptr or "/"),
            before=op.before,
            after=op.after,
        )
        for op in ops
    ]


def schema_summary(schema: Any) -> dict[str, Any]:
    """A compact, human-readable descriptor of a schema for ``before``/``after`` payloads."""
    if not isinstance(schema, dict):
        return {"schema": schema}
    out = _type_descriptor(schema)
    if "enum" in schema:
        out["enum"] = schema["enum"]
    if isinstance(schema.get("properties"), dict):
        out["properties"] = sorted(schema["properties"].keys())
    if schema.get("required"):
        out["required"] = list(schema["required"])
    if schema.get("description"):
        out["description"] = str(schema["description"])[:200]
    return out


def levenshtein(a: str, b: str) -> int:
    """Edit distance, pure Python (two-row DP)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
