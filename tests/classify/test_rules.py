"""Unit tests for the rule engine helpers and the behaviours the spec spells out."""

from __future__ import annotations

from uuid import UUID

import pytest

from kintsua.classify.levenshtein import levenshtein, names_similar
from kintsua.classify.rules import (
    RULE_NAMES,
    RULES,
    affected_operation,
    classify_by_rules,
    classify_event,
    is_major_bump,
    is_request_side,
    is_response_side,
    parse_path,
)
from kintsua.core.events import ChangeEvent, ChangeOp

SID = UUID("00000000-0000-4000-8000-000000000001")
CHARGES = "/paths/~1v1~1charges/post"
BODY = f"{CHARGES}/requestBody/content/application~1x-www-form-urlencoded/schema"


def ev(kind: str, ops: list[dict], **kw) -> ChangeEvent:
    return ChangeEvent(source_id=SID, source_kind=kind, ops=[ChangeOp(**o) for o in ops], **kw)


# ---- levenshtein -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "d"),
    [
        ("", "", 0),
        ("abc", "", 3),
        ("", "abc", 3),
        ("kitten", "sitting", 3),
        ("flaw", "lawn", 2),
        ("amount", "amount_cents", 6),
        ("getWeather", "get_weather", 2),
        ("assignee", "assignees", 1),
        ("same", "same", 0),
    ],
)
def test_levenshtein(a: str, b: str, d: int) -> None:
    assert levenshtein(a, b) == d
    assert levenshtein(b, a) == d


def test_levenshtein_max_distance_early_exit_is_a_lower_bound() -> None:
    assert levenshtein("abcdefgh", "zzzzzzzz", max_distance=2) > 2
    assert levenshtein("abc", "abd", max_distance=2) == 1


def test_names_similar() -> None:
    assert names_similar("assignee", "assignees")
    assert not names_similar("amount", "amount_cents")
    assert not names_similar("same", "same")
    assert not names_similar("", "x")


# ---- path helpers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "op"),
    [
        ("/paths/~1v1~1charges/post/parameters/3", "POST /v1/charges"),
        (f"{BODY}/properties/amount", "POST /v1/charges"),
        (
            "/paths/~1v1~1charges/get/responses/200/content/application~1json/schema",
            "GET /v1/charges",
        ),
        ("/paths/~1v1~1charges", "/v1/charges"),
        ("/paths/~1repos~1{owner}~1{repo}~1issues/get", "GET /repos/{owner}/{repo}/issues"),
        ("/tools/get_weather", "tool:get_weather"),
        ("/tools/get_weather/inputSchema/properties/x", "tool:get_weather"),
        ("/tools/get_weather/description", "tool:get_weather"),
        ("/resources/weather:~1~1a", "resource:weather://a"),
        ("/prompts/briefing/arguments/0", "prompt:briefing"),
        ("/components/schemas/Charge/properties/amount", "schema:Charge"),
        ("/components/securitySchemes/apiKey", "*"),
        ("/security/0", "*"),
        ("/info/version", ""),
        ("/paragraphs/3", ""),
    ],
)
def test_affected_operation(path: str, op: str) -> None:
    assert affected_operation(path) == op


def test_affected_operation_for_packages_uses_display_name() -> None:
    assert affected_operation("/latest") == "package"
    assert affected_operation("/latest", display_name="stripe") == "package:stripe"
    assert affected_operation("/versions/12.0.0", display_name="stripe") == "package:stripe"


@pytest.mark.parametrize(
    ("path", "req", "resp"),
    [
        ("/paths/~1v1~1charges/post/parameters/3", True, False),
        (f"{BODY}/properties/amount", True, False),
        (
            "/paths/~1v1~1charges/post/responses/200/content/application~1json/schema/properties/id",
            False,
            True,
        ),
        ("/tools/get_weather/inputSchema/properties/x", True, False),
        ("/tools/get_weather/outputSchema/properties/x", False, True),
        ("/tools/get_weather/description", False, False),
        ("/components/schemas/Charge/properties/amount", True, True),
        ("/info/version", False, False),
        ("/latest", False, False),
    ],
)
def test_request_response_side(path: str, req: bool, resp: bool) -> None:
    assert is_request_side(path) is req
    assert is_response_side(path) is resp


def test_parse_path_details() -> None:
    p = parse_path("/paths/~1v1~1charges/post/parameters/3/schema/type")
    assert p.dialect == "openapi" and p.kind == "parameter" and p.index == 3 and p.method == "post"
    p = parse_path("/paths/~1v1~1charges/post/responses/404")
    assert p.kind == "response" and p.status == "404" and p.field is None
    p = parse_path(f"{BODY}/properties/card/properties/number")
    assert p.field == "number"
    p = parse_path("/tools/get_weather/inputSchema/required")
    assert p.dialect == "mcp" and p.name == "get_weather" and p.side == "request"
    p = parse_path("/webhooks/charge.succeeded/post")
    assert p.kind == "operation"


