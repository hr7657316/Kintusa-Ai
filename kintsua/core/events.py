"""The normalised change event shared by every watcher.

Pure Pydantic; no database or network imports so the open-source CLI can use it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

SourceKind = Literal["openapi", "mcp", "pypi", "npm", "changelog", "observed"]

OpKind = Literal[
    "added",
    "removed",
    "renamed",
    "type_changed",
    "required_added",
    "required_removed",
    "enum_changed",
    "auth_changed",
    "deprecated",
    "text_changed",
]


class ChangeOp(BaseModel):
    """One atomic change inside a diff."""

    op: OpKind
    path: str  # JSON pointer, e.g. /paths/~1v1~1charges/post/parameters/3 or /tools/get_weather/inputSchema
    before: Any | None = None
    after: Any | None = None


class ChangeEvent(BaseModel):
    source_id: UUID
    source_kind: SourceKind
    from_version: str | None = None  # semver, spec version, or snapshot hash
    to_version: str | None = None
    ops: list[ChangeOp] = Field(default_factory=list)  # one per atomic change
    raw_diff_ref: str = ""  # storage pointer to the full diff
    # Optional free text that travels with the event (release notes, changelog paragraph,
    # tool description). Never trusted as instructions; always treated as data.
    text: str | None = None


class CanonicalDoc(BaseModel):
    """What a watcher's ``fetch`` returns: normalised, key-sorted, no volatile fields."""

    version: str | None = None
    body: dict[str, Any]

    def content_hash(self) -> str:
        return canonical_hash(self.body)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def json_pointer_escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def json_pointer(*tokens: str | int) -> str:
    return "/" + "/".join(json_pointer_escape(str(t)) for t in tokens)


def json_pointer_unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def split_pointer(pointer: str) -> list[str]:
    if not pointer or pointer == "/":
        return []
    return [json_pointer_unescape(t) for t in pointer.lstrip("/").split("/")]
