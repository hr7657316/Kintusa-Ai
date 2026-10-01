"""GitHub App plumbing: installation tokens (cached until expiry), per-installation
clients, per-org write semaphores, and webhook signature verification.

Everything Kintsua writes to GitHub goes through ``installation_client`` and the
``write_lock`` so secondary rate limits are respected (2 concurrent writes per org).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from kintsua.core.config import get_settings

log = logging.getLogger(__name__)

LABEL = "kintsua"
LABEL_BREAKING = "severity:breaking"
LABEL_DEGRADED = "severity:degraded"
ISSUE_PREFIX = "[Kintsua]"
BRANCH_PREFIX = "kintsua/"


@dataclass
class InstallationToken:
    token: str
    expires_at: float  # epoch seconds


class GitHubApp:
    """Thin wrapper over PyGithub's ``GithubIntegration`` with token caching."""

    def __init__(
        self, app_id: str | None = None, private_key: str | None = None, base_url: str | None = None
    ):
        s = get_settings()
        self.app_id = app_id or s.github_app_id
        self.private_key = private_key or s.github_private_key_pem
        self.base_url = base_url or s.github_api_url
        self._tokens: dict[int, InstallationToken] = {}
        self._semaphores: dict[int, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()
        self._integration = None

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.private_key)

    def _integration_client(self):
        if self._integration is None:
            from github import Auth, GithubIntegration

            if not self.configured:
                raise RuntimeError(
                    "GitHub App is not configured (GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY)"
                )
            self._integration = GithubIntegration(
                auth=Auth.AppAuth(int(self.app_id), self.private_key), base_url=self.base_url
            )
        return self._integration

    def app_jwt(self) -> str:
        from github import Auth

        return Auth.AppAuth(int(self.app_id), self.private_key).create_jwt()

    def installation_token(self, installation_id: int) -> str:
        """Installation access token, cached until ~2 minutes before expiry."""
        with self._lock:
            tok = self._tokens.get(installation_id)
            if tok and tok.expires_at - 120 > time.time():
                return tok.token
        auth = self._integration_client().get_access_token(installation_id)
        expires = auth.expires_at.timestamp() if auth.expires_at else time.time() + 3300
        with self._lock:
            self._tokens[installation_id] = InstallationToken(token=auth.token, expires_at=expires)
        return auth.token

    def installation_client(self, installation_id: int):
        """A ``github.Github`` authenticated as the installation."""
        from github import Auth, Github

        return Github(
            auth=Auth.Token(self.installation_token(installation_id)),
            base_url=self.base_url,
            per_page=100,
        )

    def clone_url(self, installation_id: int, full_name: str) -> str:
        tok = self.installation_token(installation_id)
        host = (
            "github.com"
            if "api.github.com" in self.base_url
            else self.base_url.split("//", 1)[-1].split("/")[0]
        )
        return f"https://x-access-token:{tok}@{host}/{full_name}.git"

    def write_lock(self, installation_id: int) -> threading.BoundedSemaphore:
        """Per-org semaphore: at most N concurrent writes (default 2)."""
        with self._lock:
            sem = self._semaphores.get(installation_id)
            if sem is None:
                sem = threading.BoundedSemaphore(get_settings().github_write_concurrency)
                self._semaphores[installation_id] = sem
            return sem

    def list_installations(self) -> list[dict[str, Any]]:
        out = []
        for inst in self._integration_client().get_installations():
            out.append({"id": inst.id, "account": inst.raw_data.get("account", {}).get("login")})
        return out


def verify_webhook_signature(body: bytes, signature_header: str | None, secret: str | None) -> bool:
    """``X-Hub-Signature-256: sha256=<hex>``."""
    if not secret:
        log.warning("GITHUB_WEBHOOK_SECRET not set; refusing webhook")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1])


_app: GitHubApp | None = None
_app_lock = threading.Lock()


def get_github_app() -> GitHubApp:
    global _app
    with _app_lock:
        if _app is None:
            _app = GitHubApp()
        return _app


def set_github_app(app: GitHubApp | None) -> None:
    global _app
    with _app_lock:
        _app = app


def is_protected_branch(repo, branch: str) -> bool:
    """True if the branch has protection rules. Kintsua never pushes to a protected branch."""
    try:
        return bool(repo.get_branch(branch).protected)
    except Exception:  # noqa: BLE001
        return False