# ---- rule table --------------------------------------------------------------------------


def test_rule_table_is_ordered_by_severity() -> None:
    rank = {"breaking": 0, "degraded": 1, "safe": 2, None: 3}
    seen = [rank[s] for _, s, _ in RULES]
    assert seen == sorted(seen)
    assert len(set(RULE_NAMES)) == len(RULE_NAMES)


# ---- ordering and aggregation ----------------------------------------------------------


def test_breaking_wins_over_degraded_regardless_of_op_order() -> None:
    e = ev(
        "openapi",
        [
            {
                "op": "enum_changed",
                "path": f"{BODY}/properties/parse/enum",
                "before": ["a"],
                "after": ["a", "b"],
            },
            {"op": "deprecated", "path": "/paths/~1v1~1tokens/post", "after": {"deprecated": True}},
            {
                "op": "removed",
                "path": f"{CHARGES}/responses/200/content/application~1json/schema/properties/source",
                "before": {},
            },
        ],
    )
    r = classify_by_rules(e)
    assert r is not None
    assert r.severity == "breaking" and r.category == "removal"
    assert r.rule_hits == ["removed_response_field", "deprecated", "enum_values_added"]
    assert r.affected_paths == ["POST /v1/charges", "POST /v1/tokens"]
    assert r.summary.startswith("Field 'source' was removed from the response of POST /v1/charges")
    assert "(+2 more changes)" in r.summary


def test_affected_paths_deduplicated_in_order() -> None:
    e = ev(
        "mcp",
        [
            {"op": "removed", "path": "/tools/b", "before": {}},
            {
                "op": "removed",
                "path": "/tools/a/inputSchema/properties/x",
                "before": {"type": "string"},
            },
            {
                "op": "removed",
                "path": "/tools/b/outputSchema/properties/y",
                "before": {"type": "string"},
            },
        ],
    )
    r = classify_by_rules(e)
    assert r is not None
    assert r.affected_paths == ["tool:b", "tool:a"]


def test_display_name_in_summary_and_hint_shape() -> None:
    e = ev(
        "openapi",
        [
            {
                "op": "required_added",
                "path": f"{BODY}/required",
                "before": ["amount"],
                "after": ["amount", "currency"],
            }
        ],
    )
    r = classify_by_rules(e, display_name="Stripe API")
    assert r is not None
    assert r.summary == "[Stripe API] POST /v1/charges: required parameter 'currency' was added"
    assert r.migration_hint == "Pass 'currency' in calls to POST /v1/charges"
    plain = classify_by_rules(e)
    assert (
        plain is not None
        and plain.summary == "POST /v1/charges: required parameter 'currency' was added"
    )


def test_required_added_hint_includes_default_when_known() -> None:
    e = ev(
        "openapi",
        [
            {
                "op": "added",
                "path": f"{CHARGES}/parameters/2",
                "after": {
                    "name": "mode",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "default": "live"},
                },
            }
        ],
    )
    r = classify_by_rules(e)
    assert r is not None and r.category == "required_param"
    assert r.migration_hint == "Pass 'mode' in calls to POST /v1/charges (default \"live\")"


def test_type_changed_summary_format() -> None:
    e = ev(
        "openapi",
        [
            {
                "op": "type_changed",
                "path": f"{BODY}/properties/amount",
                "before": "integer",
                "after": "string",
            }
        ],
    )
    r = classify_by_rules(e)
    assert r is not None
    assert r.summary == "Parameter 'amount' on POST /v1/charges changed type from integer to string"


def test_tool_removed_summary_format() -> None:
    r = classify_by_rules(
        ev("mcp", [{"op": "removed", "path": "/tools/get_weather", "before": {}}])
    )
    assert r is not None
    assert r.summary == "Tool get_weather was removed"
    assert r.affected_paths == ["tool:get_weather"]


def test_major_bump_summary_format() -> None:
    e = ev("pypi", [], from_version="11.6.0", to_version="12.0.0")
    r = classify_by_rules(e, display_name="stripe")
    assert r is not None
    assert (r.severity, r.category) == ("degraded", "deprecation")
    assert r.summary == "stripe 11.x -> 12.0.0: major version bump"
    assert r.rule_hits == ["major_bump"]
    assert r.affected_paths == ["package:stripe"]
    assert r.migration_hint and "12.0.0" in r.migration_hint


