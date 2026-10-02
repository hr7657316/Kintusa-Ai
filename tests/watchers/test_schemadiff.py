"""Unit tests for the JSON Schema differ: one case per op kind plus refs and cycles."""

from __future__ import annotations

from kintsua.watchers.schemadiff import diff_json_schema, levenshtein

BASE = "/tools/get_weather/inputSchema"


def kinds(ops):
    return {(o.op, o.path) for o in ops}


def obj(props, required=None, **extra):
    s = {"type": "object", "properties": props}
    if required:
        s["required"] = required
    s.update(extra)
    return s


def test_required_added_and_removed():
    before = obj(
        {"city": {"type": "string"}, "units": {"type": "string"}, "days": {"type": "integer"}},
        ["city", "days"],
    )
    after = obj(
        {"city": {"type": "string"}, "units": {"type": "string"}, "days": {"type": "integer"}},
        ["city", "units"],
    )
    ops = diff_json_schema(before, after, BASE)
    assert kinds(ops) == {
        ("required_added", f"{BASE}/properties/units"),
        ("required_removed", f"{BASE}/properties/days"),
    }
    added = next(o for o in ops if o.op == "required_added")
    assert added.before is None and added.after == "units"
    removed = next(o for o in ops if o.op == "required_removed")
    assert removed.before == "days" and removed.after is None


