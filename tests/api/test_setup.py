"""The GitHub App manifest flow (conversion endpoint mocked with respx)."""

from __future__ import annotations

import html
import json

import httpx
import respx

from kintsua.api.deps import MANIFEST_SALT, serializer
from kintsua.api.setup import build_manifest


def test_setup_page_posts_the_manifest_to_github(client, settings):
    base = settings.base_url.rstrip("/")
    manifest = build_manifest(settings)
    assert manifest["name"] == "kintsua" and manifest["url"] == base and manifest["public"] is True
    assert manifest["hook_attributes"] == {"url": f"{base}/webhooks/github", "active": True}
    assert manifest["redirect_url"] == f"{base}/setup/github/callback"
    assert manifest["callback_urls"] == [f"{base}/auth/callback"]
    assert manifest["default_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "metadata": "read",
        "checks": "read",
    }
    assert set(manifest["default_events"]) == {
        "push",
        "pull_request",
        "issues",
        "check_suite",
        "check_run",
    }

    r = client.get("/setup/github")
    assert r.status_code == 200, r.text
    page = html.unescape(r.text)
    assert 'action="https://github.com/settings/apps/new?state=' in page
    assert 'name="manifest"' in page
    assert json.dumps(manifest, indent=2) in page
    assert f"{base}/webhooks/github" in page

    r = client.get("/setup/github?org=acme&name=kintsua-dev")
    page = html.unescape(r.text)
    assert "https://github.com/organizations/acme/settings/apps/new?state=" in page
    assert '"name": "kintsua-dev"' in page


@respx.mock
def test_setup_callback_shows_env_lines_and_stores_nothing(client, settings):
    route = respx.post("https://api.github.com/app-manifests/abc123/conversions").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 4242,
                "slug": "kintsua-dev",
                "name": "kintsua-dev",
                "client_id": "Iv1.client",
                "client_secret": "oauthsecret",
                "webhook_secret": "whsec_xyz",
                "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n",
                "html_url": "https://github.com/apps/kintsua-dev",
            },
        )
    )
    state = serializer(MANIFEST_SALT).dumps({"n": "nonce"})
    r = client.get(f"/setup/github/callback?code=abc123&state={state}")
    assert r.status_code == 200, r.text
    page = html.unescape(r.text)
    assert route.called
    assert "GITHUB_APP_ID=4242" in page and "GITHUB_APP_SLUG=kintsua-dev" in page
    assert "GITHUB_WEBHOOK_SECRET=whsec_xyz" in page
    assert (
        "GITHUB_OAUTH_CLIENT_ID=Iv1.client" in page
        and "GITHUB_OAUTH_CLIENT_SECRET=oauthsecret" in page
    )
    assert (
        'GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\\nMIIE\\n-----END RSA PRIVATE KEY-----"'
        in page
    )
    assert "https://github.com/apps/kintsua-dev/installations/new" in page
    assert (
        settings.github_app_id is None and settings.github_webhook_secret == "test-webhook-secret"
    )

    # the POST form of the callback works too
    r = client.post("/setup/github/callback", data={"code": "abc123", "state": state})
    assert r.status_code == 200 and "GITHUB_APP_ID=4242" in html.unescape(r.text)

    # bad inputs
    assert client.get("/setup/github/callback").status_code == 400
    assert client.get("/setup/github/callback?code=abc123&state=forged").status_code == 400
    respx.post("https://api.github.com/app-manifests/bad/conversions").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    r = client.get(f"/setup/github/callback?code=bad&state={state}")
    assert r.status_code == 502 and "Not Found" in r.text
