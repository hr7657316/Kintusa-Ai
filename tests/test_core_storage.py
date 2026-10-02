"""Large-document storage (core-correctness-3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kintsua.core.storage import (
    StorageNotConfigured,
    get_document,
    put_document,
    storage_problem,
)


def _big(settings) -> dict:
    return {"blob": "x" * (settings.snapshot_inline_limit_bytes + 10)}


def test_prod_without_s3_refuses_a_machine_local_file(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "s3_bucket", None)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert storage_problem(settings) is not None
    with pytest.raises(StorageNotConfigured, match="S3_BUCKET"):
        put_document(_big(settings))
    assert not any(tmp_path.rglob("*.json"))
    # small documents stay inline and need no storage at all
    assert put_document({"a": 1}) == {"kind": "inline", "body": {"a": 1}}


def test_prod_with_a_shared_data_dir_may_use_files(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "s3_bucket", None)
    monkeypatch.setattr(settings, "data_dir_shared", True)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert storage_problem(settings) is None
    ref = put_document(_big(settings))
    assert ref["kind"] == "file"


def test_file_refs_are_absolute(settings, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "data_dir", Path("rel-data"))
    body = _big(settings)
    ref = put_document(body)
    assert ref["kind"] == "file" and Path(ref["path"]).is_absolute()
    monkeypatch.chdir("/")  # another process started from another directory
    assert get_document(ref) == body
