"""MCP watcher: snapshot a server's ``initialize`` / ``tools/list`` / ``resources/list`` /
``prompts/list`` and diff tool by tool.

Part of the open-source core (``kintsua watch``): pydantic + the ``mcp`` client only.

Canonical body::

    {"server":    {"name", "version", "protocolVersion", "capabilities", "instructions"?},
     "tools":     {name: {"description", "inputSchema", "outputSchema"?, "annotations"?, "title"?}},
     "resources": {uri:  {"name", "description"?, "mimeType"?, "title"?}},
     "resourceTemplates": {uriTemplate: {...}},
     "prompts":   {name: {"description"?, "arguments": [...], "title"?}}}

Volatile keys (``_meta``, ``icons``, timestamps ...) are dropped only from the server object
and the top level of each entry, never from schemas or the name-keyed maps. A listing that
fails with anything but "method not found" fails the fetch.

Diff:

* removed tool -> ``removed`` at ``/tools/<name>``; new tool -> ``added``
* changed ``inputSchema`` -> ``diff_json_schema`` ops under ``/tools/<name>/inputSchema``
  (``required_added``, ``type_changed``, ``enum_changed``, ``removed`` ...); an edit it does
  not model (a ``default``, ``readOnly``) is ``text_changed``, a reordered ``required`` is
  nothing
* changed description with an unchanged schema -> ``text_changed`` at
  ``/tools/<name>/description`` (always recorded: the September 2026 research showed drift
  behind unchanged descriptions)
* resources / prompts: ``added`` / ``removed`` / ``text_changed`` (prompt arguments also get
  ``required_added``)
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from kintsua.core.events import CanonicalDoc, ChangeOp, canonical_json, json_pointer
from kintsua.watchers._mcp_compat import (
    MCPError,
    error_code,
    init_result_of,
    list_prompts_all,
    list_resource_templates_all,
    list_resources_all,
    list_tools_all,
    open_session,
)
from kintsua.watchers.base import (
    VOLATILE_KEYS,
    BaseWatcher,
    SourceRef,
    register,
    sort_keys,
    strip_volatile,
)
from kintsua.watchers.schemadiff import diff_json_schema, residual_schema_ops, schema_summary

log = logging.getLogger(__name__)

# MCP-specific volatile keys on top of the shared list. They are dropped only at the top
# level of the server object, of each tool / resource / prompt entry and of an entry's
# ``annotations`` (e.g. a resource's ``lastModified``), never inside ``inputSchema`` /
# ``outputSchema`` or from the tool/resource/prompt maps themselves: there the keys are
# names the server chose (a ``date`` or ``session_id`` property, a tool called ``timestamp``).
MCP_VOLATILE = frozenset({"_meta", "icons", "ttlMs", "cacheScope", "resultType"})
ENTRY_VOLATILE = VOLATILE_KEYS | MCP_VOLATILE
METHOD_NOT_FOUND = -32601

TOOL_KEYS = ("title", "description", "inputSchema", "outputSchema", "annotations")
RESOURCE_KEYS = ("name", "title", "description", "mimeType", "size", "annotations")
PROMPT_KEYS = ("title", "description", "arguments")


def tool_fingerprint(input_schema: dict[str, Any] | None) -> str:
    """SHA-256 of the canonicalised (key-sorted, compact) ``inputSchema``. Used by the SDK
    to detect schema drift locally and by the indexer to key call sites."""
    return hashlib.sha256(canonical_json(input_schema or {}).encode("utf-8")).hexdigest()


def _pick(d: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in keys:
        if k in d and d[k] is not None:
            out[k] = d[k]
    ann = out.get("annotations")
    if isinstance(ann, dict):
        out["annotations"] = {k: v for k, v in ann.items() if k not in ENTRY_VOLATILE}
    return out


def build_body(
    init: dict[str, Any] | None,
    tools: list[dict[str, Any]],
    resources: list[dict[str, Any]] | None = None,
    prompts: list[dict[str, Any]] | None = None,
    resource_templates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalise wire-format listings into the canonical body (volatile keys stripped,
    keys sorted, lists keyed by name/uri)."""
    init = init or {}
    server_info = init.get("serverInfo") or {}
    server = {
        "name": server_info.get("name"),
        "version": server_info.get("version"),
        "protocolVersion": init.get("protocolVersion"),
        # protocol-defined keys only: safe to strip volatile ones recursively
        "capabilities": strip_volatile(init.get("capabilities") or {}, MCP_VOLATILE),
    }
    if init.get("instructions"):
        server["instructions"] = init["instructions"]
    tools_map: dict[str, Any] = {}
    for t in tools:
        name = t.get("name")
        if not name:
            continue
        entry = _pick(t, TOOL_KEYS)
        entry.setdefault("inputSchema", {})
        tools_map[str(name)] = entry
    res_map: dict[str, Any] = {}
    for r in resources or []:
        uri = r.get("uri")
        if not uri:
            continue
        res_map[str(uri)] = _pick(r, RESOURCE_KEYS)
    tmpl_map: dict[str, Any] = {}
    for r in resource_templates or []:
        uri = r.get("uriTemplate")
        if not uri:
            continue
        tmpl_map[str(uri)] = _pick(r, RESOURCE_KEYS)
    prompt_map: dict[str, Any] = {}
    for p in prompts or []:
        name = p.get("name")
        if not name:
            continue
        entry = _pick(p, PROMPT_KEYS)
        entry["arguments"] = [
            _pick(arg, ("name", "title", "description", "required"))
            for arg in (entry.get("arguments") or [])
            if isinstance(arg, dict)
        ]
        prompt_map[str(name)] = entry
    body = {
        "server": server,
        "tools": tools_map,
        "resources": res_map,
        "resourceTemplates": tmpl_map,
        "prompts": prompt_map,
    }
    return sort_keys(body)


