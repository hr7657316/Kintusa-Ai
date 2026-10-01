"""PyPI / npm watchers with the registries, GitHub and raw.githubusercontent mocked."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from kintsua.core.events import CanonicalDoc
from kintsua.core.util import utcnow
from kintsua.watchers.base import SnapshotRecord, SourceRef, compare
from kintsua.watchers.packages import (
    NpmWatcher,
    PyPIWatcher,
    extract_changelog_section,
    fetch_release_notes,
    github_repo,
    is_prerelease,
    major_of,
    normalise_repo_url,
    sort_versions,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "watchers"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text("utf-8"))


def kinds(ops):
    return {(o.op, o.path) for o in ops}


CHANGELOG = (FIX / "CHANGELOG.md").read_text("utf-8")
GH_RELEASES = r"https://api\.github\.com/repos/acme/acme-python/releases/tags/.*"


@respx.mock
async def test_pypi_fetch_diff_and_release_notes_from_changelog():
    reg = respx.get("https://pypi.org/pypi/acme-sdk/json")
    reg.side_effect = [
        httpx.Response(200, json=load("pypi_before.json")),
        httpx.Response(200, json=load("pypi_after.json")),
    ]
    respx.get(url__regex=GH_RELEASES).mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    respx.get("https://raw.githubusercontent.com/acme/acme-python/HEAD/CHANGELOG.md").mock(
        return_value=httpx.Response(200, text=CHANGELOG)
    )
    w = PyPIWatcher()
    src = SourceRef(kind="pypi", locator="acme-sdk")
    d1 = await w.fetch(src)
    d2 = await w.fetch(src)

    assert d1.version == "1.2.3" and d1.body["latest"] == "1.2.3"
    # yanked 1.2.0 and the 2.0.0a1 prerelease are excluded, order is semver
    assert d1.body["versions"] == ["0.9.0", "1.0.0", "1.1.0", "1.2.3"]
    assert d1.body["repository"] == "https://github.com/acme/acme-python"
    assert d1.body["requires_python"] == ">=3.8" and d1.body["name"] == "acme-sdk"
    assert d1.body["release_notes"]["1.2.3"].startswith("## [1.2.3]")
    assert d2.version == "2.0.0"
    notes = d2.body["release_notes"]["2.0.0"]
    assert notes.startswith("## [2.0.0]") and "BREAKING" in notes and "## [1.2.4]" not in notes

    ops = w.diff(d1, d2)
    s = kinds(ops)
    assert ("text_changed", "/latest") in s
    assert ("deprecated", "/latest") in s
    dep = next(o for o in ops if o.op == "deprecated")
    assert dep.before == "1" and dep.after == "2"
    assert ("added", "/versions/1.2.4") in s and ("added", "/versions/2.0.0") in s
    assert ("text_changed", "/requires_python") in s
    assert not any(o.op == "removed" for o in ops)

    text = w.event_text(d1, d2)
    assert text.startswith("acme-sdk 1.2.3 -> 2.0.0") and "BREAKING" in text
    prev = SnapshotRecord(
        source_id=src.id,
        content_hash=d1.content_hash(),
        version="1.2.3",
        captured_at=utcnow(),
        body=d1.body,
    )
    ev = compare(w, src, prev, d2, text=text)
    assert (
        ev is not None
        and ev.from_version == "1.2.3"
        and ev.to_version == "2.0.0"
        and "BREAKING" in ev.text
    )


@respx.mock
async def test_pypi_prerelease_opt_in_and_skip_release_notes():
    respx.get("https://pypi.org/pypi/acme-sdk/json").mock(
        return_value=httpx.Response(200, json=load("pypi_before.json"))
    )
    d = await PyPIWatcher().fetch(
        SourceRef(
            kind="pypi", locator="acme-sdk", meta={"prerelease": True, "skip_release_notes": True}
        )
    )
    assert "2.0.0a1" in d.body["versions"] and d.body["release_notes"] == {}
    assert d.version == "1.2.3"


@respx.mock
async def test_npm_fetch_with_github_release_and_diff():
    respx.get(url__regex=r"https://registry\.npmjs\.org/@acme(%2F|/)sdk$").mock(
        return_value=httpx.Response(200, json=load("npm_registry.json"))
    )
    respx.get("https://api.github.com/repos/acme/acme-node/releases/tags/v2.0.0").mock(
        return_value=httpx.Response(200, json=load("gh_release.json"))
    )
    w = NpmWatcher()
    d = await w.fetch(SourceRef(kind="npm", locator="@acme/sdk"))
    assert d.version == "2.0.0" and d.body["name"] == "@acme/sdk"
    assert d.body["versions"] == ["1.0.0", "1.1.0", "2.0.0"]  # 3.0.0-beta.1 excluded
    assert d.body["engines"] == {"node": ">=18"}
    assert d.body["deprecated"] == "use @acme/sdk-v2 instead"
    assert d.body["repository"] == "https://github.com/acme/acme-node"
    assert d.body["release_notes"]["2.0.0"].startswith("## Breaking changes")

    before = CanonicalDoc(
        version="1.1.0",
        body={
            **d.body,
            "latest": "1.1.0",
            "versions": ["1.0.0", "1.1.0"],
            "engines": {"node": ">=16"},
            "deprecated": None,
            "release_notes": {},
        },
    )
    ops = w.diff(before, d)
    s = kinds(ops)
    assert ("deprecated", "/latest") in s and ("deprecated", "/deprecated") in s
    assert ("added", "/versions/2.0.0") in s and ("text_changed", "/engines") in s
    assert w.event_text(before, d).startswith("@acme/sdk 1.1.0 -> 2.0.0")


@respx.mock
async def test_github_rate_limit_falls_back_to_changelog_and_tag_variants():
    calls: list[str] = []

    def releases(request):
        calls.append(request.url.path)
        return httpx.Response(403, json={"message": "API rate limit exceeded"})

    respx.get(url__regex=GH_RELEASES).mock(side_effect=releases)
    respx.get("https://raw.githubusercontent.com/acme/acme-python/HEAD/CHANGELOG.md").mock(
        return_value=httpx.Response(200, text=CHANGELOG)
    )
    async with httpx.AsyncClient() as client:
        notes = await fetch_release_notes(
            client, "https://github.com/acme/acme-python", "1.2.4", package="acme-sdk"
        )
    assert notes.startswith("## [1.2.4]") and "retry on 429" in notes and "2.0.0" not in notes
    assert len(calls) == 1  # rate limited: stop hitting the API immediately

    # v-prefixed tag missing, bare tag present
    respx.clear()
    respx.get("https://api.github.com/repos/acme/acme-python/releases/tags/v9.0.0").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.github.com/repos/acme/acme-python/releases/tags/9.0.0").mock(
        return_value=httpx.Response(200, json={"body": "notes for nine"})
    )
    async with httpx.AsyncClient() as client:
        assert (
            await fetch_release_notes(client, "https://github.com/acme/acme-python", "9.0.0")
            == "notes for nine"
        )
    # not a GitHub repo -> nothing to fetch
    async with httpx.AsyncClient() as client:
        assert await fetch_release_notes(client, "https://gitlab.com/acme/x", "1.0.0") is None


@respx.mock
async def test_registry_errors_propagate():
    respx.get("https://pypi.org/pypi/nope/json").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await PyPIWatcher().fetch(SourceRef(kind="pypi", locator="nope"))


def test_extract_changelog_section_styles():
    assert extract_changelog_section(CHANGELOG, "1.2.4").startswith("## [1.2.4]")
    assert extract_changelog_section(CHANGELOG, "0.0.1") is None
    keep_a_changelog = "# Changelog\n\n## [Unreleased]\n\n## [1.1.0] - 2026-01-02\n### Added\n- thing\n\n## [1.0.0] - 2025-12-01\n- initial\n"
    sec = extract_changelog_section(keep_a_changelog, "1.1.0")
    assert sec.startswith("## [1.1.0]") and "- thing" in sec and "initial" not in sec
    vstyle = "## v2.1.0 — July 22, 2026\n\nStuff.\n\n## v2.0.0 — June 1, 2026\n\nOld.\n"
    sec = extract_changelog_section(vstyle, "2.1.0")
    assert "Stuff." in sec and "Old." not in sec
    rst = "Changelog\n=========\n\n3.0.0 (2026-01-01)\n------------------\n\n* breaking thing\n\n2.9.0 (2025-12-01)\n------------------\n\n* older\n"
    sec = extract_changelog_section(rst, "3.0.0")
    assert "breaking thing" in sec and "older" not in sec
    # a version that is a prefix of another is not confused
    assert "2.9.0" not in (extract_changelog_section(rst, "3.0.0") or "")


def test_version_helpers():
    assert is_prerelease("1.0.0rc1") and is_prerelease("5.0.0-beta.1") and is_prerelease("2.0.0a1")
    assert not is_prerelease("1.0.0") and not is_prerelease("2026.8.31")
    assert major_of("v2.1.0") == 2 and major_of("3.22.1") == 3 and major_of(None) is None
    assert sort_versions(["1.10.0", "1.2.0", "1.9.1", "weird"]) == [
        "1.2.0",
        "1.9.1",
        "1.10.0",
        "weird",
    ]
    assert github_repo("git+https://github.com/openai/openai-node.git") == ("openai", "openai-node")
    assert github_repo("https://github.com/stripe/stripe-python/tree/master") == (
        "stripe",
        "stripe-python",
    )
    assert github_repo("https://example.com/x") is None
    assert (
        normalise_repo_url({"url": "git+ssh://git@github.com/a/b.git"}) == "https://github.com/a/b"
    )
    assert normalise_repo_url("git://gitlab.com/a/b.git") == "https://gitlab.com/a/b"
    assert normalise_repo_url(None) is None
