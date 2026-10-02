"""Regression tests for the watcher review findings that need no database: MCP schema
normalisation, honest schema-diff fallbacks, rename/auth false positives, tolerant listings,
version-only changes, lockfile collisions and fetch bounds."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import respx

from kintsua.classify.rules import classify_by_rules
from kintsua.core.events import CanonicalDoc
from kintsua.core.util import utcnow
from kintsua.watchers import mcp as mcpmod
from kintsua.watchers._http import ResponseTooLarge
from kintsua.watchers._mcp_compat import MCPConnectError, MCPError, list_tools_all
from kintsua.watchers.base import FileSnapshotStore, SnapshotRecord, SourceRef, compare
from kintsua.watchers.mcp import (
    MCPWatcher,
    build_body,
    diff_mcp_bodies,
    fetch_server_snapshot,
    fetch_tools,
    tool_fingerprint,
)
from kintsua.watchers.openapi import (
    OpenAPIWatcher,
    SpecFormatError,
    canonicalise,
    diff_openapi,
    fetch_spec,
    parse_spec_text,
)
from kintsua.watchers.schemadiff import residual_schema_ops, schemas_equivalent

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "watchers"


def kinds(ops):
    return {(o.op, o.path) for o in ops}


def _event(watcher, before: CanonicalDoc, after: CanonicalDoc, kind="mcp"):
    src = SourceRef.stable(kind, "https://x.example/" + kind)
    prev = SnapshotRecord(
        source_id=src.id,
        content_hash=before.content_hash(),
        version=before.version,
        captured_at=utcnow(),
        body=before.body,
    )
    return compare(watcher, src, prev, after)


# --------------------------------------------------------------------------------------
# watchers-correctness-2: schema property names that look volatile are kept
# --------------------------------------------------------------------------------------

VOLATILE_NAMED = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "timestamp": {"type": "string"},
        "session_id": {"type": "string"},
        "title": {"type": "string"},
    },
    "required": ["date", "title"],
}


def test_build_body_keeps_schema_properties_named_like_volatile_keys():
    tools = [
        {"name": "create_event", "inputSchema": VOLATILE_NAMED, "_meta": {"x": 1}},
        {"name": "date", "description": "a tool called date", "inputSchema": {"type": "object"}},
    ]
    body = build_body({"serverInfo": {"name": "s", "version": "1"}}, tools)
    props = body["tools"]["create_event"]["inputSchema"]["properties"]
    assert set(props) == {"date", "timestamp", "session_id", "title"}
    assert "date" in body["tools"]  # a tool named like a volatile key survives
    assert "_meta" not in body["tools"]["create_event"]
    # a resource annotation's lastModified is still volatile
    res = build_body(
        None,
        [],
        resources=[
            {"uri": "r://x", "name": "x", "annotations": {"lastModified": "now", "priority": 1}}
        ],
    )
    assert res["resources"]["r://x"]["annotations"] == {"priority": 1}


def test_changes_to_volatile_named_properties_are_diffed():
    before = build_body(None, [{"name": "create_event", "inputSchema": VOLATILE_NAMED}])
    changed = json.loads(json.dumps(VOLATILE_NAMED))
    changed["properties"]["date"] = {"type": "integer"}
    del changed["properties"]["session_id"]
    after = build_body(None, [{"name": "create_event", "inputSchema": changed}])
    k = kinds(diff_mcp_bodies(before, after))
    assert ("type_changed", "/tools/create_event/inputSchema/properties/date") in k
    assert ("removed", "/tools/create_event/inputSchema/properties/session_id") in k


async def test_fetch_tools_keeps_the_wire_schema_so_fingerprints_match_the_sdk(monkeypatch):
    wire = [{"name": "create_event", "inputSchema": VOLATILE_NAMED, "_meta": {"m": 1}}]

    @asynccontextmanager
    async def fake_open(locator, meta):
        yield object()

    async def fake_list(session):
        return json.loads(json.dumps(wire))

    monkeypatch.setattr(mcpmod, "open_session", fake_open)
    monkeypatch.setattr(mcpmod, "list_tools_all", fake_list)
    tools = await fetch_tools("https://x.example/mcp")
    assert "_meta" not in tools[0]
    assert tool_fingerprint(tools[0]["inputSchema"]) == tool_fingerprint(VOLATILE_NAMED)


# --------------------------------------------------------------------------------------
# watchers-correctness-3: no synthetic type_changed for cosmetic schema edits
# --------------------------------------------------------------------------------------

DAYS = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "days": {"type": "integer", "default": 1},
    },
    "required": ["city", "days"],
}


def _tool_body(schema):
    return build_body(
        {"serverInfo": {"name": "w", "version": "1"}},
        [{"name": "get_weather", "inputSchema": schema}],
    )


def test_reordered_required_list_is_not_a_change():
    after = json.loads(json.dumps(DAYS))
    after["required"] = ["days", "city"]
    assert schemas_equivalent(DAYS, after)
    assert diff_mcp_bodies(_tool_body(DAYS), _tool_body(after)) == []


def test_default_only_change_is_text_changed_and_never_breaking():
    after = json.loads(json.dumps(DAYS))
    after["properties"]["days"]["default"] = 3
    before_doc = CanonicalDoc(version="1", body=_tool_body(DAYS))
    after_doc = CanonicalDoc(version="2", body=_tool_body(after))
    ops = MCPWatcher().diff(before_doc, after_doc)
    assert kinds(ops) == {
        ("text_changed", "/tools/get_weather/inputSchema/properties/days/default")
    }
    assert ops[0].before == 1 and ops[0].after == 3
    ev = _event(MCPWatcher(), before_doc, after_doc)
    cls = classify_by_rules(ev)
    assert cls is None or cls.severity != "breaking"


def test_residual_schema_ops_collapses_many_edits():
    before = {"properties": {f"p{i}": {"type": "string"} for i in range(40)}}
    after = {"properties": {f"p{i}": {"type": "string", "readOnly": True} for i in range(40)}}
    ops = residual_schema_ops(before, after, "/x/schema")
    assert [(o.op, o.path) for o in ops] == [("text_changed", "/x/schema")]
    assert residual_schema_ops(before, before, "/x") == []


def _spec(schema_prop: dict, security=None, schemes=None):
    return canonicalise(
        {
            "openapi": "3.0.0",
            "info": {"title": "x", "version": "1"},
            "paths": {
                "/v1/x": {
                    "get": {
                        "security": security,
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {"id": schema_prop},
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
            "components": {"securitySchemes": schemes or {}},
        }
    )


def test_openapi_read_only_flag_is_not_a_type_change():
    ops = diff_openapi(_spec({"type": "string"}), _spec({"type": "string", "readOnly": True}))
    assert not any(o.op == "type_changed" for o in ops)
    assert all(o.op == "text_changed" for o in ops) and ops


# --------------------------------------------------------------------------------------
# watchers-correctness-4: renames are not invented across methods / codes / bare params
# --------------------------------------------------------------------------------------


def _ops_spec(methods: dict) -> dict:
    return canonicalise(
        {"openapi": "3.0.0", "info": {"title": "x", "version": "1"}, "paths": methods}
    )


def test_http_methods_and_status_codes_never_pair_as_renames():
    before = _ops_spec(
        {
            "/v1/x": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "post": {"responses": {"200": {"description": "ok"}}},
            }
        }
    )
    after = _ops_spec(
        {
            "/v1/x": {
                "put": {"responses": {"200": {"description": "ok"}}},
                "post": {"responses": {"201": {"description": "ok"}}},
            }
        }
    )
    k = kinds(diff_openapi(before, after))
    assert not any(op == "renamed" for op, _ in k)
    assert ("removed", "/paths/~1v1~1x/get") in k and ("added", "/paths/~1v1~1x/put") in k
    assert ("removed", "/paths/~1v1~1x/post/responses/200") in k
    assert ("added", "/paths/~1v1~1x/post/responses/201") in k


def test_unrelated_optional_string_params_do_not_pair_as_rename():
    def spec(param: str) -> dict:
        return _ops_spec(
            {
                "/v1/charges": {
                    "get": {
                        "parameters": [
                            {"name": param, "in": "query", "schema": {"type": "string"}}
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )

    ops = diff_openapi(spec("legacy_filter"), spec("customer_email"))
    assert {o.op for o in ops} == {"removed", "added"}
    # a near-identical name still pairs
    renamed = diff_openapi(spec("customer_id"), spec("customerid"))
    assert [o.op for o in renamed] == ["renamed"]


# --------------------------------------------------------------------------------------
# watchers-correctness-5: security wording / order is not an auth change
# --------------------------------------------------------------------------------------


BEARER = {"type": "http", "scheme": "bearer", "description": "Use your API key"}
OAUTH = {
    "type": "oauth2",
    "flows": {
        "clientCredentials": {"tokenUrl": "https://t", "scopes": {"read": "r", "write": "w"}}
    },
}


def test_security_scheme_description_edit_is_text_changed():
    before = _spec({"type": "string"}, schemes={"bearer": BEARER})
    after = _spec(
        {"type": "string"}, schemes={"bearer": {**BEARER, "description": "Use your secret API key"}}
    )
    ops = diff_openapi(before, after)
    assert kinds(ops) == {("text_changed", "/components/securitySchemes/bearer/description")}


def test_reordered_security_alternatives_and_scopes_are_not_changes():
    s1 = [{"bearer": []}, {"oauth": ["write", "read"]}]
    s2 = [{"oauth": ["read", "write"]}, {"bearer": []}]
    schemes = {"bearer": BEARER, "oauth": OAUTH}
    oauth_reordered = {
        "type": "oauth2",
        "flows": {
            "clientCredentials": {"tokenUrl": "https://t", "scopes": {"write": "w", "read": "r"}}
        },
    }
    before = _spec({"type": "string"}, security=s1, schemes=schemes)
    after = _spec(
        {"type": "string"}, security=s2, schemes={"bearer": BEARER, "oauth": oauth_reordered}
    )
    assert diff_openapi(before, after) == []


def test_real_auth_changes_are_still_auth_changed():
    schemes = {"oauth": OAUTH}
    more_scopes = json.loads(json.dumps(OAUTH))
    more_scopes["flows"]["clientCredentials"]["scopes"]["admin"] = "a"
    before = _spec({"type": "string"}, security=[{"oauth": ["read"]}], schemes=schemes)
    after = _spec(
        {"type": "string"}, security=[{"oauth": ["read", "admin"]}], schemes={"oauth": more_scopes}
    )
    k = kinds(diff_openapi(before, after))
    assert ("auth_changed", "/components/securitySchemes/oauth") in k
    assert ("auth_changed", "/paths/~1v1~1x/get/security") in k


# --------------------------------------------------------------------------------------
# watchers-correctness-8: only "method not found" makes an optional listing empty
# --------------------------------------------------------------------------------------


def _patched_session(monkeypatch, *, resources_error=None, prompts_error=None):
    class Sess:
        _kintsua_init_result = {
            "serverInfo": {"name": "s", "version": "1"},
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
        }

    @asynccontextmanager
    async def fake_open(locator, meta):
        yield Sess()

    async def tools(session):
        return [{"name": "t", "inputSchema": {"type": "object"}}]

    def failing(err, value):
        async def fn(session):
            if err is not None:
                raise err
            return value

        return fn

    monkeypatch.setattr(mcpmod, "open_session", fake_open)
    monkeypatch.setattr(mcpmod, "list_tools_all", tools)
    monkeypatch.setattr(
        mcpmod, "list_resources_all", failing(resources_error, [{"uri": "r://a", "name": "a"}])
    )
    monkeypatch.setattr(mcpmod, "list_resource_templates_all", failing(None, []))
    monkeypatch.setattr(mcpmod, "list_prompts_all", failing(prompts_error, [{"name": "p"}]))


async def test_method_not_found_listing_is_empty(monkeypatch):
    _patched_session(monkeypatch, resources_error=MCPError(-32601, "Method not found"))
    body = await fetch_server_snapshot("https://x.example/mcp")
    assert body["resources"] == {} and "p" in body["prompts"]


@pytest.mark.parametrize(
    "err", [MCPError(-32001, "Request timed out"), TimeoutError("read timeout"), OSError("reset")]
)
async def test_listing_timeouts_and_transport_errors_fail_the_fetch(monkeypatch, err):
    _patched_session(monkeypatch, prompts_error=err)
    with pytest.raises(type(err)):
        await fetch_server_snapshot("https://x.example/mcp")


# --------------------------------------------------------------------------------------
# watchers-correctness-9: a version string bump alone is not a change event
# --------------------------------------------------------------------------------------


def test_version_only_bumps_produce_no_event():
    spec = json.loads((FIX / "openapi3_before.json").read_text("utf-8"))
    bumped = json.loads(json.dumps(spec))
    bumped["info"]["version"] = "9.9.9"
    b, a = canonicalise(spec), canonicalise(bumped)
    assert (
        _event(
            OpenAPIWatcher(),
            CanonicalDoc(version="1.0.0", body=b),
            CanonicalDoc(version="9.9.9", body=a),
            "openapi",
        )
        is None
    )
    body = json.loads((FIX / "mcp_before.json").read_text("utf-8"))
    body2 = json.loads(json.dumps(body))
    body2["server"]["version"] = "7.0.0"
    assert (
        _event(
            MCPWatcher(),
            CanonicalDoc(version="1.4.0", body=body),
            CanonicalDoc(version="7.0.0", body=body2),
        )
        is None
    )


# --------------------------------------------------------------------------------------
# watchers-correctness-11: two servers on one host keep separate lockfiles
# --------------------------------------------------------------------------------------


def test_same_host_servers_do_not_share_a_lockfile(tmp_path):
    store = FileSnapshotStore(tmp_path)
    a = SourceRef.stable("mcp", "https://api.githubcopilot.com/mcp/")
    b = SourceRef.stable("mcp", "https://api.githubcopilot.com/mcp/x/issues/")
    doc_a = CanonicalDoc(version="1", body={"tools": {"a": {}}})
    doc_b = CanonicalDoc(version="1", body={"tools": {"b": {}}})
    store.save(a, doc_a)
    store.save(b, doc_b)
    assert len(list(tmp_path.glob("*.lock.json"))) == 2
    assert store.path_for(a).name == "api.githubcopilot.com.lock.json"
    assert store.path_for(b).name == "api.githubcopilot.com-mcp-x-issues.lock.json"
    # the CLI passes the default (host) name for both: each still finds its own lockfile
    host = "api.githubcopilot.com"
    assert store.latest(a.id, name=host).body == doc_a.body
    assert store.latest(b.id, name=host).body == doc_b.body
    assert store.latest(a.id).body == doc_a.body and store.latest(b.id).body == doc_b.body
    # saving A again keeps writing the host file (it is A's)
    store.save(a, CanonicalDoc(version="2", body={"tools": {"a2": {}}}))
    assert store.latest(a.id, name=host).version == "2"
    assert store.latest(b.id, name=host).version == "1"


# --------------------------------------------------------------------------------------
# watchers-security-5: bounded fetches
# --------------------------------------------------------------------------------------

ALIAS_BOMB = """
openapi: 3.0.0
info: {title: x, version: "1"}
a: &a ["x","x","x","x","x","x","x","x","x","x"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b,*b]
d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c,*c]
e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d,*d]
f: &f [*e,*e,*e,*e,*e,*e,*e,*e,*e,*e]
g: &g [*f,*f,*f,*f,*f,*f,*f,*f,*f,*f]
paths: {}
"""


def test_yaml_alias_bomb_is_refused_quickly():
    t0 = time.monotonic()
    with pytest.raises(SpecFormatError, match="aliases"):
        parse_spec_text(ALIAS_BOMB)
    assert time.monotonic() - t0 < 2
    # modest anchors are fine
    ok = parse_spec_text(
        "openapi: 3.0.0\ninfo: {title: x, version: '1'}\nx: &e {type: string}\ny: [*e, *e]\npaths: {}\n"
    )
    assert ok["y"] == [{"type": "string"}, {"type": "string"}]


@respx.mock
async def test_oversized_spec_response_is_refused():
    respx.get("https://big.example.test/spec.json").mock(
        return_value=httpx.Response(200, content=b"{" + b" " * 5000 + b"}")
    )
    with pytest.raises(ResponseTooLarge):
        await fetch_spec("https://big.example.test/spec.json", {"max_bytes": 1000})
    assert await fetch_spec("https://big.example.test/spec.json", {"max_bytes": 10_000}) == {}


async def test_endless_mcp_pagination_is_bounded(monkeypatch):
    import kintsua.watchers._mcp_compat as compat

    class Endless:
        n = 0

        async def list_tools(self, *, params=None):
            Endless.n += 1

            class R:
                def model_dump(self, **_):
                    return {"tools": [{"name": f"t{Endless.n}"}], "nextCursor": f"c{Endless.n}"}

            return R()

    monkeypatch.setattr(compat, "MAX_LIST_PAGES", 25)
    with pytest.raises(MCPConnectError, match="pages"):
        await list_tools_all(Endless())
    assert Endless.n == 25