async def fetch_server_snapshot(locator: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Connect to the server and return the canonical body. Servers that do not advertise
    the ``resources`` / ``prompts`` capability (or answer "method not found") yield empty maps."""
    meta = dict(meta or {})
    async with open_session(locator, meta) as session:
        init = init_result_of(session) or {}
        caps = init.get("capabilities") or {}
        tools = await list_tools_all(session) if _has_cap(caps, "tools", init) else []
        resources: list[dict[str, Any]] = []
        templates: list[dict[str, Any]] = []
        prompts: list[dict[str, Any]] = []
        if _has_cap(caps, "resources", init):
            resources = await _tolerant(list_resources_all, session, "resources/list")
            templates = await _tolerant(
                list_resource_templates_all, session, "resources/templates/list"
            )
        if _has_cap(caps, "prompts", init):
            prompts = await _tolerant(list_prompts_all, session, "prompts/list")
    return build_body(init, tools, resources, prompts, templates)


def _has_cap(caps: dict[str, Any], name: str, init: dict[str, Any]) -> bool:
    # Servers that omit ``capabilities`` entirely are still asked (some proxies do that);
    # servers that list capabilities but not this one are not.
    if not init or "capabilities" not in init:
        return True
    return name in caps


async def _tolerant(fn: Any, session: Any, what: str) -> list[dict[str, Any]]:
    """An optional listing the server does not implement ("method not found") is empty.
    Anything else (a timeout, a transport or server error) fails the fetch: an empty map
    would read as every resource / prompt removed, then re-added on the next good poll."""
    try:
        return await fn(session)
    except MCPError as e:
        if error_code(e) != METHOD_NOT_FOUND:
            raise
        log.debug("%s not supported by server: %s", what, e)
        return []


async def fetch_tools(locator: str, meta: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Tool definitions (wire format, with ``inputSchema``) for the indexer and the SDK."""
    async with open_session(locator, dict(meta or {})) as session:
        tools = await list_tools_all(session)
    # wire format minus per-response metadata; inputSchema is untouched so
    # tool_fingerprint(t["inputSchema"]) equals the SDK's fingerprint of the same tool
    return [
        sort_keys({k: v for k, v in t.items() if k not in MCP_VOLATILE})
        for t in tools
        if t.get("name")
    ]


# --------------------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------------------


def diff_mcp_bodies(before: dict[str, Any], after: dict[str, Any]) -> list[ChangeOp]:
    ops: list[ChangeOp] = []
    _diff_tools(before.get("tools") or {}, after.get("tools") or {}, ops)
    _diff_keyed(
        before.get("resources") or {}, after.get("resources") or {}, "resources", RESOURCE_KEYS, ops
    )
    _diff_keyed(
        before.get("resourceTemplates") or {},
        after.get("resourceTemplates") or {},
        "resourceTemplates",
        RESOURCE_KEYS,
        ops,
    )
    _diff_prompts(before.get("prompts") or {}, after.get("prompts") or {}, ops)
    b_srv, a_srv = before.get("server") or {}, after.get("server") or {}
    for key in ("protocolVersion", "instructions"):
        if b_srv.get(key) != a_srv.get(key):
            ops.append(
                ChangeOp(
                    op="text_changed",
                    path=json_pointer("server", key),
                    before=b_srv.get(key),
                    after=a_srv.get(key),
                )
            )
    if (b_srv.get("capabilities") or {}) != (a_srv.get("capabilities") or {}):
        ops.append(
            ChangeOp(
                op="text_changed",
                path=json_pointer("server", "capabilities"),
                before=b_srv.get("capabilities"),
                after=a_srv.get("capabilities"),
            )
        )
    return ops


def _diff_tools(before: dict[str, Any], after: dict[str, Any], ops: list[ChangeOp]) -> None:
    for name in sorted(set(before) | set(after)):
        base = json_pointer("tools", name)
        if name not in after:
            ops.append(
                ChangeOp(op="removed", path=base, before=_tool_summary(before[name]), after=None)
            )
            continue
        if name not in before:
            ops.append(
                ChangeOp(op="added", path=base, before=None, after=_tool_summary(after[name]))
            )
            continue
        b, a = before[name], after[name]
        b_schema, a_schema = b.get("inputSchema") or {}, a.get("inputSchema") or {}
        if b_schema != a_schema:
            # edits the semantic differ does not model (a default, readOnly, the order of
            # `required`) are text_changed or nothing, never a synthetic type change
            schema_ops = diff_json_schema(b_schema, a_schema, base + "/inputSchema")
            ops.extend(schema_ops or residual_schema_ops(b_schema, a_schema, base + "/inputSchema"))
        b_out, a_out = b.get("outputSchema") or {}, a.get("outputSchema") or {}
        if b_out != a_out:
            out_ops = diff_json_schema(b_out, a_out, base + "/outputSchema")
            ops.extend(out_ops or residual_schema_ops(b_out, a_out, base + "/outputSchema"))
        # description drift is ALWAYS recorded, schema change or not
        if b.get("description") != a.get("description"):
            ops.append(
                ChangeOp(
                    op="text_changed",
                    path=base + "/description",
                    before=b.get("description"),
                    after=a.get("description"),
                )
            )
        if b.get("title") != a.get("title"):
            ops.append(
                ChangeOp(
                    op="text_changed",
                    path=base + "/title",
                    before=b.get("title"),
                    after=a.get("title"),
                )
            )
        if (b.get("annotations") or {}) != (a.get("annotations") or {}):
            ops.append(
                ChangeOp(
                    op="text_changed",
                    path=base + "/annotations",
                    before=b.get("annotations"),
                    after=a.get("annotations"),
                )
            )


def _tool_summary(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": tool.get("description"),
        "inputSchema": schema_summary(tool.get("inputSchema") or {}),
        "fingerprint": tool_fingerprint(tool.get("inputSchema") or {}),
    }


def _diff_keyed(
    before: dict[str, Any],
    after: dict[str, Any],
    section: str,
    keys: tuple[str, ...],
    ops: list[ChangeOp],
) -> None:
    for key in sorted(set(before) | set(after)):
        base = json_pointer(section, key)
        if key not in after:
            ops.append(ChangeOp(op="removed", path=base, before=before[key], after=None))
        elif key not in before:
            ops.append(ChangeOp(op="added", path=base, before=None, after=after[key]))
        else:
            b, a = before[key], after[key]
            for k in keys:
                if b.get(k) != a.get(k):
                    ops.append(
                        ChangeOp(
                            op="text_changed", path=base + "/" + k, before=b.get(k), after=a.get(k)
                        )
                    )


def _diff_prompts(before: dict[str, Any], after: dict[str, Any], ops: list[ChangeOp]) -> None:
    for name in sorted(set(before) | set(after)):
        base = json_pointer("prompts", name)
        if name not in after:
            ops.append(ChangeOp(op="removed", path=base, before=before[name], after=None))
            continue
        if name not in before:
            ops.append(ChangeOp(op="added", path=base, before=None, after=after[name]))
            continue
        b, a = before[name], after[name]
        for k in ("description", "title"):
            if b.get(k) != a.get(k):
                ops.append(
                    ChangeOp(
                        op="text_changed", path=base + "/" + k, before=b.get(k), after=a.get(k)
                    )
                )
        b_args = {arg.get("name"): arg for arg in b.get("arguments") or [] if arg.get("name")}
        a_args = {arg.get("name"): arg for arg in a.get("arguments") or [] if arg.get("name")}
        for arg in sorted(set(b_args) | set(a_args)):
            p = json_pointer("prompts", name, "arguments", arg)
            if arg not in a_args:
                ops.append(ChangeOp(op="removed", path=p, before=b_args[arg], after=None))
            elif arg not in b_args:
                ops.append(ChangeOp(op="added", path=p, before=None, after=a_args[arg]))
                if a_args[arg].get("required"):
                    ops.append(ChangeOp(op="required_added", path=p, before=None, after=arg))
            else:
                if bool(a_args[arg].get("required")) and not bool(b_args[arg].get("required")):
                    ops.append(ChangeOp(op="required_added", path=p, before=None, after=arg))
                elif bool(b_args[arg].get("required")) and not bool(a_args[arg].get("required")):
                    ops.append(ChangeOp(op="required_removed", path=p, before=arg, after=None))
                if b_args[arg].get("description") != a_args[arg].get("description"):
                    ops.append(
                        ChangeOp(
                            op="text_changed",
                            path=p + "/description",
                            before=b_args[arg].get("description"),
                            after=a_args[arg].get("description"),
                        )
                    )


@register
class MCPWatcher(BaseWatcher):
    kind = "mcp"

    async def fetch(self, source: SourceRef) -> CanonicalDoc:
        body = await fetch_server_snapshot(source.locator, source.meta)
        version = (body.get("server") or {}).get("version") or None
        return CanonicalDoc(version=str(version) if version else None, body=body)

    def diff(self, before: CanonicalDoc, after: CanonicalDoc) -> list[ChangeOp]:
        return diff_mcp_bodies(before.body, after.body)

    def event_text(self, before: CanonicalDoc, after: CanonicalDoc) -> str | None:
        """Free text that travels with the event: the new/changed tool descriptions (data,
        never instructions)."""
        parts: list[str] = []
        b_tools, a_tools = before.body.get("tools") or {}, after.body.get("tools") or {}
        for name, tool in sorted(a_tools.items()):
            if name not in b_tools or b_tools[name].get("description") != tool.get("description"):
                desc = tool.get("description")
                if desc:
                    parts.append(f"{name}: {desc}")
        text = "\n".join(parts).strip()
        return text[:20_000] or None
