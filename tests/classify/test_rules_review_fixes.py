"""Regression tests for the classifier review fixes (rules side).

Where it matters the ops come from the real watchers (``OpenAPIWatcher.diff``,
``diff_mcp_bodies``, ``PyPIWatcher.diff`` / ``NpmWatcher.diff``), so the rules are tested
against the shapes they actually receive.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

import pytest

from kintsua.classify.rules import (
    RULE_ADDED_OPTIONAL,
    RULE_AUTH_CHANGED,
    RULE_REQUIRED_ADDED,
    RULE_REQUIRED_ADDED_RESPONSE,
    RULE_REQUIRED_ADDED_SHARED,
    classify_by_rules,
)
from kintsua.core.events import CanonicalDoc, ChangeEvent, ChangeOp
from kintsua.watchers.mcp import diff_mcp_bodies
from kintsua.watchers.openapi import OpenAPIWatcher, canonicalise
from kintsua.watchers.packages import NpmWatcher, PyPIWatcher

SID = UUID("00000000-0000-4000-8000-000000000003")
CHARGES = "/paths/~1v1~1charges/post"
BODY = f"{CHARGES}/requestBody/content/application~1json/schema"


def ev(kind: str, ops: list[ChangeOp] | list[dict], **kw: Any) -> ChangeEvent:
    return ChangeEvent(
        source_id=SID,
        source_kind=kind,
        ops=[o if isinstance(o, ChangeOp) else ChangeOp(**o) for o in ops],
        **kw,
    )


def openapi_ops(before: dict, after: dict) -> list[ChangeOp]:
    return OpenAPIWatcher().diff(
        CanonicalDoc(body=canonicalise(before)), CanonicalDoc(body=canonicalise(after))
    )


# ---- classify-correctness-2: required_added on a shared component schema -----------------


def _charges_spec(*, request_ref: str | None = None) -> dict:
    """``charge`` is used only in responses; ``params`` (when ``request_ref``) is the
    POST request body."""
    post: dict[str, Any] = {
        "responses": {
            "200": {
                "description": "ok",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/charge"}}
                },
            }
        }
    }
    if request_ref:
        post["requestBody"] = {"content": {"application/json": {"schema": {"$ref": request_ref}}}}
    return {
        "openapi": "3.0.0",
        "info": {"title": "Stripe", "version": "1"},
        "paths": {"/v1/charges": {"post": post}},
        "components": {
            "schemas": {
                "charge": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
                "params": {
                    "type": "object",
                    "properties": {"amount": {"type": "integer"}},
                    "required": ["amount"],
                },
            }
        },
    }


def _require_new(spec: dict, schema: str, field: str) -> dict:
    out = copy.deepcopy(spec)
    s = out["components"]["schemas"][schema]
    s["properties"][field] = {"type": "string"}
    s["required"].append(field)
    return out


def test_component_required_field_used_only_in_responses_is_safe() -> None:
    before = _charges_spec()
    r = classify_by_rules(
        ev("openapi", openapi_ops(before, _require_new(before, "charge", "new_field"))),
        display_name="Stripe API",
    )
    assert r is not None
    assert (r.severity, r.category) == ("safe", "behaviour"), r.summary
    assert r.rule_hits[0] == RULE_REQUIRED_ADDED_RESPONSE
    assert RULE_REQUIRED_ADDED_SHARED in r.rule_hits and RULE_REQUIRED_ADDED not in r.rule_hits
    assert "is now always present" in r.summary
    assert r.migration_hint is None  # no 'Pass new_field in calls to schema charge'


def test_component_required_field_inlined_in_a_request_body_stays_breaking() -> None:
    before = _charges_spec(request_ref="#/components/schemas/params")
    r = classify_by_rules(
        ev("openapi", openapi_ops(before, _require_new(before, "params", "currency"))),
        display_name="Stripe API",
    )
    assert r is not None
    assert (r.severity, r.category) == ("breaking", "required_param")
    assert r.summary.startswith("[Stripe API] POST /v1/charges: required parameter 'currency'")
    assert r.migration_hint == "Pass 'currency' in calls to POST /v1/charges"


def test_component_required_field_with_no_inlined_use_stays_breaking() -> None:
    """A component used too deep to be inlined has only the component-level op: the
    rules cannot tell the side, so they stay conservative."""
    r = classify_by_rules(
        ev(
            "openapi",
            [
                {
                    "op": "required_added",
                    "path": "/components/schemas/address/properties/postal_code",
                    "before": None,
                    "after": "postal_code",
                }
            ],
        )
    )
    assert r is not None
    assert (r.severity, r.category) == ("breaking", "required_param")
    assert r.affected_paths == ["schema:address"]


def test_required_added_in_a_shared_response_component_is_response_side() -> None:
    r = classify_by_rules(
        ev(
            "openapi",
            [
                {
                    "op": "required_added",
                    "path": "/components/responses/ChargeList/content/application~1json"
                    "/schema/properties/has_more",
                    "before": None,
                    "after": "has_more",
                }
            ],
        )
    )
    assert r is not None and (r.severity, r.category) == ("safe", "behaviour")


# ---- classify-correctness-3: renames need a distinctive definition -----------------------


def test_undocumented_no_argument_tools_are_not_a_rename() -> None:
    noarg = {"type": "object", "properties": {}}
    x = {"name": "x", "description": "X", "inputSchema": {"type": "object"}}
    ops = diff_mcp_bodies(
        {"tools": {"ping": {"name": "ping", "inputSchema": noarg}, "x": x}},
        {"tools": {"health_check": {"name": "health_check", "inputSchema": noarg}, "x": x}},
    )
    r = classify_by_rules(ev("mcp", ops))
    assert r is not None
    assert (r.severity, r.category) == ("breaking", "removal"), r.summary
    assert "renamed" not in r.rule_hits
    assert r.summary.startswith("Tool ping was removed")


def test_undocumented_single_argument_tools_are_not_a_rename() -> None:
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    ops = diff_mcp_bodies(
        {"tools": {"delete_file": {"name": "delete_file", "inputSchema": schema}}},
        {"tools": {"read_file": {"name": "read_file", "inputSchema": schema}}},
    )
    r = classify_by_rules(ev("mcp", ops))
    assert r is not None and (r.severity, r.category) == ("breaking", "removal")


def test_documented_tool_with_identical_definition_is_still_a_rename() -> None:
    tool = {
        "description": "Current conditions for a location.",
        "inputSchema": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    }
    ops = diff_mcp_bodies(
        {"tools": {"get_weather": {"name": "get_weather", **tool}}},
        {"tools": {"fetch_current_conditions": {"name": "fetch_current_conditions", **tool}}},
    )
    r = classify_by_rules(ev("mcp", ops))
    assert r is not None and (r.severity, r.category) == ("breaking", "rename")
    assert r.migration_hint == "Rename get_weather to fetch_current_conditions"


def test_empty_values_do_not_make_a_definition_distinctive() -> None:
    ops = [
        {"op": "removed", "path": f"{BODY}/properties/foo", "before": {"description": ""}},
        {"op": "added", "path": f"{BODY}/properties/zap", "after": {"description": ""}},
    ]
    r = classify_by_rules(ev("openapi", ops))
    assert r is not None and r.category == "removal" and "renamed" not in r.rule_hits


# ---- classify-correctness-5: package major bumps from the real watchers ------------------


def _pkg_doc(latest: str, versions: list[str]) -> CanonicalDoc:
    return CanonicalDoc(
        version=latest, body={"name": "pkg", "latest": latest, "versions": versions}
    )


@pytest.mark.parametrize("with_event_versions", [True, False])
def test_pypi_major_bump_from_the_watcher_names_full_versions(with_event_versions: bool) -> None:
    ops = PyPIWatcher().diff(
        _pkg_doc("11.6.0", ["11.5.0", "11.6.0"]), _pkg_doc("12.0.0", ["11.6.0", "12.0.0"])
    )
    assert any(o.op == "deprecated" and o.after == "12" for o in ops)  # bare majors
    kw = {"from_version": "11.6.0", "to_version": "12.0.0"} if with_event_versions else {}
    r = classify_by_rules(ev("pypi", ops, **kw), display_name="stripe")
    assert r is not None and (r.severity, r.category) == ("degraded", "deprecation")
    assert r.summary == "stripe 11.x -> 12.0.0: major version bump"
    assert r.rule_hits == ["major_bump"]
    assert r.migration_hint is not None and "from 11.6.0 to 12.0.0" in r.migration_hint
    assert "12.x series" in r.migration_hint


def test_npm_rollback_of_a_major_is_not_a_major_bump() -> None:
    ops = NpmWatcher().diff(_pkg_doc("2.0.0", ["1.9.9", "2.0.0"]), _pkg_doc("1.9.9", ["1.9.9"]))
    assert any(o.op == "deprecated" and (o.before, o.after) == ("2", "1") for o in ops)
    r = classify_by_rules(
        ev("npm", ops, from_version="2.0.0", to_version="1.9.9"), display_name="foo"
    )
    assert r is not None
    assert r.severity == "safe" and "major_bump" not in r.rule_hits
    assert "major version bump" not in r.summary and "moved back" in r.summary


def test_package_deprecated_op_alone_still_falls_back_to_its_majors() -> None:
    r = classify_by_rules(
        ev("npm", [{"op": "deprecated", "path": "/latest", "before": "1", "after": "2"}]),
        display_name="foo",
    )
    assert r is not None and r.rule_hits == ["major_bump"]


# ---- classify-correctness-7: added security options are not breaking ---------------------


def _secured_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "GitHub", "version": "1"},
        "security": [{"basic": []}],
        "paths": {"/repos": {"get": {"responses": {"200": {"description": "ok"}}}}},
        "components": {"securitySchemes": {"basic": {"type": "http", "scheme": "basic"}}},
    }


def test_added_security_scheme_is_safe_and_does_not_fan_out() -> None:
    before = _secured_spec()
    after = copy.deepcopy(before)
    after["components"]["securitySchemes"]["bearer"] = {"type": "http", "scheme": "bearer"}
    r = classify_by_rules(ev("openapi", openapi_ops(before, after)), display_name="GitHub")
    assert r is not None and r.severity == "safe" and r.rule_hits == [RULE_ADDED_OPTIONAL]
    assert r.affected_paths == []
    assert r.summary == "[GitHub] Security scheme 'bearer' was added"


def test_added_alternative_global_requirement_is_safe() -> None:
    before = _secured_spec()
    before["components"]["securitySchemes"]["bearer"] = {"type": "http", "scheme": "bearer"}
    after = copy.deepcopy(before)
    after["security"].append({"bearer": []})
    r = classify_by_rules(ev("openapi", openapi_ops(before, after)))
    assert r is not None and r.severity == "safe" and r.affected_paths == []
    # the reverse (an alternative removed) is breaking for everyone
    back = classify_by_rules(ev("openapi", openapi_ops(after, before)))
    assert back is not None and back.severity == "breaking" and back.category == "auth"
    assert back.affected_paths == ["*"]


def test_security_newly_required_stays_breaking() -> None:
    before = _secured_spec()
    before.pop("security")
    r = classify_by_rules(ev("openapi", openapi_ops(before, _secured_spec())))
    assert r is not None and (r.severity, r.category) == ("breaking", "auth")


@pytest.mark.parametrize(
    ("path", "before", "after", "rule"),
    [
        ("/security/1", None, {"bearer": []}, RULE_ADDED_OPTIONAL),  # generic diff append
        ("/security/0", None, {"bearer": []}, RULE_AUTH_CHANGED),  # the list was empty
        ("/security/0/bearer", None, [], RULE_AUTH_CHANGED),  # AND-ed onto a requirement
        (
            f"{CHARGES}/security",
            [{"oauth": ["charges:write"]}],
            [{"oauth": ["charges:write"]}, {"key": []}],
            RULE_ADDED_OPTIONAL,
        ),
        (
            f"{CHARGES}/security",
            [{"oauth": ["charges:write"]}],
            [{"oauth": ["charges:write", "charges:read"]}],
            RULE_AUTH_CHANGED,
        ),
        ("/components/securitySchemes/basic", {"type": "http"}, None, RULE_AUTH_CHANGED),
        (
            "/components/securitySchemes/basic",
            {"type": "http", "scheme": "basic"},
            {"type": "http", "scheme": "bearer"},
            RULE_AUTH_CHANGED,
        ),
    ],
)
def test_auth_change_shapes(path: str, before: Any, after: Any, rule: str) -> None:
    op = {"op": "auth_changed", "path": path, "before": before, "after": after}
    r = classify_by_rules(ev("openapi", [op]))
    assert r is not None and r.rule_hits[0] == rule, r.summary
    if path.startswith(CHARGES) and rule == RULE_ADDED_OPTIONAL:
        assert r.affected_paths == ["POST /v1/charges"]


# ---- classify-security-3: vendor fragments are flattened and bounded ---------------------

EVIL = "x\n# Security notice\n[Rotate your keys here](https://evil.example/phish)\r\n\x00"


def test_enum_value_with_newlines_gives_a_one_line_bounded_summary() -> None:
    op = {
        "op": "enum_changed",
        "path": f"{BODY}/properties/mode/enum",
        "before": ["a"],
        "after": ["a", EVIL + "y" * 10_000],
    }
    r = classify_by_rules(ev("openapi", [op]), display_name="Stripe API")
    assert r is not None and r.severity == "degraded"
    for text in (r.summary, r.migration_hint or ""):
        assert "\n" not in text and "\r" not in text and "\x00" not in text
        assert "\n# Security notice" not in text
    assert len(r.summary) <= 500
    assert r.migration_hint is not None and len(r.migration_hint) <= 1000


def test_huge_default_gives_a_bounded_hint() -> None:
    op = {
        "op": "added",
        "path": f"{CHARGES}/parameters/2",
        "after": {
            "name": "mode",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "default": "A" * 100_000},
        },
    }
    r = classify_by_rules(ev("openapi", [op]))
    assert r is not None and r.category == "required_param"
    assert r.migration_hint is not None and len(r.migration_hint) < 400
    assert r.migration_hint.startswith("Pass 'mode' in calls to POST /v1/charges (default \"AAA")


def test_sunset_and_names_are_flattened() -> None:
    ops = [
        {
            "op": "deprecated",
            "path": CHARGES,
            "after": {"deprecated": True, "x-sunset": "2027-01-01\n# Act now\n" + "z" * 5000},
        },
        {"op": "removed", "path": "/tools/" + "evil\n# heading\n" + "t" * 5000, "before": {}},
    ]
    r = classify_by_rules(ev("openapi", ops[:1]))
    assert r is not None and "\n" not in r.summary and len(r.summary) < 200
    assert r.summary.startswith("POST /v1/charges is deprecated (sunset 2027-01-01 # Act now")
    t = classify_by_rules(ev("mcp", ops[1:]))
    assert t is not None and "\n" not in t.summary and len(t.summary) <= 500
    assert "\n" not in (t.migration_hint or "")
    # affected_paths keep the exact name: the matcher joins on it
    assert t.affected_paths == ["tool:evil\n# heading\n" + "t" * 5000]
