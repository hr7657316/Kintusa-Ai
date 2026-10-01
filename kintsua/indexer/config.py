"""``.kintsua.yml`` parsing and the helpers other components use to read it.

Spec shape::

    sources:
      - kind: openapi
        url: https://api.vendor.com/openapi.json
      - kind: changelog
        url: https://vendor.com/changelog
    mute:
      - stripe:/v1/legacy/*
    fix:
      mode: pr          # pr | issue_only
      base: main

Extras accepted (optional): ``test_command`` (string, overrides test detection in the
fixer), ``fix.radius`` (import-hop radius the validator allows), ``sources[].host`` /
``sources[].hosts`` (API hosts a spec or changelog covers, so raw HTTP calls to that host
resolve to it) and ``sources[].kind: mcp`` with ``url`` or ``command``. Unknown keys are
ignored; bad values raise ``ValueError`` with a readable message.
"""

from __future__ import annotations

import shlex
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

FixMode = Literal["pr", "issue_only"]
DEFAULT_FIX_MODE: FixMode = "issue_only"


class ConfigSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["openapi", "changelog", "mcp"]
    url: str | None = None
    command: str | None = None  # mcp stdio servers only
    name: str | None = None
    host: str | None = None
    hosts: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _url_ok(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"url must be an http(s) URL, got {v!r}")
        return v

    @field_validator("hosts", mode="before")
    @classmethod
    def _hosts_list(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v

    @model_validator(mode="after")
    def _locator_present(self) -> ConfigSource:
        if self.kind == "mcp":
            if not self.url and not self.command:
                raise ValueError("an mcp source needs a url or a command")
            if self.command is not None and not shlex.split(self.command):
                raise ValueError("mcp command must not be empty")
        elif not self.url:
            raise ValueError(f"a {self.kind} source needs a url")
        self.hosts = [h.lower().strip() for h in ([self.host] if self.host else []) + self.hosts]
        self.hosts = [h for h in dict.fromkeys(self.hosts) if h]
        self.host = self.hosts[0] if self.hosts else None
        return self

    @property
    def locator(self) -> str:
        if self.kind == "mcp" and not self.url:
            return shlex.join(shlex.split(self.command or ""))
        return self.url or ""


class FixConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: FixMode | None = None
    base: str | None = None
    radius: int | None = Field(default=None, ge=0, le=5)

    @field_validator("base")
    @classmethod
    def _base_ok(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = str(v).strip()
        if not v or any(c.isspace() for c in v) or v.startswith("-"):
            raise ValueError(f"fix.base must be a branch name, got {v!r}")
        return v


class KintsuaConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sources: list[ConfigSource] = Field(default_factory=list)
    mute: list[str] = Field(default_factory=list)
    fix: FixConfig = Field(default_factory=FixConfig)
    test_command: str | None = None

    @field_validator("mute", mode="before")
    @classmethod
    def _mute_list(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if not isinstance(v, list):
            raise ValueError("mute must be a list of patterns")
        out = []
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"mute entries must be non-empty strings, got {item!r}")
            out.append(item.strip())
        return out

    @field_validator("sources", mode="before")
    @classmethod
    def _sources_list(cls, v: Any) -> Any:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("sources must be a list")
        return v

    @field_validator("fix", mode="before")
    @classmethod
    def _fix_dict(cls, v: Any) -> Any:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("fix must be a mapping with mode and/or base")
        return v

    @field_validator("test_command")
    @classmethod
    def _test_cmd(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = str(v).strip()
        return v or None

    def to_repo_config(self) -> dict[str, Any]:
        """The dict stored on ``Repo.config`` (keys other components read)."""
        out: dict[str, Any] = {
            "sources": [
                s.model_dump(exclude_none=True, exclude_defaults=True) for s in self.sources
            ],
            "mute": list(self.mute),
            "fix": self.fix.model_dump(exclude_none=True),
        }
        for s, dumped in zip(self.sources, out["sources"], strict=True):
            dumped["kind"] = s.kind
            if s.hosts:
                dumped["hosts"] = list(s.hosts)
        if self.test_command:
            out["test_command"] = self.test_command
        return out


def _readable(err: ValidationError) -> str:
    parts = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e.get("loc", ()) if x != "__root__")
        msg = e.get("msg", "invalid value")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def parse_kintsua_yml(text: str | None) -> dict[str, Any]:
    """Parse and validate ``.kintsua.yml``; returns the dict stored on ``Repo.config``.

    Empty / comment-only files yield the defaults. Raises ``ValueError`` with a readable
    message for malformed YAML, a non-mapping document, or bad field values.
    """
    if text is None or not text.strip():
        return KintsuaConfig().to_repo_config()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f".kintsua.yml is not valid YAML: {e}") from e
    if data is None:
        return KintsuaConfig().to_repo_config()
    if not isinstance(data, dict):
        raise ValueError(".kintsua.yml must be a mapping (sources / mute / fix)")
    return load_kintsua_config(data).to_repo_config()


def load_kintsua_config(data: dict[str, Any] | None) -> KintsuaConfig:
    """Validate an already-parsed mapping (the dict on ``Repo.config`` round-trips)."""
    try:
        return KintsuaConfig.model_validate(data or {})
    except ValidationError as e:
        raise ValueError(f".kintsua.yml: {_readable(e)}") from e


def effective_fix_mode(
    repo_config: dict[str, Any] | None, org_settings: dict[str, Any] | None
) -> FixMode:
    """Repo ``fix.mode`` if set, else the org default, else ``issue_only``."""
    fix = (repo_config or {}).get("fix") or {}
    mode = fix.get("mode") if isinstance(fix, dict) else None
    if mode in ("pr", "issue_only"):
        return mode  # type: ignore[return-value]
    org_mode = (org_settings or {}).get("fix_mode")
    if org_mode in ("pr", "issue_only"):
        return org_mode  # type: ignore[return-value]
    return DEFAULT_FIX_MODE


def fix_base_branch(repo_config: dict[str, Any] | None, default_branch: str) -> str:
    fix = (repo_config or {}).get("fix") or {}
    base = fix.get("base") if isinstance(fix, dict) else None
    return str(base) if base else default_branch


def mute_patterns(config: dict[str, Any] | None) -> list[str]:
    mute = (config or {}).get("mute") or []
    return [str(m) for m in mute if isinstance(m, str) and m.strip()]


def config_sources(config: dict[str, Any] | None) -> list[ConfigSource]:
    """The validated ``sources[]`` of a stored repo config (bad entries are skipped)."""
    out: list[ConfigSource] = []
    for raw in (config or {}).get("sources") or []:
        if not isinstance(raw, dict):
            continue
        try:
            out.append(ConfigSource.model_validate(raw))
        except ValidationError:
            continue
    return out


def test_command(config: dict[str, Any] | None) -> str | None:
    v = (config or {}).get("test_command")
    return str(v) if v else None
