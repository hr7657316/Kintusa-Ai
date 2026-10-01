"""Snapshot and diff storage: inline JSONB when small, S3-compatible (or a local data
directory) when large.

A ``storage_ref`` is a JSON object: ``{"kind": "inline", "body": ...}``,
``{"kind": "s3", "bucket": ..., "key": ...}`` or ``{"kind": "file", "path": ...}``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kintsua.core.config import get_settings
from kintsua.core.events import canonical_hash, canonical_json

log = logging.getLogger(__name__)


def put_document(body: Any, *, prefix: str = "snapshots") -> dict[str, Any]:
    """Store ``body`` and return a storage_ref."""
    s = get_settings()
    payload = canonical_json(body)
    raw = payload.encode("utf-8")
    if len(raw) < s.snapshot_inline_limit_bytes:
        return {"kind": "inline", "body": body}
    key = f"{prefix}/{canonical_hash(body)}.json"
    if s.s3_bucket:
        _s3_client().put_object(
            Bucket=s.s3_bucket, Key=key, Body=raw, ContentType="application/json"
        )
        return {"kind": "s3", "bucket": s.s3_bucket, "key": key}
    path = Path(s.data_dir) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"kind": "file", "path": str(path)}


def get_document(ref: dict[str, Any] | None) -> Any:
    if not ref:
        return None
    kind = ref.get("kind")
    if kind == "inline":
        return ref.get("body")
    if kind == "s3":
        obj = _s3_client().get_object(Bucket=ref["bucket"], Key=ref["key"])
        return json.loads(obj["Body"].read().decode("utf-8"))
    if kind == "file":
        return json.loads(Path(ref["path"]).read_text("utf-8"))
    raise ValueError(f"unknown storage ref kind: {kind!r}")


def ref_to_string(ref: dict[str, Any]) -> str:
    """A compact pointer string for ChangeEvent.raw_diff_ref."""
    kind = ref.get("kind")
    if kind == "s3":
        return f"s3://{ref['bucket']}/{ref['key']}"
    if kind == "file":
        return f"file://{ref['path']}"
    return "inline"


def _s3_client():  # pragma: no cover - needs S3
    import boto3

    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key_id,
        aws_secret_access_key=s.s3_secret_access_key,
    )
