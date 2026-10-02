"""Request bodies for the API (they mirror docs/CONTRACTS.md).

Everything the SDK, the CLI, the settings page and the operator pages send is validated here
so the routers only deal with typed data.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
MAX_TOOL_NAME = 2000
MAX_LATENCY_MS = 24 * 3600 * 1000


class SessionUser(BaseModel):
    """What the signed session cookie carries."""

    login: str
    id: int
    installation_ids: list[int] = Field(default_factory=list)

    @field_validator("installation_ids", mode="before")
    @classmethod
    def _ints(cls, v: Any) -> Any:
        if v is None:
            return []
        return [int(x) for x in v]


# ---- /v1/ingest ---------------------------------------------------------------------


class IngestEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool_name: str = Field(min_length=1, max_length=MAX_TOOL_NAME)
    kind: Literal["mcp", "http"] | None = None
    schema_fingerprint: str | None = Field(default=None, max_length=64)
    outcome: Literal["ok", "error", "schema_mismatch"]
    error_class: str | None = Field(default=None, max_length=255)
    latency_ms: int | None = None
    sample: dict[str, Any] | None = None
    observed_at: datetime | None = None

    @field_validator("latency_ms", mode="before")
    @classmethod
    def _latency(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            raise ValueError("latency_ms must be a number")
        return max(0, min(n, MAX_LATENCY_MS))

    @field_validator("tool_name", "error_class", "schema_fingerprint")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


class IngestBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repo: str = Field(min_length=3, max_length=255)
    sdk_version: str | None = Field(default=None, max_length=64)
    events: list[IngestEvent] = Field(default_factory=list)

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        v = v.strip()
        if "/" not in v:
            raise ValueError("repo must be owner/name")
        return v


# ---- /v1/watch/push ---------------------------------------------------------------


class WatchSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["mcp", "openapi"]
    locator: str = Field(min_length=1, max_length=4000)
    display_name: str | None = Field(default=None, max_length=255)
    vendor: str | None = Field(default=None, max_length=128)

    @field_validator("locator", "display_name", "vendor")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        return v or None


class WatchDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str | None = Field(default=None, max_length=255)
    body: dict[str, Any]


class WatchPushBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repo: str = Field(min_length=3, max_length=255)
    source: WatchSource
    doc: WatchDoc

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        v = v.strip()
        if "/" not in v:
            raise ValueError("repo must be owner/name")
        return v


# ---- settings API -----------------------------------------------------------------


def guess_mcp_transport(url: str) -> str:
    """``sse`` for a URL whose path ends in ``/sse``, else ``http`` (streamable HTTP); the
    same guess as ``kintsua.indexer.config.ConfigSource.mcp_transport``."""
    return "sse" if urlsplit(url).path.rstrip("/").endswith("/sse") else "http"


class AddSourceBody(BaseModel):
    """``POST /v1/repos/{id}/sources``: a spec / changelog URL or an MCP server.

    ``transport`` (MCP only): ``http`` | ``sse`` for a URL (guessed from the path when
    absent: ``.../sse`` is SSE), ``stdio`` for a command line; ``None`` for other kinds.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["openapi", "changelog", "mcp"]
    url: str | None = None
    locator: str | None = None
    command: str | None = None
    display_name: str | None = Field(default=None, max_length=255)
    hosts: list[str] = Field(default_factory=list)
    transport: Literal["http", "sse", "stdio"] | None = None

    @field_validator("hosts", mode="before")
    @classmethod
    def _hosts(cls, v: Any) -> Any:
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [h.strip() for h in v.split(",") if h.strip()]
        return v

    @field_validator("transport", mode="before")
    @classmethod
    def _transport(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip().lower().replace("_", "-")
            if not v:
                return None
            if v in ("streamable-http", "streamablehttp"):
                return "http"
        return v

    @model_validator(mode="after")
    def _resolve(self) -> AddSourceBody:
        raw = (self.url or self.locator or self.command or "").strip()
        if not raw:
            raise ValueError("a url (or an MCP command) is required")
        if self.kind == "mcp" and not raw.lower().startswith(("http://", "https://")):
            parts = shlex.split(raw)
            if not parts:
                raise ValueError("mcp command must not be empty")
            self.command = shlex.join(parts)
            self.url = None
        else:
            parts_url = urlsplit(raw)
            if parts_url.scheme not in ("http", "https") or not parts_url.netloc:
                raise ValueError(f"url must be an http(s) URL, got {raw!r}")
            self.url = raw
            self.command = None
        self.locator = self.url or self.command
        if self.kind != "mcp":
            self.transport = None
        elif self.url:
            if self.transport == "stdio":
                raise ValueError("transport stdio needs a command, not a url")
            self.transport = self.transport or guess_mcp_transport(self.url)
        else:
            if self.transport not in (None, "stdio"):
                raise ValueError(f"transport {self.transport} needs a url")
            self.transport = "stdio"
        self.hosts = [h.lower().strip() for h in self.hosts if h and h.strip()]
        if self.display_name is not None:
            self.display_name = self.display_name.strip() or None
        return self

    def config_entry(self) -> dict[str, Any]:
        """The ``sources[]`` entry stored on ``Repo.config`` (same shape as .kintsua.yml,
        including an MCP source's ``transport``)."""
        out: dict[str, Any] = {"kind": self.kind}
        if self.url:
            out["url"] = self.url
        if self.command:
            out["command"] = self.command
        if self.display_name:
            out["name"] = self.display_name
        if self.hosts:
            out["hosts"] = list(self.hosts)
        if self.transport:
            out["transport"] = self.transport
        return out


class MuteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pattern: str = Field(min_length=1, max_length=500)

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("pattern must not be empty")
        return v


class FixModeBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["pr", "issue_only"]


class OpenIssueBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    impact_id: str | None = None

    @field_validator("impact_id", mode="before")
    @classmethod
    def _blank(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class CreateKeyBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    org_id: str | None = None

    @field_validator("org_id", mode="before")
    @classmethod
    def _blank(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class CheckoutBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    org_id: str | None = None
    email: str | None = Field(default=None, max_length=320)

    @field_validator("org_id", "email", mode="before")
    @classmethod
    def _blank(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("not a valid email address")
        return v


class SubscribeBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("not a valid email address")
        return v


__all__ = [
    "AddSourceBody",
    "CheckoutBody",
    "CreateKeyBody",
    "FixModeBody",
    "IngestBatch",
    "IngestEvent",
    "MuteBody",
    "OpenIssueBody",
    "SessionUser",
    "SubscribeBody",
    "WatchDoc",
    "WatchPushBody",
    "WatchSource",
    "guess_mcp_transport",
]