@pytest.mark.parametrize(
    ("a", "b", "major"),
    [
        ("11.6.0", "12.0.0", True),
        ("1.17.5", "2.0.0", True),
        ("v1.2.3", "v2.0.0", True),
        ("0.4.2", "0.5.0", True),
        ("0.4.2", "0.4.3", False),
        ("1.51.0", "1.52.0", False),
        ("2.0.0", "1.9.0", False),  # a downgrade is not a bump
        ("abc", "2.0.0", False),
        (None, "2.0.0", False),
    ],
)
def test_is_major_bump(a: str | None, b: str, major: bool) -> None:
    assert is_major_bump(a, b) is major


def test_package_versions_fall_back_to_ops_when_event_versions_missing() -> None:
    e = ev("npm", [{"op": "text_changed", "path": "/latest", "before": "1.9.0", "after": "2.0.0"}])
    r = classify_by_rules(e, display_name="@modelcontextprotocol/sdk")
    assert r is not None and r.rule_hits == ["major_bump"]


# ---- rename detection --------------------------------------------------------------------


def test_rename_pair_by_identical_schema() -> None:
    schema = {"type": "string", "maxLength": 22, "description": "Shown on the statement."}
    e = ev(
        "openapi",
        [
            {"op": "removed", "path": f"{BODY}/properties/statement_descriptor", "before": schema},
            {
                "op": "added",
                "path": f"{BODY}/properties/statement_descriptor_suffix",
                "after": schema,
            },
        ],
    )
    r = classify_by_rules(e)
    assert r is not None
    assert (r.severity, r.category) == ("breaking", "rename")
    assert (
        r.migration_hint
        == "Rename statement_descriptor to statement_descriptor_suffix in calls to POST /v1/charges"
    )
    assert r.rule_hits == ["renamed"]


def test_rename_pair_by_levenshtein() -> None:
    e = ev(
        "mcp",
        [
            {
                "op": "removed",
                "path": "/tools/get_wether",
                "before": {"inputSchema": {"type": "object"}},
            },
            {
                "op": "added",
                "path": "/tools/get_weather",
                "after": {"inputSchema": {"type": "object", "properties": {"q": {}}}},
            },
        ],
    )
    r = classify_by_rules(e)
    assert r is not None and r.category == "rename"
    assert r.summary == "Tool get_wether was renamed to get_weather"
    assert r.migration_hint == "Rename get_wether to get_weather"


def test_no_rename_for_trivial_schema_and_distant_names() -> None:
    e = ev(
        "openapi",
        [
            {
                "op": "removed",
                "path": f"{BODY}/properties/description",
                "before": {"type": "string"},
            },
            {"op": "added", "path": f"{BODY}/properties/metadata", "after": {"type": "string"}},
        ],
    )
    r = classify_by_rules(e)
    assert r is not None
    assert r.category == "removal"
    assert "renamed" not in r.rule_hits
    assert "added_optional" in r.rule_hits


def test_short_names_do_not_pair_on_distance_alone() -> None:
    e = ev(
        "mcp",
        [
            {"op": "removed", "path": "/tools/a", "before": {"inputSchema": {"type": "object"}}},
            {
                "op": "added",
                "path": "/tools/b",
                "after": {"inputSchema": {"type": "object", "properties": {}}},
            },
        ],
    )
    r = classify_by_rules(e)
    assert r is not None and r.category == "removal" and "renamed" not in r.rule_hits


def test_exact_schema_match_beats_name_distance_when_pairing() -> None:
    schema = {
        "description": "Alerts",
        "inputSchema": {"type": "object", "properties": {"state": {"type": "string"}}},
    }
    e = ev(
        "mcp",
        [
            {
                "op": "removed",
                "path": "/tools/get_alert",
                "before": {"inputSchema": {"type": "object"}},
            },
            {"op": "removed", "path": "/tools/get_alerts", "before": schema},
            {"op": "added", "path": "/tools/list_alerts", "after": schema},
        ],
    )
    r = classify_by_rules(e)
    assert r is not None
    assert r.summary.startswith("Tool get_alert was removed")
    assert "renamed" in r.rule_hits
    assert r.migration_hint == "Stop calling tool get_alert or switch to a replacement tool"


def test_moved_parameter_with_identical_definition_is_noise() -> None:
    p = {"name": "limit", "in": "query", "schema": {"type": "integer"}}
    e = ev(
        "openapi",
        [
            {"op": "removed", "path": f"{CHARGES}/parameters/1", "before": p},
            {"op": "added", "path": f"{CHARGES}/parameters/3", "after": p},
        ],
    )
    assert classify_by_rules(e) is None


# ---- normalisation of generic-diff artefacts -------------------------------------------


def test_required_flag_flip_on_parameter() -> None:
    e = ev(
        "openapi",
        [
            {
                "op": "type_changed",
                "path": f"{CHARGES}/parameters/2/required",
                "before": False,
                "after": True,
            }
        ],
    )
    r = classify_by_rules(e)
    assert r is not None and r.category == "required_param"
    assert "parameter #2" in r.summary


