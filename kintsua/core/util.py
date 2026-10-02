"""Small shared helpers (no heavy imports)."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def short_hash(data: str | bytes, n: int = 8) -> str:
    return sha256_hex(data)[:n]


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def parse_duration(s: str) -> timedelta:
    """'7d', '12h', '30m', '45s', '2w' -> timedelta."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", s)
    if not m:
        raise ValueError(f"bad duration: {s!r}")
    n, unit = int(m.group(1)), m.group(2)
    key = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[unit]
    return timedelta(**{key: n})


SECRET_PATTERNS = [
    re.compile(r"(?i)(sk|rk|pk)_(live|test)_[0-9a-zA-Z]{8,}"),
    re.compile(r"(?i)bearer\s+[a-z0-9\-_\.=]{12,}"),
    # an Authorization / Proxy-Authorization header with any scheme (Basic, Token, ...)
    re.compile(
        r"(?i)\b(?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?[a-z][a-z0-9_\-]*\s+[^\s\"',}]{6,}"
    ),
    re.compile(r"(?i)gh[pousr]_[0-9a-zA-Z]{20,}"),
    re.compile(r"github_pat_[0-9A-Za-z_]{22,}"),
    re.compile(r"(?i)xox[abeprs]-[0-9a-zA-Z\-]{10,}"),
    re.compile(r"(?i)xapp-[0-9a-zA-Z\-]{10,}"),
    re.compile(r"(?i)AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"(?i)sk-ant-[0-9a-zA-Z\-_]{20,}"),
    re.compile(r"(?i)sk-(?:proj|svcacct|admin)-[0-9a-zA-Z\-_]{20,}"),
    re.compile(r"(?i)sk-[0-9a-zA-Z]{20,}"),
    re.compile(r"kts_[0-9A-Za-z_\-]{20,}"),  # Kintsua SDK keys
    re.compile(r"npm_[0-9A-Za-z]{36}"),
    re.compile(r"pypi-[0-9A-Za-z_\-]{50,}"),
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password|authorization|client_secret|private_key)"
        r"[\"']?\s*[:=]\s*(bearer\s+)?[\"']?[^\s\"',}]{6,}"
    ),
    re.compile(r"eyJ[0-9a-zA-Z_\-]{10,}\.[0-9a-zA-Z_\-]{10,}\.[0-9a-zA-Z_\-]{10,}"),
]
# credentials in a URL's userinfo (https://user:password@host/...); the scheme and host stay
URL_USERINFO_PATTERN = re.compile(r"(?i)(?<=://)[^/\s:@]+:[^/\s@]+@")


def redact_secrets(text: str) -> str:
    out = URL_USERINFO_PATTERN.sub("[REDACTED]@", text)
    for pat in SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out
