"""OpenAPI watcher: canonicalisation (3.x, Swagger 2.0, Discovery), the native differ, rename
detection, the oasdiff mapping and the HTTP fetch (respx)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from kintsua.core.events import ChangeOp
from kintsua.watchers.base import SourceRef
from kintsua.watchers.openapi import (
    OpenAPIWatcher,
    SpecFormatError,
    canonicalise,
    dereference,
    detect_format,
    detect_renames,
    diff_openapi,
    map_oasdiff_changelog,
    run_oasdiff,
    swagger2_to_openapi3,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "watchers"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text("utf-8"))


def kinds(ops):
    return {(o.op, o.path) for o in ops}


# --------------------------------------------------------------------------------------
# canonicalisation
# --------------------------------------------------------------------------------------


def test_canonicalise_openapi3_drops_volatile_keys_and_dereferences():
    body = canonicalise(load("openapi3_before.json"))
    assert detect_format(load("openapi3_before.json")) == "openapi3"
    assert set(body) == {"components", "info", "paths", "security"}
    assert body["info"] == {"title": "Pets API", "version": "1.0.0"}
    get = body["paths"]["/v1/pets"]["get"]
    assert "x-internal" not in get["parameters"][0]
    assert "example" not in get["responses"]["200"]["content"]["application/json"]
    # structural ref (components/parameters) is always inlined
    assert body["paths"]["/v1/pets/{id}"]["parameters"][0]["name"] == "id"
    # schema refs inlined one level: Pet is expanded, its Owner ref is kept
    schema = body["paths"]["/v1/pets/{id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema["type"] == "object" and "weight_kg" in schema["properties"]
    assert schema["properties"]["owner"] == {"$ref": "#/components/schemas/Owner"}
    # components keep their refs (reported once, at the component)
    assert body["components"]["schemas"]["Owner"]["properties"]["pet"] == {
        "$ref": "#/components/schemas/Pet"
    }
    assert body["components"]["securitySchemes"]["apiKey"]["type"] == "apiKey"
    assert body["security"] == [{"apiKey": []}]
    # sorted keys all the way down
    assert list(body) == sorted(body)
    assert list(body["paths"]) == sorted(body["paths"])
    assert list(schema["properties"]) == sorted(schema["properties"])


def test_dereference_is_cycle_safe_at_any_depth():
    doc = load("openapi3_before.json")
    out = dereference(doc["paths"], doc, max_schema_depth=25)
    text = json.dumps(out)
    assert '"$ref"' in text  # the Pet <-> Owner cycle is cut by keeping a $ref string
    assert len(text) < 200_000


def test_deref_depth_is_configurable():
    body = canonicalise(load("openapi3_before.json"), {"deref_depth": 0})
    schema = body["paths"]["/v1/pets/{id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema == {"$ref": "#/components/schemas/Pet"}
    # structural refs are still resolved
    assert body["paths"]["/v1/pets/{id}"]["parameters"][0]["name"] == "id"


def test_swagger2_conversion():
    raw = load("swagger2.json")
    assert detect_format(raw) == "swagger2"
    doc3 = swagger2_to_openapi3(raw)
    post = doc3["paths"]["/pet"]["post"]
    assert post["requestBody"]["required"] is True
    assert post["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Pet"
    }
    assert "application/xml" in post["requestBody"]["content"]
    assert post["security"] == [{"petstore_auth": ["write:pets", "read:pets"]}]
    get = doc3["paths"]["/pet/findByStatus"]["get"]
    p = get["parameters"][0]
    assert p["name"] == "status" and p["required"] is True
    assert p["schema"]["type"] == "array" and p["schema"]["items"]["enum"] == [
        "available",
        "pending",
        "sold",
    ]
    assert "collectionFormat" not in p["schema"] and "type" not in p
    assert get["responses"]["200"]["content"]["application/json"]["schema"]["items"] == {
        "$ref": "#/components/schemas/Pet"
    }
    form = doc3["paths"]["/pet/{petId}"]["post"]
    body = form["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]
    assert body["required"] == ["status"] and set(body["properties"]) == {"name", "status"}
    assert form["deprecated"] is True
    upload = doc3["paths"]["/pet/{petId}/uploadImage"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]
    assert upload["properties"]["file"] == {"type": "string", "format": "binary"}
    assert doc3["paths"]["/pet/{petId}"]["parameters"][0] == {
        "$ref": "#/components/parameters/petId"
    }
    comps = doc3["components"]
    assert comps["schemas"]["Pet"]["properties"]["category"] == {
        "$ref": "#/components/schemas/Category"
    }
    assert comps["securitySchemes"]["api_key"] == {
        "type": "apiKey",
        "name": "api_key",
        "in": "header",
    }
    oauth = comps["securitySchemes"]["petstore_auth"]
    assert oauth["type"] == "oauth2" and "implicit" in oauth["flows"]
    assert oauth["flows"]["implicit"]["scopes"]["read:pets"] == "read your pets"
    # the full pipeline resolves the parameter ref and keeps the version
    canon = canonicalise(raw)
    assert canon["info"]["version"] == "1.0.7"
    assert canon["paths"]["/pet/{petId}"]["parameters"][0]["name"] == "petId"
    assert "example" not in canon["components"]["schemas"]["Pet"]["properties"]["name"]


def test_discovery_conversion():
    raw = load("discovery.json")
    assert detect_format(raw) == "discovery"
    body = canonicalise(raw, {"format": "discovery"})
    assert canonicalise(raw) == body  # auto-detected
    assert body["info"] == {"title": "Google Drive API", "version": "v3"}
    assert set(body["paths"]) == {
        "/drive/v3/files",
        "/drive/v3/files/{fileId}",
        "/drive/v3/files/{fileId}/comments",
        "/drive/v3/about",
    }
    lst = body["paths"]["/drive/v3/files"]["get"]
    assert lst["operationId"] == "drive.files.list"
    assert [p["name"] for p in lst["parameters"]] == ["corpus", "pageSize", "q", "spaces"]
    corpus = lst["parameters"][0]
    assert corpus["deprecated"] is True and corpus["schema"]["enum"] == ["domain", "user"]
    assert "enumDescriptions" not in corpus["schema"]
    spaces = lst["parameters"][3]
    assert spaces["schema"] == {"type": "array", "items": {"type": "string"}}
    assert lst["security"] == [
        {
            "oauth2": [
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/drive.readonly",
            ]
        }
    ]
    resp = lst["responses"]["200"]["content"]["application/json"]["schema"]
    assert resp["properties"]["files"]["items"] == {"$ref": "#/components/schemas/File"}
    get = body["paths"]["/drive/v3/files/{fileId}"]["get"]
    assert get["parameters"][0] == {
        "in": "path",
        "name": "fileId",
        "required": True,
        "schema": {"type": "string"},
        "description": "The ID of the file.",
    } or (
        get["parameters"][0]["name"] == "fileId"
        and get["parameters"][0]["in"] == "path"
        and get["parameters"][0]["required"] is True
    )
    create = body["paths"]["/drive/v3/files"]["post"]
    assert "id" in create["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert body["components"]["schemas"]["File"]["properties"]["owners"]["items"] == {
        "$ref": "#/components/schemas/User"
    }
    flows = body["components"]["securitySchemes"]["oauth2"]["flows"]["implicit"]
    assert "https://www.googleapis.com/auth/drive.readonly" in flows["scopes"]
    # a method losing a parameter is a removed op with the same differ
    changed = json.loads(json.dumps(raw))
    del changed["resources"]["files"]["methods"]["list"]["parameters"]["q"]
    ops = diff_openapi(body, canonicalise(changed))
    assert ("removed", "/paths/~1drive~1v3~1files/get/parameters/2") in kinds(ops)


def test_detect_format_rejects_garbage():
    with pytest.raises(SpecFormatError):
        detect_format({"hello": "world"})


# --------------------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------------------


def test_native_diff_covers_every_change_kind():
    before = canonicalise(load("openapi3_before.json"))
    after = canonicalise(load("openapi3_after.json"))
    ops = diff_openapi(before, after)
    s = kinds(ops)
    p = "/paths/~1v1~1pets/get"
    # parameters (by name+in, index pointers into the merged list)
    assert ("removed", f"{p}/parameters/0") in s  # limit (index in the old doc)
    limit = next(o for o in ops if o.op == "removed" and o.path == f"{p}/parameters/0")
    assert limit.before["name"] == "limit" and limit.before["in"] == "query"
    assert ("enum_changed", f"{p}/parameters/0/schema") in s  # status (index in the new doc)
    enum = next(o for o in ops if o.op == "enum_changed")
    assert enum.before == ["available", "pending", "sold"] and enum.after == [
        "available",
        "pending",
        "adopted",
    ]
    assert ("text_changed", f"{p}/parameters/1/description") in s  # sort
    assert ("added", f"{p}/parameters/2") in s  # page
    assert ("added", f"{p}/parameters/3") in s and (
        "required_added",
        f"{p}/parameters/3",
    ) in s  # tenant
    tenant = next(o for o in ops if o.op == "required_added" and o.path == f"{p}/parameters/3")
    assert tenant.after == "tenant"
    assert ("text_changed", f"{p}/summary") in s
    # operations
    assert ("removed", "/paths/~1v1~1legacy~1export/get") in s
    removed = next(o for o in ops if o.path == "/paths/~1v1~1legacy~1export/get")
    assert removed.before == {
        "method": "GET",
        "path": "/v1/legacy/export",
        "operationId": "legacyExport",
    }
    assert ("added", "/paths/~1v1~1pets~1{id}~1photos/post") in s
    assert ("deprecated", "/paths/~1v1~1pets~1{id}/delete") in s
    # auth
    assert ("auth_changed", "/security") in s
    assert ("auth_changed", "/components/securitySchemes/apiKey") in s
    assert ("auth_changed", "/components/securitySchemes/oauth2") in s
    assert ("auth_changed", "/paths/~1v1~1owners~1{ownerId}/get/security") in s
    # component schemas (refs kept, reported once)
    assert ("type_changed", "/components/schemas/Pet/properties/weight_kg") in s
    assert ("text_changed", "/components/schemas/Pet/properties/tag/description") in s
    assert ("renamed", "/components/schemas/NewPet/properties/tag") in s
    ren = next(
        o
        for o in ops
        if o.op == "renamed" and o.path == "/components/schemas/NewPet/properties/tag"
    )
    assert ren.before == "tag" and ren.after == "tags"
    assert ("added", "/components/schemas/NewPet/properties/species") in s
    assert ("required_added", "/components/schemas/NewPet/properties/species") in s
    # ... and attributed to the operations that inline them (deref depth 1)
    rb = "/paths/~1v1~1pets/post/requestBody/content/application~1json/schema"
    assert ("renamed", f"{rb}/properties/tag") in s
    assert ("required_added", f"{rb}/properties/species") in s
    resp = "/paths/~1v1~1pets~1{id}/get/responses/200/content/application~1json/schema"
    assert ("type_changed", f"{resp}/properties/weight_kg") in s
    # no false renames: limit -> page differ in type and name
    assert not any(o.op == "renamed" and "parameters" in o.path for o in ops)
    # nothing about the untouched owner endpoint besides security
    assert {pth for _, pth in s if pth.startswith("/paths/~1v1~1owners")} == {
        "/paths/~1v1~1owners~1{ownerId}/get/security"
    }
    # self diff is empty
    assert diff_openapi(before, before) == []
    assert diff_openapi(after, after) == []


def test_watcher_diff_uses_native_walker_and_version():
    w = OpenAPIWatcher()
    before = canonicalise(load("openapi3_before.json"))
    after = canonicalise(load("openapi3_after.json"))
    from kintsua.core.events import CanonicalDoc

    ops = w.diff(
        CanonicalDoc(version="1.0.0", body=before), CanonicalDoc(version="2.0.0", body=after)
    )
    assert ("deprecated", "/paths/~1v1~1pets~1{id}/delete") in kinds(ops)


def test_detect_renames_pairs_siblings_by_shape_or_distance():
    ops = [
        ChangeOp(
            op="removed", path="/components/schemas/A/properties/petName", before={"type": "string"}
        ),
        ChangeOp(
            op="added", path="/components/schemas/A/properties/pet_name", after={"type": "integer"}
        ),
        ChangeOp(
            op="removed",
            path="/components/schemas/B/properties/colour",
            before={"type": "string", "description": "c"},
        ),
        ChangeOp(
            op="added",
            path="/components/schemas/B/properties/tint",
            after={"type": "string", "description": "t"},
        ),
        ChangeOp(
            op="removed", path="/components/schemas/C/properties/x", before={"type": "string"}
        ),
        ChangeOp(op="added", path="/components/schemas/D/properties/y", after={"type": "string"}),
        ChangeOp(op="text_changed", path="/info/title", before="a", after="b"),
    ]
    out = detect_renames(ops)
    k = kinds(out)
    assert ("renamed", "/components/schemas/A/properties/petName") in k  # distance 2
    assert (
        "renamed",
        "/components/schemas/B/properties/colour",
    ) in k  # identical shape (text ignored)
    assert ("removed", "/components/schemas/C/properties/x") in k and (
        "added",
        "/components/schemas/D/properties/y",
    ) in k
    assert ("text_changed", "/info/title") in k
    a = next(o for o in out if o.op == "renamed" and "A/" in o.path)
    assert a.before == "petName" and a.after == "pet_name"


def test_map_oasdiff_changelog_ids():
    ops = map_oasdiff_changelog(load("oasdiff_changelog.json"))
    k = kinds(ops)
    assert ("removed", "/paths/~1v1~1legacy~1export/get") in k
    assert ("required_added", "/paths/~1v1~1pets/get") in k
    assert ("enum_changed", "/paths/~1v1~1pets/get") in k
    assert ("deprecated", "/paths/~1v1~1pets~1{id}/delete") in k
    assert ("auth_changed", "/security") in k
    assert ("added", "/paths/~1v1~1pets~1{id}~1photos/post") in k
    assert ("type_changed", "/paths/~1v1~1pets~1{id}/get") in k
    assert ("text_changed", "/paths/~1v1~1pets/get") in k  # unknown id -> text_changed
    req = next(o for o in ops if o.op == "required_added")
    assert req.after["oasdiff"] == "new-required-request-parameter" and req.after["args"] == [
        "query",
        "tenant",
    ]


def test_run_oasdiff_is_opt_in(monkeypatch):
    monkeypatch.delenv("KINTSUA_USE_OASDIFF", raising=False)
    assert run_oasdiff({}, {}) is None
    monkeypatch.setenv("KINTSUA_USE_OASDIFF", "1")
    monkeypatch.setattr("kintsua.watchers.openapi.shutil.which", lambda name: None)
    assert run_oasdiff({}, {}) is None


# --------------------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------------------

YAML_SPEC = """
openapi: 3.0.0
info: {title: Mini, version: "3.2.1"}
servers: [{url: https://api.mini.example}]
paths:
  /things:
    get:
      operationId: listThings
      x-codegen-request-body-name: body
      responses:
        "200": {description: ok}
"""


@respx.mock
async def test_fetch_follows_redirects_parses_yaml_and_sends_headers():
    respx.get("https://specs.example.com/old.yaml").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://specs.example.com/openapi.yaml"}
        )
    )
    route = respx.get("https://specs.example.com/openapi.yaml").mock(
        return_value=httpx.Response(200, text=YAML_SPEC, headers={"content-type": "text/yaml"})
    )
    doc = await OpenAPIWatcher().fetch(
        SourceRef(
            kind="openapi",
            locator="https://specs.example.com/old.yaml",
            meta={"headers": {"X-Test": "1"}, "user_agent": "kintsua-test/9"},
        )
    )
    assert doc.version == "3.2.1"
    assert list(doc.body["paths"]) == ["/things"]
    assert "x-codegen-request-body-name" not in doc.body["paths"]["/things"]["get"]
    req = route.calls.last.request
    assert req.headers["user-agent"] == "kintsua-test/9" and req.headers["x-test"] == "1"


@respx.mock
async def test_fetch_rejects_non_spec_and_http_errors():
    respx.get("https://specs.example.com/nope.json").mock(
        return_value=httpx.Response(200, json={"hello": 1})
    )
    with pytest.raises(SpecFormatError):
        await OpenAPIWatcher().fetch(
            SourceRef(kind="openapi", locator="https://specs.example.com/nope.json")
        )
    respx.get("https://specs.example.com/500.json").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await OpenAPIWatcher().fetch(
            SourceRef(kind="openapi", locator="https://specs.example.com/500.json")
        )
