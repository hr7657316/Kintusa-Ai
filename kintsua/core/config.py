"""Process configuration.

Everything comes from the environment (or a local ``.env``). The open-source CLI and
the runtime SDK never import this module; only the hosted service does.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# Placeholder session secrets shipped in this repo (config default, docker-compose,
# .env.example). Any of them in prod means anyone can forge a session cookie.
DEFAULT_SESSION_SECRET = "dev-session-secret-change-me"
KNOWN_PLACEHOLDER_SECRETS = frozenset({DEFAULT_SESSION_SECRET, "change-me", "changeme", "secret"})
MIN_PROD_SESSION_SECRET_LEN = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- service -----------------------------------------------------------------
    env: Literal["dev", "test", "prod"] = "dev"
    base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    data_dir: Path = Field(default=Path(".kintsua-data"))

    # --- database ----------------------------------------------------------------
    database_url: str = "postgresql+psycopg://postgres:dev@localhost:5432/kintsua"
    db_pool_size: int = 5

    # --- GitHub App --------------------------------------------------------------
    github_app_id: str | None = None
    github_app_slug: str = "kintsua"
    github_app_private_key: str | None = None  # PEM text, or a path to a PEM file
    github_webhook_secret: str | None = None
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    github_api_url: str = "https://api.github.com"
    github_write_concurrency: int = 2  # per-org concurrent writes (secondary rate limits)

    # --- web sessions ------------------------------------------------------------
    # signs the session cookie, the only thing that scopes a browser session to its
    # installations; in prod it must be set to a random value of 32+ characters
    session_secret: str = DEFAULT_SESSION_SECRET
    session_max_age_s: int = 60 * 60 * 24 * 14

    # --- LLM ---------------------------------------------------------------------
    llm_provider: Literal["anthropic", "openai", "fake"] = "anthropic"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    llm_classifier_model: str = "claude-opus-5-5"
    llm_fixer_model: str = "claude-opus-5-5"
    llm_classifier_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_fixer_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    llm_confidence_threshold: float = 0.6

    # --- snapshot storage --------------------------------------------------------
    snapshot_inline_limit_bytes: int = 256 * 1024
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    # Without S3_BUCKET, large documents go to files under DATA_DIR, which only the
    # machine that wrote them can read. Prod refuses that unless every process shares
    # DATA_DIR (one machine, or one shared volume): set DATA_DIR_SHARED=true then.
    data_dir_shared: bool = False

    # --- watchers ----------------------------------------------------------------
    tick_interval_s: int = 15 * 60
    source_backoff_ceiling_s: int = 48 * 3600
    poll_http_timeout_s: float = 60.0
    # apply kintsua.core.netguard to every non-seed source fetch (SSRF protection)
    net_guard: bool = True
    user_agent: str = "kintsua-watcher/0.1 (+https://github.com/hr7657316/Kintusa-Ai)"

    # --- indexer -----------------------------------------------------------------
    index_budget_s: int = 10 * 60
    push_debounce_s: int = 5 * 60

    # --- fixer -------------------------------------------------------------------
    sandbox_backend: Literal["docker", "local"] = "docker"
    sandbox_image: str = "kintsua-sandbox:latest"
    sandbox_wall_clock_s: int = 15 * 60
    sandbox_memory: str = "4g"
    sandbox_cpus: float = 2.0
    sandbox_egress_allowlist: list[str] = Field(
        default_factory=lambda: [
            "pypi.org",
            "files.pythonhosted.org",
            "registry.npmjs.org",
            "api.github.com",
            "github.com",
            "codeload.github.com",
        ]
    )
    fix_concurrency: int = 3
    fix_defer_delay_s: int = 60  # re-queue delay when the concurrency cap is hit
    fix_test_timeout_s: int = 600
    sandbox_https_proxy: str | None = None  # a proxy makes the egress allowlist real
    fix_max_steps: int = 12
    fix_max_changed_lines: int = 300
    fix_import_radius: int = 1

    # --- classifier / matcher ----------------------------------------------------
    auto_issue: bool = False  # False = draft issues (status detected) until a human flips it
    runtime_promotion_threshold: int = 3
    runtime_promotion_window_s: int = 24 * 3600

    # --- billing -----------------------------------------------------------------
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_id: str | None = None
    pro_price_per_integration_usd: int = 29
    free_watched_integration_limit: int = 5

    # --- SDK ingest --------------------------------------------------------------
    ingest_max_batch: int = 500

    # --- job queue / worker --------------------------------------------------------
    # a 'running' job whose locked_at is older than this is treated as orphaned by a dead
    # worker and handed out again; running jobs heartbeat locked_at, so the lease only
    # needs to outlast a heartbeat gap, not the longest job
    job_lease_s: int = 15 * 60
    # after SIGTERM/SIGINT the worker lets the running job finish for this long, then
    # aborts it (the job goes straight back to the queue and its sandbox is removed);
    # keep it below the platform's kill timeout. 0 = wait for the job, however long
    worker_shutdown_grace_s: float = 3.0

    @model_validator(mode="after")
    def _refuse_unsafe_prod_config(self) -> Settings:
        secret = self.session_secret or ""
        weak = (
            secret in KNOWN_PLACEHOLDER_SECRETS
            or "change-me" in secret
            or len(secret) < MIN_PROD_SESSION_SECRET_LEN
        )
        if self.env == "prod" and weak:
            raise ValueError(
                "SESSION_SECRET must be set to a random value of at least "
                f"{MIN_PROD_SESSION_SECRET_LEN} characters in prod (the default would let "
                "anyone forge a session cookie)"
            )
        if self.env == "dev" and secret == DEFAULT_SESSION_SECRET:
            log.warning("SESSION_SECRET is the built-in default; fine for local dev only")
        return self

    @property
    def github_private_key_pem(self) -> str | None:
        v = self.github_app_private_key
        if not v:
            return None
        if v.startswith("-----BEGIN"):
            return v.replace("\\n", "\n")
        p = Path(v)
        if p.exists():
            return p.read_text()
        return v

    @property
    def github_configured(self) -> bool:
        return bool(self.github_app_id and self.github_private_key_pem)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Tests change the environment and need a fresh Settings object."""
    get_settings.cache_clear()


def is_test() -> bool:
    return os.environ.get("KINTSUA_ENV", os.environ.get("ENV", "")) == "test" or bool(
        os.environ.get("PYTEST_CURRENT_TEST")
    )