def test_required_list_with_additions_and_removals() -> None:
    e = ev(
        "openapi",
        [
            {
                "op": "text_changed",
                "path": f"{BODY}/required",
                "before": ["a", "b"],
                "after": ["b", "c"],
            }
        ],
    )
    r = classify_by_rules(e)
    assert r is not None
    assert r.severity == "breaking" and r.rule_hits == ["required_added", "required_removed"]
    assert "'c'" in r.summary


def test_enum_constraint_introduced_is_a_type_change() -> None:
    e = ev(
        "openapi",
        [{"op": "added", "path": f"{BODY}/properties/currency/enum", "after": ["usd", "eur"]}],
    )
    r = classify_by_rules(e)
    assert r is not None and r.category == "type_change"
    assert "unconstrained to one of 'usd', 'eur'" in r.summary


def test_enum_constraint_dropped_is_degraded() -> None:
    e = ev(
        "openapi",
        [{"op": "removed", "path": f"{BODY}/properties/currency/enum", "before": ["usd", "eur"]}],
    )
    r = classify_by_rules(e)
    assert r is not None and (r.severity, r.category) == ("degraded", "behaviour")


def test_security_scheme_description_is_text_only() -> None:
    e = ev(
        "openapi",
        [
            {
                "op": "text_changed",
                "path": "/components/securitySchemes/oauth2/description",
                "before": "a",
                "after": "b",
            }
        ],
    )
    assert classify_by_rules(e) is None


def test_security_flow_change_is_auth() -> None:
    e = ev(
        "openapi",
        [
            {
                "op": "text_changed",
                "path": "/components/securitySchemes/oauth2/flows/authorizationCode/tokenUrl",
                "before": "https://a/token",
                "after": "https://b/token",
            }
        ],
    )
    r = classify_by_rules(e)
    assert r is not None and r.category == "auth" and r.affected_paths == ["*"]


def test_undeprecation_is_ignored() -> None:
    e = ev(
        "openapi",
        [{"op": "type_changed", "path": f"{CHARGES}/deprecated", "before": True, "after": False}],
    )
    assert classify_by_rules(e) is None


def test_deprecated_with_sunset_date() -> None:
    e = ev(
        "openapi",
        [
            {
                "op": "deprecated",
                "path": CHARGES,
                "after": {"deprecated": True, "x-sunset": "2027-01-01"},
            }
        ],
    )
    r = classify_by_rules(e)
    assert r is not None and r.summary == "POST /v1/charges is deprecated (sunset 2027-01-01)"


def test_prompt_argument_required_added() -> None:
    e = ev(
        "mcp",
        [
            {
                "op": "added",
                "path": "/prompts/briefing/arguments/1",
                "after": {"name": "city", "required": True},
            }
        ],
    )
    r = classify_by_rules(e)
    assert (
        r is not None and r.category == "required_param" and r.affected_paths == ["prompt:briefing"]
    )


# ---- classify_event placeholder ---------------------------------------------------------


def test_classify_event_placeholder_docs_only_for_descriptions() -> None:
    e = ev(
        "mcp",
        [
            {
                "op": "text_changed",
                "path": "/tools/get_weather/description",
                "before": "a",
                "after": "b",
            }
        ],
    )
    c = classify_event(e, display_name="Weather MCP")
    assert c.severity == "unknown" and c.category == "docs_only"
    assert c.summary == "[Weather MCP] 1 text-only change; needs review"
    assert c.rule_hits == ["text_only"]
    assert c.affected_paths == ["tool:get_weather"]


def test_classify_event_placeholder_behaviour_for_changelog() -> None:
    e = ev(
        "changelog",
        [
            {"op": "text_changed", "path": "/paragraphs/1", "before": None, "after": "x"},
            {"op": "text_changed", "path": "/paragraphs/2", "before": "y", "after": "z"},
        ],
    )
    c = classify_event(e)
    assert c.category == "behaviour" and c.summary == "2 text-only changes; needs review"


def test_classify_event_placeholder_for_empty_event() -> None:
    c = classify_event(ev("openapi", []))
    assert c.severity == "unknown" and c.summary == "No classifiable changes; needs review"


def test_rules_are_pure_and_do_not_import_server_modules() -> None:
    import sys
    from pathlib import Path

    import kintsua.classify.rules as rules_mod

    src = Path(rules_mod.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "kintsua.core.config",
        "kintsua.core.db",
        "kintsua.core.models",
        "sqlalchemy",
        "anthropic",
        "openai",
    ):
        assert forbidden not in src
    assert "kintsua.classify.rules" in sys.modules
