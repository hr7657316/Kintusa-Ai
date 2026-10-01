"""SDK keys: created or rotated through the session, shown once, stored hashed."""

from __future__ import annotations

import json

from kintsua.core.models import Org
from kintsua.core.util import sha256_hex
from tests.api.conftest import INSTALLATION_ID, bearer, mint_session


def test_create_key_is_shown_once_and_stored_hashed(client, factory, session):
    org = factory.org()
    mint_session(client)
    r = client.post("/v1/keys", json={})
    assert r.status_code == 200, r.text
    data = r.json()
    key = data["key"]
    assert key.startswith("kts_") and len(key) == 36
    assert data["prefix"] == key[:12] and data["rotated"] is False and data["org_id"] == str(org.id)
    session.expire_all()
    org = session.get(Org, org.id)
    assert org.sdk_key_hash == sha256_hex(key)
    assert org.sdk_key_prefix == key[:12] and org.sdk_key_created_at is not None
    assert key not in (org.sdk_key_hash or "")

    listing = client.get("/v1/keys").json()["keys"]
    assert listing == [
        {
            "org_id": str(org.id),
            "org": "acme",
            "prefix": key[:12],
            "created_at": org.sdk_key_created_at.isoformat(),
            "has_key": True,
        }
    ]
    assert key not in json.dumps(listing)

    # the key authenticates the SDK endpoints (404 = auth passed, repo unknown)
    r = client.post("/v1/ingest", json={"repo": "acme/nope", "events": []}, headers=bearer(key))
    assert r.status_code == 404

    # rotating invalidates the old key
    r = client.post("/v1/keys", json={"org_id": str(org.id)})
    assert r.status_code == 200 and r.json()["rotated"] is True
    key2 = r.json()["key"]
    assert key2 != key
    assert (
        client.post(
            "/v1/ingest", json={"repo": "acme/nope", "events": []}, headers=bearer(key)
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/v1/ingest", json={"repo": "acme/nope", "events": []}, headers=bearer(key2)
        ).status_code
        == 404
    )

    # the HTMX fragment shows it once
    r = client.post("/v1/keys", data={"org_id": str(org.id)}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "KINTSUA_KEY=kts_" in r.text and "not shown again" in r.text


def test_keys_require_a_session_and_a_visible_org(client, factory):
    org = factory.org(installation_id=777)
    assert client.post("/v1/keys", json={}).status_code == 401
    assert client.get("/v1/keys").status_code == 401
    mint_session(client, [INSTALLATION_ID])
    assert client.post("/v1/keys", json={}).status_code == 404
    assert client.post("/v1/keys", json={"org_id": str(org.id)}).status_code == 404
    assert client.get("/v1/keys").json() == {"keys": []}


def test_several_orgs_need_an_explicit_org_id(client, factory):
    factory.org("a", installation_id=1)
    b = factory.org("b", installation_id=2)
    mint_session(client, [1, 2])
    assert client.post("/v1/keys", json={}).status_code == 400
    r = client.post("/v1/keys", json={"org_id": str(b.id)})
    assert r.status_code == 200 and r.json()["org"] == "b"
    assert len(client.get("/v1/keys").json()["keys"]) == 2
