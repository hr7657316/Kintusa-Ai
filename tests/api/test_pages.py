"""The landing page and the two settings pages."""

from __future__ import annotations

from kintsua.api.billing import PRICING_LINE
from tests.api.conftest import mint_session


def test_landing_page_has_pricing_privacy_install_and_cli(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    assert PRICING_LINE in html
    assert "Free for public repos and up to 5 watched integrations" in html
    assert "Pro $29 per watched integration per month" in html
    assert "no payloads and no PII" in html and "hashed at rest" in html and "data deletion" in html
    assert "https://github.com/apps/kintsua/installations/new" in html
    assert "kintsua watch snapshot" in html and "kintsua watch diff" in html
    assert 'href="/report/weekly"' in html
    assert 'action="/report/subscribe"' in html and 'hx-post="/report/subscribe"' in html
    assert "unpkg.com/htmx.org" in html
    assert "opens a GitHub issue within an hour" in html


def test_settings_pages_redirect_anonymous_users_to_login(client):
    r = client.get("/settings", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login?next=/settings"
    r = client.get("/settings/acme/support-agent", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login?next=/settings/acme")
    r = client.get("/settings")
    assert r.status_code == 200 and "Sign in with GitHub" in r.text


def test_settings_index_lists_only_visible_repos(client, factory):
    org = factory.org()
    factory.repo(org)
    other = factory.org("other", installation_id=9)
    factory.repo(other, "other/app")
    mint_session(client)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "acme/support-agent" in r.text and "other/app" not in r.text
    assert 'href="/settings/acme/support-agent"' in r.text
    mint_session(client, [9999])
    r = client.get("/settings")
    assert r.status_code == 200 and "No installation is visible" in r.text


def test_settings_repo_page_renders_map_drafts_mutes_and_key(client, factory):
    org = factory.org()
    repo = factory.repo(org, config={"mute": ["stripe:/v1/legacy/*"]})
    src = factory.source()
    dep = factory.dependency(repo, src)
    factory.call_site(dep)
    factory.dependency(repo, None, kind="http", name="api.unknown.example")
    impact, _, cls = factory.breaking_impact(repo, source=src)
    mint_session(client)

    r = client.get("/settings/acme/support-agent")
    assert r.status_code == 200, r.text
    html = r.text
    assert "acme/support-agent" in html
    # dependency map rows, watched and grey
    assert "row-watched" in html and "row-unwatched" in html
    assert "<code>stripe</code>" in html and "api.unknown.example" in html
    assert "1 watched" in html and "1 unwatched" in html
    # the draft impact and its "Open issue" button
    assert cls.summary in html
    assert "Open issue" in html and str(impact.id) in html
    assert f'hx-post="/v1/repos/{repo.id}/issues/open"' in html
    assert "1 draft issue(s)" in html
    # mute rules, fix mode, key, deletion, yml snippet
    assert "stripe:/v1/legacy/*" in html and f'hx-post="/v1/repos/{repo.id}/mute"' in html
    assert 'id="fix-mode"' in html and "<strong>issue_only</strong>" in html
    assert "Create SDK key" in html and 'hx-post="/v1/keys"' in html
    assert f'hx-delete="/v1/repos/{repo.id}/data"' in html and "hx-confirm" in html
    assert "mode: pr          # pr | issue_only" in html
    assert "unpkg.com/htmx.org" in html

    # case-insensitive lookup; 404 outside the session's installations
    assert client.get("/settings/ACME/Support-Agent").status_code == 200
    mint_session(client, [999])
    r = client.get("/settings/acme/support-agent")
    assert r.status_code == 404 and "Not found" in r.text