def test_type_changed_variants():
    cases = [
        ({"type": "integer"}, {"type": "string"}),
        ({"type": "string"}, {"type": ["string", "null"]}),
        ({"type": "string", "format": "date"}, {"type": "string", "format": "date-time"}),
        ({"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}),
        (
            {"type": "array", "items": {"type": "string"}},
            {"type": "array", "items": {"type": "integer"}},
        ),
    ]
    for b, a in cases:
        ops = diff_json_schema(obj({"x": b}), obj({"x": a}), BASE)
        assert any(
            o.op == "type_changed" and o.path.startswith(f"{BASE}/properties/x") for o in ops
        ), (b, a)
        assert not any(o.op in ("added", "removed") for o in ops)
    op = diff_json_schema(obj({"x": {"type": "integer"}}), obj({"x": {"type": "string"}}), BASE)[0]
    assert op.before == {"type": "integer"} and op.after == {"type": "string"}


def test_type_list_order_and_nullable_are_normalised():
    assert (
        diff_json_schema(
            obj({"x": {"type": ["string", "null"]}}), obj({"x": {"type": ["null", "string"]}}), BASE
        )
        == []
    )
    assert (
        diff_json_schema(
            obj({"x": {"type": "string", "nullable": True}}),
            obj({"x": {"type": ["string", "null"]}}),
            BASE,
        )
        == []
    )


def test_enum_changed():
    ops = diff_json_schema(
        obj({"units": {"type": "string", "enum": ["metric", "imperial"]}}),
        obj({"units": {"type": "string", "enum": ["metric", "imperial", "kelvin"]}}),
        BASE,
    )
    assert kinds(ops) == {("enum_changed", f"{BASE}/properties/units")}
    assert ops[0].before == ["metric", "imperial"] and ops[0].after == [
        "metric",
        "imperial",
        "kelvin",
    ]
    # same values, different order: no change
    assert (
        diff_json_schema(obj({"u": {"enum": ["a", "b"]}}), obj({"u": {"enum": ["b", "a"]}}), BASE)
        == []
    )
    # enum dropped entirely
    ops = diff_json_schema(
        obj({"u": {"type": "string", "enum": ["a"]}}), obj({"u": {"type": "string"}}), BASE
    )
    assert kinds(ops) == {("enum_changed", f"{BASE}/properties/u")} and ops[0].after is None
    # const behaves like a one-element enum
    ops = diff_json_schema(obj({"u": {"const": "a"}}), obj({"u": {"const": "b"}}), BASE)
    assert ops[0].op == "enum_changed" and ops[0].before == ["a"] and ops[0].after == ["b"]


def test_added_and_removed_properties():
    before = obj(
        {"a": {"type": "string"}, "b": {"type": "integer", "description": "gone"}}, ["a", "b"]
    )
    after = obj({"a": {"type": "string"}, "c": {"type": "boolean"}}, ["a"])
    ops = diff_json_schema(before, after, BASE)
    assert kinds(ops) == {("removed", f"{BASE}/properties/b"), ("added", f"{BASE}/properties/c")}
    removed = next(o for o in ops if o.op == "removed")
    assert removed.before == {"type": "integer", "description": "gone"} and removed.after is None
    # a removed required property does NOT also emit required_removed
    assert not any(o.op == "required_removed" for o in ops)


def test_new_required_property_emits_added_and_required_added():
    ops = diff_json_schema(
        obj({"a": {"type": "string"}}, ["a"]),
        obj({"a": {"type": "string"}, "b": {"type": "string"}}, ["a", "b"]),
        BASE,
    )
    assert kinds(ops) == {
        ("added", f"{BASE}/properties/b"),
        ("required_added", f"{BASE}/properties/b"),
    }


def test_text_changed_only_for_description_and_title():
    before = obj({"x": {"type": "string", "description": "old", "title": "X"}})
    after = obj({"x": {"type": "string", "description": "new", "title": "X!"}})
    ops = diff_json_schema(before, after, BASE)
    assert kinds(ops) == {
        ("text_changed", f"{BASE}/properties/x/description"),
        ("text_changed", f"{BASE}/properties/x/title"),
    }
    d = next(o for o in ops if o.path.endswith("/description"))
    assert d.before == "old" and d.after == "new"


def test_deprecated():
    ops = diff_json_schema(
        obj({"x": {"type": "string"}}), obj({"x": {"type": "string", "deprecated": True}}), BASE
    )
    assert kinds(ops) == {("deprecated", f"{BASE}/properties/x")}
    ops = diff_json_schema(
        obj({"x": {"type": "string"}}), obj({"x": {"type": "string", "x-deprecated": True}}), BASE
    )
    assert kinds(ops) == {("deprecated", f"{BASE}/properties/x")}
    # already deprecated: nothing new
    assert (
        diff_json_schema(obj({"x": {"deprecated": True}}), obj({"x": {"deprecated": True}}), BASE)
        == []
    )


def test_nested_objects_items_and_combinators():
    before = obj(
        {
            "filters": obj({"lang": {"type": "string"}, "region": {"type": "string"}}),
            "tags": {"type": "array", "items": {"type": "string"}},
            "value": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "other": {"anyOf": [{"type": "string"}]},
        }
    )
    after = obj(
        {
            "filters": obj(
                {"lang": {"type": "string", "enum": ["en"]}, "country": {"type": "string"}}
            ),
            "tags": {"type": "array", "items": {"type": "integer"}},
            "value": {"oneOf": [{"type": "string"}]},
            "other": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        }
    )
    ops = diff_json_schema(before, after, BASE)
    assert kinds(ops) == {
        ("enum_changed", f"{BASE}/properties/filters/properties/lang"),
        ("removed", f"{BASE}/properties/filters/properties/region"),
        ("added", f"{BASE}/properties/filters/properties/country"),
        ("type_changed", f"{BASE}/properties/tags/items"),
        ("removed", f"{BASE}/properties/value/oneOf/1"),
        ("added", f"{BASE}/properties/other/anyOf/1"),
    }


def test_defs_are_walked():
    before = {"type": "object", "$defs": {"Address": obj({"zip": {"type": "string"}})}}
    after = {
        "type": "object",
        "$defs": {"Address": obj({"zip": {"type": "integer"}}), "Geo": obj({})},
    }
    ops = diff_json_schema(before, after, "/schema")
    assert kinds(ops) == {
        ("type_changed", "/schema/$defs/Address/properties/zip"),
        ("added", "/schema/$defs/Geo"),
    }


def test_constraints_count_as_type_changes():
    ops = diff_json_schema(
        obj({"name": {"type": "string", "maxLength": 10}}),
        obj({"name": {"type": "string", "maxLength": 5, "pattern": "^a"}}),
        BASE,
    )
    assert kinds(ops) == {
        ("type_changed", f"{BASE}/properties/name/maxLength"),
        ("type_changed", f"{BASE}/properties/name/pattern"),
    }


def test_additional_properties():
    ops = diff_json_schema(
        obj({}, additionalProperties=True), obj({}, additionalProperties=False), BASE
    )
    assert kinds(ops) == {("type_changed", f"{BASE}/additionalProperties")}
    ops = diff_json_schema(
        obj({}, additionalProperties={"type": "string"}),
        obj({}, additionalProperties={"type": "integer"}),
        BASE,
    )
    assert kinds(ops) == {("type_changed", f"{BASE}/additionalProperties")}


def test_identical_schemas_produce_no_ops():
    s = obj({"a": {"type": "string", "enum": ["x"], "description": "d"}}, ["a"])
    assert diff_json_schema(s, s, BASE) == []
    assert diff_json_schema({}, {}, BASE) == []
    assert diff_json_schema(None, None, BASE) == []


def test_resolvers_follow_refs_and_cut_cycles():
    comps_before = {
        "Pet": obj({"name": {"type": "string"}, "owner": {"$ref": "#/components/schemas/Owner"}}),
        "Owner": obj({"email": {"type": "string"}, "pet": {"$ref": "#/components/schemas/Pet"}}),
    }
    comps_after = {
        "Pet": obj({"name": {"type": "string"}, "owner": {"$ref": "#/components/schemas/Owner"}}),
        "Owner": obj({"email": {"type": "integer"}, "pet": {"$ref": "#/components/schemas/Pet"}}),
    }

    def resolver(comps):
        def resolve(node):
            ref = node.get("$ref", "")
            return comps.get(ref.rsplit("/", 1)[-1], node) if ref else node

        return resolve

    ops = diff_json_schema(
        {"$ref": "#/components/schemas/Pet"},
        {"$ref": "#/components/schemas/Pet"},
        "/schema",
        resolve_before=resolver(comps_before),
        resolve_after=resolver(comps_after),
        max_ref_depth=4,
    )
    assert kinds(ops) == {("type_changed", "/schema/properties/owner/properties/email")}
    # without resolvers, identical refs are simply equal and nothing is reported
    assert (
        diff_json_schema(
            {"$ref": "#/components/schemas/Pet"}, {"$ref": "#/components/schemas/Pet"}, "/s"
        )
        == []
    )
    # a changed ref target is a type change even with resolvers
    ops = diff_json_schema(
        {"$ref": "#/components/schemas/Pet"},
        {"$ref": "#/components/schemas/Owner"},
        "/schema",
        resolve=resolver(comps_after),
    )
    assert ("type_changed", "/schema") in kinds(ops)


def test_levenshtein():
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein("tag", "tags") == 1
    assert levenshtein("", "ab") == 2
    assert levenshtein("same", "same") == 0
    assert levenshtein("limit", "page") == 5
