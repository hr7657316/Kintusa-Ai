"""Package watchers: PyPI (``kind="pypi"``) and npm (``kind="npm"``).

Canonical body::

    {"name": "openai", "latest": "3.22.1",
     "versions": ["3.21.0", "3.22.0", "3.22.1", ...],      # stable only unless meta.prerelease
     "repository": "https://github.com/openai/openai-python",
     "requires_python": ">=3.10"  |  "engines": {"node": ">=22"},
     "deprecated": "<npm deprecation message>" | null,
     "release_notes": {"3.22.1": "<GitHub release body or CHANGELOG section, <= 20 KB>"}}

``diff`` is synchronous and must not touch the network, so release notes are fetched inside
``fetch`` (GitHub Releases for the tag ``v<ver>`` / ``<ver>`` via api.github.com, tolerating
403/404, else the ``<ver>`` section of ``CHANGELOG.md`` from the repo's default branch via
raw.githubusercontent.com) and stored in ``body["release_notes"]`` keyed by version;
``diff`` and ``event_text`` read them from there. Every body is read with a byte cap
(``meta["max_bytes"]``, 32 MB; 8 MB for release notes) and guarded sources
(``meta["_net_guard"]``) have every request checked by ``kintsua.core.netguard``.

Ops when ``latest`` changes: ``text_changed`` at ``/latest`` (old -> new), ``deprecated`` at
``/latest`` when the major changed (before = old major, after = new major), ``added`` at
``/versions/<v>`` for every new version (``removed`` for yanked ones), ``text_changed`` at
``/requires_python`` or ``/engines`` when the supported runtime changes, and ``deprecated``
at ``/deprecated`` when npm marks the latest version deprecated.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote

import httpx
from packaging.version import InvalidVersion, Version

from kintsua.core.events import CanonicalDoc, ChangeOp, json_pointer
from kintsua.watchers._http import (
    ResponseTooLarge,
    client_kwargs,
    get_bounded,
    guard_url,
    max_bytes_for,
)
from kintsua.watchers.base import BaseWatcher, SourceRef, register

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "kintsua-watcher/0.1 (+https://github.com/hr7657316/Kintusa-Ai)"
DEFAULT_TIMEOUT_S = 60.0
PYPI_URL = "https://pypi.org/pypi/{name}/json"
NPM_URL = "https://registry.npmjs.org/{name}"
GITHUB_API = "https://api.github.com"
RAW_GITHUB = "https://raw.githubusercontent.com"
CHANGELOG_FILES = ("CHANGELOG.md", "CHANGES.md", "HISTORY.md", "CHANGELOG.rst", "CHANGELOG")
RELEASE_NOTES_LIMIT = 20_000
RELEASE_FETCH_MAX_BYTES = 8 * 1024 * 1024
_GITHUB_RE = re.compile(
    r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?].*)?$"
)


# --------------------------------------------------------------------------------------
# Version helpers
# --------------------------------------------------------------------------------------


def parse_version(v: str) -> Version | None:
    try:
        return Version(v)
    except InvalidVersion:
        m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", v)
        if m:
            try:
                return Version(".".join(x for x in m.groups() if x))
            except InvalidVersion:
                return None
        return None


def is_prerelease(v: str) -> bool:
    pv = parse_version(v)
    if pv is None:
        return True
    if pv.is_prerelease or pv.is_devrelease:
        return True
    # npm style "5.0.0-beta.1" parses as a PEP 440 prerelease; "1.2.3-rc1" too. Anything
    # with a hyphen suffix that packaging normalised away is still a prerelease.
    return bool(re.search(r"\d-[0-9A-Za-z]", v))


def major_of(v: str | None) -> int | None:
    if not v:
        return None
    pv = parse_version(v)
    return pv.major if pv is not None else None


def sort_versions(versions: list[str]) -> list[str]:
    def key(v: str) -> tuple[int, Any]:
        pv = parse_version(v)
        return (0, pv) if pv is not None else (1, v)

    parsed = [v for v in versions if parse_version(v) is not None]
    unparsed = sorted(v for v in versions if parse_version(v) is None)
    return sorted(parsed, key=key) + unparsed


def github_repo(url: str | None) -> tuple[str, str] | None:
    if not url:
        return None
    m = _GITHUB_RE.search(str(url))
    if not m:
        return None
    return m.group(1), m.group(2)


def normalise_repo_url(url: Any) -> str | None:
    if isinstance(url, dict):
        url = url.get("url")
    if not url or not isinstance(url, str):
        return None
    gh = github_repo(url)
    if gh:
        return f"https://github.com/{gh[0]}/{gh[1]}"
    url = re.sub(r"^git\+", "", url)
    url = re.sub(r"\.git$", "", url)
    if url.startswith("git://"):
        url = "https://" + url[len("git://") :]
    return url if url.startswith("http") else None


# --------------------------------------------------------------------------------------
# Release notes
# --------------------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def extract_changelog_section(text: str, version: str) -> str | None:
    """The section of a Markdown/RST changelog that documents ``version``: from the heading
    that mentions it up to the next heading of the same or higher level (or the next
    version heading)."""
    lines = text.splitlines()
    ver_re = re.compile(r"(?<![\w.])v?" + re.escape(version) + r"(?![\w.])")
    version_line_re = re.compile(r"^(#{1,6}\s*)?\[?v?\d+\.\d+(\.\d+)?")
    start = None
    level = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or not ver_re.search(stripped):
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            start, level = i, len(m.group(1))
            break
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if version_line_re.match(stripped) or (nxt and set(nxt) <= set("=-~^") and len(nxt) >= 3):
            start, level = i, 0
            break
    if start is None:
        return None
    out: list[str] = [lines[start]]
    for line in lines[start + 1 :]:
        stripped = line.strip()
        m = _HEADING_RE.match(stripped)
        if m and (level == 0 or len(m.group(1)) <= level):
            break
        if level == 0 and version_line_re.match(stripped) and not stripped.startswith(("-", "*")):
            break
        out.append(line)
    section = "\n".join(out).strip()
    return section[:RELEASE_NOTES_LIMIT] or None


def _github_headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def fetch_release_notes(
    client: httpx.AsyncClient, repo_url: str | None, version: str, *, package: str | None = None
) -> str | None:
    """GitHub Releases (``v<ver>``, ``<ver>``, ``<pkg>@<ver>``) then CHANGELOG files."""
    gh = github_repo(repo_url)
    if not gh:
        return None
    owner, repo = gh
    tags = [f"v{version}", version]
    if package:
        tags.append(f"{package}@{version}")
    rate_limited = False
    for tag in tags:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/releases/tags/{quote(tag, safe='@')}"
        try:
            r = await get_bounded(
                client, url, max_bytes=RELEASE_FETCH_MAX_BYTES, headers=_github_headers()
            )
        except (httpx.HTTPError, ResponseTooLarge) as e:
            log.debug("release lookup failed %s: %s", url, e)
            break
        if r.status_code == 200:
            try:
                body = (r.json() or {}).get("body")
            except ValueError:
                body = None
            if body and str(body).strip():
                return str(body).strip()[:RELEASE_NOTES_LIMIT]
            break  # the release exists but has no notes: fall through to CHANGELOG
        if r.status_code in (403, 429):
            rate_limited = True
            break
        if r.status_code != 404:
            break
    if rate_limited:
        log.info("GitHub API rate limited; trying CHANGELOG for %s/%s", owner, repo)
    for fname in CHANGELOG_FILES:
        url = f"{RAW_GITHUB}/{owner}/{repo}/HEAD/{fname}"
        try:
            r = await get_bounded(client, url, max_bytes=RELEASE_FETCH_MAX_BYTES)
        except (httpx.HTTPError, ResponseTooLarge) as e:
            log.debug("changelog fetch failed %s: %s", url, e)
            continue
        if r.status_code != 200 or not r.text:
            continue
        section = extract_changelog_section(r.text, version)
        if section:
            return section
    return None


# --------------------------------------------------------------------------------------
# Shared diff
# --------------------------------------------------------------------------------------


class _RegistryWatcher(BaseWatcher):
    kind = ""
    runtime_key = ""  # "requires_python" | "engines"

    def _client(self, meta: dict[str, Any]) -> httpx.AsyncClient:
        headers = {
            "User-Agent": str(meta.get("user_agent") or DEFAULT_USER_AGENT),
            "Accept": "application/json",
        }
        for k, v in (meta.get("headers") or {}).items():
            headers[str(k)] = os.path.expandvars(str(v))
        timeout = float(meta.get("timeout_s") or DEFAULT_TIMEOUT_S)
        # guarded sources: every request (registry, GitHub release, CHANGELOG) and every
        # redirect hop goes through kintsua.core.netguard
        return httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            headers=headers,
            **client_kwargs(meta),
        )

    def diff(self, before: CanonicalDoc, after: CanonicalDoc) -> list[ChangeOp]:
        b, a = before.body, after.body
        ops: list[ChangeOp] = []
        b_latest, a_latest = b.get("latest"), a.get("latest")
        if b_latest != a_latest:
            ops.append(ChangeOp(op="text_changed", path="/latest", before=b_latest, after=a_latest))
            b_major, a_major = major_of(b_latest), major_of(a_latest)
            if b_major is not None and a_major is not None and b_major != a_major:
                ops.append(
                    ChangeOp(
                        op="deprecated", path="/latest", before=str(b_major), after=str(a_major)
                    )
                )
        b_versions, a_versions = set(b.get("versions") or []), set(a.get("versions") or [])
        for v in sort_versions(list(a_versions - b_versions)):
            ops.append(ChangeOp(op="added", path=json_pointer("versions", v), before=None, after=v))
        for v in sort_versions(list(b_versions - a_versions)):
            ops.append(
                ChangeOp(op="removed", path=json_pointer("versions", v), before=v, after=None)
            )
        rk = self.runtime_key
        if rk and (b.get(rk) or None) != (a.get(rk) or None):
            ops.append(
                ChangeOp(op="text_changed", path="/" + rk, before=b.get(rk), after=a.get(rk))
            )
        if a.get("deprecated") and not b.get("deprecated"):
            ops.append(
                ChangeOp(
                    op="deprecated", path="/deprecated", before=None, after=a.get("deprecated")
                )
            )
        return ops

    def event_text(self, before: CanonicalDoc, after: CanonicalDoc) -> str | None:
        """Release notes for the new ``latest`` (fetched during ``fetch``)."""
        notes = after.body.get("release_notes") or {}
        latest = after.body.get("latest")
        text = notes.get(latest) if latest else None
        if not text:
            new = [v for v in after.body.get("versions") or [] if v in notes]
            text = "\n\n".join(notes[v] for v in new) if new else None
        if not text:
            return None
        return f"{after.body.get('name')} {before.body.get('latest')} -> {latest}\n\n{text}"[
            :RELEASE_NOTES_LIMIT
        ]


@register
class PyPIWatcher(_RegistryWatcher):
    kind = "pypi"
    runtime_key = "requires_python"

    async def fetch(self, source: SourceRef) -> CanonicalDoc:
        meta = source.meta or {}
        name = source.locator.strip()
        url = PYPI_URL.format(name=quote(name, safe=""))
        guard_url(url, meta)
        async with self._client(meta) as client:
            r = await get_bounded(client, url, max_bytes=max_bytes_for(meta))
            r.raise_for_status()
            data = r.json()
            info = data.get("info") or {}
            releases = data.get("releases") or {}
            versions = [
                v
                for v, files in releases.items()
                if not (files and all(f.get("yanked") for f in files if isinstance(f, dict)))
            ]
            if not meta.get("prerelease"):
                versions = [v for v in versions if not is_prerelease(v)]
            versions = sort_versions(versions)
            latest = str(info.get("version") or (versions[-1] if versions else ""))
            if not meta.get("prerelease") and is_prerelease(latest) and versions:
                latest = versions[-1]
            repo = _pypi_repo_url(info)
            body: dict[str, Any] = {
                "name": info.get("name") or name,
                "latest": latest,
                "versions": versions,
                "repository": repo,
                "requires_python": info.get("requires_python") or None,
                "summary": info.get("summary") or None,
                "release_notes": {},
            }
            if latest and repo and not meta.get("skip_release_notes"):
                notes = await fetch_release_notes(client, repo, latest, package=name)
                if notes:
                    body["release_notes"][latest] = notes
        return CanonicalDoc(version=latest or None, body=body)


def _pypi_repo_url(info: dict[str, Any]) -> str | None:
    urls = info.get("project_urls") or {}
    preferred = ("Repository", "Source", "Source Code", "Code", "GitHub", "Homepage", "Home")
    for key in preferred:
        for k, v in urls.items():
            if str(k).strip().lower() == key.lower() and github_repo(v):
                return normalise_repo_url(v)
    for v in urls.values():
        if github_repo(v):
            return normalise_repo_url(v)
    for cand in (info.get("home_page"), info.get("project_url")):
        if github_repo(cand):
            return normalise_repo_url(cand)
    for key in preferred:
        for k, v in urls.items():
            if (
                str(k).strip().lower() == key.lower()
                and isinstance(v, str)
                and v.startswith("http")
            ):
                return v
    return None


@register
class NpmWatcher(_RegistryWatcher):
    kind = "npm"
    runtime_key = "engines"

    async def fetch(self, source: SourceRef) -> CanonicalDoc:
        meta = source.meta or {}
        name = source.locator.strip()
        url = NPM_URL.format(name=quote(name, safe="@"))
        guard_url(url, meta)
        async with self._client(meta) as client:
            r = await get_bounded(client, url, max_bytes=max_bytes_for(meta))
            r.raise_for_status()
            data = r.json()
            dist_tags = data.get("dist-tags") or {}
            all_versions = data.get("versions") or {}
            versions = list(all_versions)
            if not meta.get("prerelease"):
                versions = [v for v in versions if not is_prerelease(v)]
            versions = sort_versions(versions)
            latest = str(dist_tags.get("latest") or (versions[-1] if versions else ""))
            manifest = all_versions.get(latest) or {}
            repo = normalise_repo_url(data.get("repository") or manifest.get("repository"))
            body: dict[str, Any] = {
                "name": data.get("name") or name,
                "latest": latest,
                "versions": versions,
                "repository": repo,
                "engines": manifest.get("engines") or None,
                "deprecated": manifest.get("deprecated") or None,
                "summary": data.get("description") or None,
                "release_notes": {},
            }
            if latest and repo and not meta.get("skip_release_notes"):
                notes = await fetch_release_notes(client, repo, latest, package=name)
                if notes:
                    body["release_notes"][latest] = notes
        return CanonicalDoc(version=latest or None, body=body)
