"""Regression tests for the classifier review fixes (server side: ``classify/run.py``).

* classify-correctness-1: a large diff is stored truncated (a 200-op preview plus the full
  document behind ``storage_ref`` / ``raw_diff_ref``); the classifier must read the full
  document, and must not call a partial diff ``safe`` when it is gone.
* classify-correctness-4: the consumer operations shown to the model follow the matcher's
  join (``dep.meta.openapi_source_id`` / ``changelog_source_id``).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from kintsua.classify.rules import classify_by_rules
from kintsua.classify.run import (
    RULE_DIFF_TRUNCATED,
    StoredDiffUnavailable,
    build_change_event,
    classify_change_event,
    consumer_operations_for,
    stored_diff_ref,
)
from kintsua.core.events import ChangeEvent, ChangeOp
from kintsua.core.models import CallSite, ChangeEventRow, Dependency, Org, Repo
from kintsua.core.storage import put_document, ref_to_string
from tests.classify.test_run import make_event, make_source, match_jobs

PREVIEW = 200
CAPTURE = "/paths/~1v1~1charges~1{id}~1capture/post"


def big_event(source_id: uuid.UUID, *, n_noise: int = 260) -> ChangeEvent:
    """``n_noise`` removed descriptions (wording only), then the one removed operation:
    breaking in full, but past the first 200 ops (even after ranking removals first)."""
    ops = [
        ChangeOp(
            op="removed",
            path=f"/paths/~1v1~1things{i}/get/description",
            before=f"Thing {i}. " + "x" * 1200,
            after=None,
        )
        for i in range(n_noise)
    ]
    ops.append(ChangeOp(op="removed", path=CAPTURE, before={"summary": "Capture"}, after=None))
    return ChangeEvent(source_id=source_id, source_kind="openapi", ops=ops)


def truncated_row(session, source, event: ChangeEvent, *, legacy: bool = False) -> ChangeEventRow:
    """What the watcher writes for a large diff: the full dump in storage, the row with
    the first 200 ops. ``legacy`` rows carry only the ``raw_diff_ref`` string."""
    dump = event.model_dump(mode="json")
    ref = put_document(dump, prefix="diffs")
    assert ref["kind"] == "file"
    diff = dict(dump)
    diff["raw_diff_ref"] = ref_to_string(ref)
    diff["ops"] = dump["ops"][:PREVIEW]
    diff["ops_truncated"] = True
    diff["ops_total"] = len(dump["ops"])
    if not legacy:
        diff["storage_ref"] = ref
    row = ChangeEventRow(source_id=source.id, diff=diff)
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def data_dir(settings, monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


# ---- classify-correctness-1 --------------------------------------------------------------


def test_stored_diff_ref_from_dict_or_string() -> None:
    assert stored_diff_ref({"storage_ref": {"kind": "file", "path": "/d/x.json"}}) == {
        "kind": "file",
        "path": "/d/x.json",
    }
    assert stored_diff_ref({"raw_diff_ref": "file:///d/diffs/x.json"}) == {
        "kind": "file",
        "path": "/d/diffs/x.json",
    }
    assert stored_diff_ref({"raw_diff_ref": "s3://bucket/diffs/a/b.json"}) == {
        "kind": "s3",
        "bucket": "bucket",
        "key": "diffs/a/b.json",
    }
    for raw in ("inline", "", "s3://bucket", "file://", None):
        assert stored_diff_ref({"raw_diff_ref": raw}) is None


@pytest.mark.parametrize("legacy", [False, True], ids=["storage_ref", "raw_diff_ref_only"])
def test_truncated_row_is_rebuilt_from_the_full_stored_diff(session, data_dir, legacy) -> None:
    source = make_source(session)
    full = big_event(source.id)
    row = truncated_row(session, source, full, legacy=legacy)
    preview = ChangeEvent.model_validate({**row.diff, "source_id": source.id})
    assert classify_by_rules(preview) is None  # the preview alone looks wording-only

    event = build_change_event(row, source)
    assert len(event.ops) == len(full.ops)
    assert event.ops[-1].path == CAPTURE
    r = classify_by_rules(event)
    assert r is not None and (r.severity, r.category) == ("breaking", "removal")
    assert "POST /v1/charges/{id}/capture" in r.affected_paths


def test_truncated_row_written_by_the_watcher_round_trips(session, data_dir) -> None:
    from kintsua.watchers.run import _store_diff

    source = make_source(session)
    full = big_event(source.id)
    diff = _store_diff(full.model_copy(deep=True))
    assert diff.get("ops_truncated") is True and len(diff["ops"]) < len(full.ops)
    row = ChangeEventRow(source_id=source.id, diff=diff)
    session.add(row)
    session.flush()
    event = build_change_event(row, source)
    assert [o.path for o in event.ops] == [o.path for o in full.ops]


def test_classify_change_event_uses_the_full_diff(session, data_dir, fake_llm) -> None:
    source = make_source(session)
    row = truncated_row(session, source, big_event(source.id))
    session.commit()
    out = classify_change_event(session, row)
    assert (out.severity, out.category) == ("breaking", "removal")
    assert RULE_DIFF_TRUNCATED not in out.rule_hits
    assert fake_llm.calls == []  # the rules decided on the full diff
    assert len(match_jobs(session, row.id)) == 1


def test_missing_stored_diff_is_not_classified_from_the_preview(
    session, data_dir, fake_llm
) -> None:
    source = make_source(session)
    row = truncated_row(session, source, big_event(source.id))
    Path(row.diff["storage_ref"]["path"]).unlink()
    session.commit()
    with pytest.raises(StoredDiffUnavailable):
        build_change_event(row, source)
    assert len(build_change_event(row, source, load_stored=False).ops) == PREVIEW
    fake_llm.queue_json(
        {
            "severity": "safe",
            "category": "docs_only",
            "summary": "Descriptions were removed",
            "affected_paths": [],
            "migration_hint": None,
            "confidence": 0.95,
        }
    )
    out = classify_change_event(session, row)
    assert out.severity == "unknown" and out.needs_human is True
    assert RULE_DIFF_TRUNCATED in out.rule_hits
    assert "full diff unavailable" in out.summary


def test_missing_stored_diff_keeps_a_breaking_preview_verdict(session, data_dir, fake_llm) -> None:
    source = make_source(session)
    ops = [ChangeOp(op="removed", path=CAPTURE, before={"summary": "Capture"})] + [
        ChangeOp(op="added", path=f"/paths/~1v1~1things{i}", after={"get": {"x": "y" * 1200}})
        for i in range(260)
    ]
    row = truncated_row(
        session, source, ChangeEvent(source_id=source.id, source_kind="openapi", ops=ops)
    )
    Path(row.diff["storage_ref"]["path"]).unlink()
    session.commit()
    out = classify_change_event(session, row)
    assert (out.severity, out.category) == ("breaking", "removal")
    assert out.needs_human is False and RULE_DIFF_TRUNCATED in out.rule_hits


def test_transient_storage_error_propagates_so_the_job_retries(
    session, data_dir, fake_llm, monkeypatch
) -> None:
    import kintsua.classify.run as run_mod

    source = make_source(session)
    row = truncated_row(session, source, big_event(source.id))
    session.commit()

    def flaky(ref):
        raise ConnectionError("S3 is down")

    monkeypatch.setattr(run_mod, "get_document", flaky)
    with pytest.raises(ConnectionError):
        classify_change_event(session, row)


# ---- classify-correctness-4 --------------------------------------------------------------


def _sdk_consumer(session, registry_source, operations, **meta) -> None:
    org = Org(github_installation_id=uuid.uuid4().int % 10**9, name="acme")
    session.add(org)
    session.flush()
    repo = Repo(
        org_id=org.id, github_repo_id=uuid.uuid4().int % 10**9, full_name="acme/support-agent"
    )
    session.add(repo)
    session.flush()
    dep = Dependency(
        repo_id=repo.id,
        source_id=registry_source.id,
        kind="pypi",
        name=registry_source.locator,
        meta={k: str(v) for k, v in meta.items()},
    )
    session.add(dep)
    session.flush()
    for i, op in enumerate(operations):
        session.add(CallSite(dependency_id=dep.id, file_path="agent.py", line=10 + i, operation=op))
    session.flush()


def test_changelog_event_prompt_lists_sdk_consumers_linked_through_meta(session, fake_llm) -> None:
    changelog = make_source(
        session,
        kind="changelog",
        locator="https://developers.notion.com/page/changelog",
        display_name="Notion changelog",
    )
    pypi = make_source(session, kind="pypi", locator="notion-client", display_name="notion-client")
    _sdk_consumer(session, pypi, ["databases.query"], changelog_source_id=changelog.id)
    row = make_event(
        session,
        changelog,
        [{"op": "text_changed", "path": "/paragraphs/3", "before": None, "after": "t"}],
        text="databases.query filter `text` property removed on Nov 1",
    )
    session.commit()
    fake_llm.queue_json(
        {
            "severity": "breaking",
            "category": "removal",
            "summary": "databases.query drops the text filter",
            "affected_paths": ["databases.query"],
            "migration_hint": None,
            "confidence": 0.9,
        }
    )
    classify_change_event(session, row)
    user = fake_llm.calls[-1]["user"]
    assert "- databases.query" in user
    assert "no known consumers" not in user
    assert "acme" not in user and "support-agent" not in user


def test_consumer_operations_follow_openapi_and_changelog_links(session) -> None:
    spec = make_source(session)
    changelog = make_source(session, kind="changelog", locator="https://x/changelog")
    other = make_source(session, kind="openapi", locator="https://other/openapi.json")
    pypi = make_source(session, kind="pypi", locator="stripe", display_name="stripe")
    _sdk_consumer(session, pypi, ["stripe.Charge.create"], openapi_source_id=spec.id)
    _sdk_consumer(session, pypi, ["stripe.Refund.create"], changelog_source_id=changelog.id)
    assert consumer_operations_for(session, spec.id) == ["stripe.Charge.create"]
    assert consumer_operations_for(session, changelog.id) == ["stripe.Refund.create"]
    assert consumer_operations_for(session, pypi.id) == [
        "stripe.Charge.create",
        "stripe.Refund.create",
    ]
    assert consumer_operations_for(session, other.id) == []
