"""Server-side classifier: ``classify_change_event``, the job handler and the batch.

These need Postgres (``session``) and the scripted model (``fake_llm``).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from kintsua.classify.run import (
    RULE_LLM_PROMOTED,
    build_change_event,
    classify_batch,
    classify_change_event,
    consumer_operations_for,
    handle_classify_event,
    merge_rules_and_model,
    should_call_model,
)
from kintsua.classify.schema import Classification
from kintsua.core.models import (
    CallSite,
    ChangeEventRow,
    Dependency,
    Job,
    Org,
    Repo,
    Source,
)
from kintsua.core.models import Classification as ClassificationRow
from kintsua.core.queue import HANDLERS, NonRetryable, drain

CHARGES_BODY = (
    "/paths/~1v1~1charges/post/requestBody/content/application~1x-www-form-urlencoded/schema"
)


def make_source(
    session,
    kind="openapi",
    locator="https://api.stripe.com/openapi.json",
    display_name="Stripe API",
):
    s = Source(kind=kind, locator=locator, display_name=display_name, vendor="stripe")
    session.add(s)
    session.flush()
    return s


def make_event(session, source, ops, *, text=None, from_version=None, to_version=None):
    row = ChangeEventRow(
        source_id=source.id,
        diff={
            "source_kind": source.kind,
            "from_version": from_version,
            "to_version": to_version,
            "ops": ops,
            "raw_diff_ref": "inline",
            "text": text,
        },
    )
    session.add(row)
    session.flush()
    return row


def make_consumer(session, source, operations, full_name="acme/support-agent"):
    org = Org(github_installation_id=uuid.uuid4().int % 10**9, name=full_name.split("/")[0])
    session.add(org)
    session.flush()
    repo = Repo(org_id=org.id, github_repo_id=uuid.uuid4().int % 10**9, full_name=full_name)
    session.add(repo)
    session.flush()
    dep = Dependency(repo_id=repo.id, source_id=source.id, kind=source.kind, name="stripe")
    session.add(dep)
    session.flush()
    for i, op in enumerate(operations):
        session.add(
            CallSite(
                dependency_id=dep.id, file_path=f"{full_name}/agent.py", line=10 + i, operation=op
            )
        )
    session.flush()
    return repo


def match_jobs(session, event_id):
    return list(
        session.scalars(
            select(Job).where(
                Job.kind == "match_event", Job.dedupe_key == f"match_event:{event_id}"
            )
        )
    )


RULES_ONLY_OPS = [
    {
        "op": "required_added",
        "path": f"{CHARGES_BODY}/required",
        "before": ["amount"],
        "after": ["amount", "currency"],
    }
]


# ---- pure helpers ------------------------------------------------------------------------


def test_should_call_model_matrix():
    from kintsua.core.events import ChangeEvent

    sid = uuid.uuid4()
    rules = Classification(severity="safe", category="behaviour", summary="s")
    assert should_call_model(ChangeEvent(source_id=sid, source_kind="openapi"), None)
    assert not should_call_model(ChangeEvent(source_id=sid, source_kind="openapi"), rules)
    assert should_call_model(ChangeEvent(source_id=sid, source_kind="changelog"), rules)
    assert should_call_model(ChangeEvent(source_id=sid, source_kind="pypi", text="notes"), rules)
    assert not should_call_model(ChangeEvent(source_id=sid, source_kind="pypi", text="  "), rules)
    assert not should_call_model(ChangeEvent(source_id=sid, source_kind="npm"), rules)


def test_merge_keeps_rules_unless_model_escalates_with_confidence():
    rules = Classification(
        severity="degraded",
        category="deprecation",
        summary="stripe 11.x -> 12.0.0: major version bump",
        affected_paths=["package:stripe"],
        migration_hint="Upgrade",
        rule_hits=["major_bump"],
    )
    weak = Classification(
        severity="breaking",
        category="removal",
        summary="model",
        affected_paths=["stripe.Charge.create"],
        rule_hits=["llm"],
        llm_confidence=0.4,
    )
    m = merge_rules_and_model(rules, weak, threshold=0.6)
    assert m.severity == "degraded" and m.category == "deprecation" and m.summary == rules.summary
    assert m.rule_hits == ["major_bump", "llm"]
    assert m.affected_paths == ["package:stripe", "stripe.Charge.create"]
    assert m.llm_confidence == 0.4

    strong = Classification(
        severity="breaking",
        category="removal",
        summary="Client.connect() no longer accepts a URL",
        rule_hits=["llm"],
        llm_confidence=0.9,
        migration_hint="Pass a Transport",
    )
    m = merge_rules_and_model(rules, strong, threshold=0.6)
    assert m.severity == "breaking" and m.category == "removal" and m.summary == strong.summary
    assert m.rule_hits == ["major_bump", "llm", RULE_LLM_PROMOTED]
    assert m.migration_hint == "Pass a Transport"

    # the model never lowers a rule verdict
    lower = Classification(
        severity="safe",
        category="docs_only",
        summary="nothing",
        rule_hits=["llm"],
        llm_confidence=0.99,
    )
    m = merge_rules_and_model(rules, lower, threshold=0.6)
    assert m.severity == "degraded" and RULE_LLM_PROMOTED not in m.rule_hits

    # safe -> degraded escalation with confidence is allowed
    safe_rules = Classification(
        severity="safe",
        category="behaviour",
        summary="openai 1.51.0 -> 1.52.0: new release",
        rule_hits=["version_bump"],
    )
    degraded = Classification(
        severity="degraded",
        category="deprecation",
        summary="deprecates x",
        rule_hits=["llm"],
        llm_confidence=0.8,
    )
    assert merge_rules_and_model(safe_rules, degraded, threshold=0.6).severity == "degraded"

    assert merge_rules_and_model(None, strong, threshold=0.6) is strong
    assert merge_rules_and_model(rules, None, threshold=0.6) is rules
    with pytest.raises(ValueError):
        merge_rules_and_model(None, None, threshold=0.6)


# ---- DB paths ----------------------------------------------------------------------------


def test_rules_path_writes_row_and_enqueues_match_event(session, fake_llm):
    source = make_source(session)
    row = make_event(
        session, source, RULES_ONLY_OPS, from_version="2024-06-20", to_version="2024-09-30"
    )
    session.commit()

    out = classify_change_event(session, row)

    assert out.change_event_id == row.id
    assert out.severity == "breaking" and out.category == "required_param"
    assert out.summary == "[Stripe API] POST /v1/charges: required parameter 'currency' was added"
    assert out.affected_paths == ["POST /v1/charges"]
    assert out.rule_hits == ["required_added"]
    assert out.llm_model is None and out.llm_confidence is None and out.prompt_hash is None
    assert out.needs_human is False
    assert fake_llm.calls == []  # rules decided; the model was not called
    jobs = match_jobs(session, row.id)
    assert len(jobs) == 1 and jobs[0].payload == {"change_event_id": str(row.id)}
    # committed: visible from a fresh session
    from kintsua.core.db import new_session

    s2 = new_session()
    try:
        assert (
            s2.scalar(select(ClassificationRow).where(ClassificationRow.change_event_id == row.id))
            is not None
        )
    finally:
        s2.close()


def test_idempotent_second_call_returns_existing_and_reenqueues(session, fake_llm):
    source = make_source(session)
    row = make_event(session, source, RULES_ONLY_OPS)
    session.commit()
    first = classify_change_event(session, row)
    # simulate the match job having run
    for j in match_jobs(session, row.id):
        j.status = "done"
    session.commit()
    second = classify_change_event(session, row)
    assert second.id == first.id
    assert (
        session.scalar(
            select(ClassificationRow)
            .where(ClassificationRow.change_event_id == row.id)
            .with_only_columns(ClassificationRow.id)
        )
        == first.id
    )
    assert len(session.scalars(select(ClassificationRow)).all()) == 1
    queued = [j for j in match_jobs(session, row.id) if j.status == "queued"]
    assert len(queued) == 1
    # and a third call while that one is still queued does not add another
    classify_change_event(session, row)
    assert len([j for j in match_jobs(session, row.id) if j.status == "queued"]) == 1


def test_text_only_event_goes_to_model_with_anonymised_consumers(session, fake_llm):
    source = make_source(
        session, kind="mcp", locator="https://mcp.weather.example/mcp", display_name="Weather MCP"
    )
    make_consumer(session, source, ["get_weather", "get_forecast"], full_name="acme/support-agent")
    make_consumer(session, source, ["get_weather"], full_name="globex/ops-bot")
    other = make_source(
        session, kind="mcp", locator="https://mcp.other.example/mcp", display_name="Other MCP"
    )
    make_consumer(session, other, ["unrelated_tool"], full_name="initech/other")
    row = make_event(
        session,
        source,
        [
            {
                "op": "text_changed",
                "path": "/tools/get_weather/description",
                "before": "Current conditions.",
                "after": "Current conditions; now rate limited to 10/min.",
            }
        ],
        text="Current conditions; now rate limited to 10/min.",
    )
    session.commit()
    fake_llm.queue_json(
        {
            "severity": "degraded",
            "category": "behaviour",
            "summary": "get_weather is now rate limited to 10 calls per minute",
            "affected_paths": ["tool:get_weather"],
            "migration_hint": "Add backoff around get_weather",
            "confidence": 0.82,
        }
    )

    out = classify_change_event(session, row)

    assert out.severity == "degraded" and out.category == "behaviour"
    assert out.rule_hits == ["llm"]
    assert out.llm_model == "fake" and out.llm_confidence == 0.82 and out.prompt_hash
    assert out.needs_human is False
    assert out.affected_paths == ["tool:get_weather"]
    user = fake_llm.calls[-1]["user"]
    assert "- get_forecast" in user and "- get_weather" in user
    assert "unrelated_tool" not in user
    for secret in ("acme", "support-agent", "globex", "ops-bot", "agent.py"):
        assert secret not in user
    assert "rate limited" in user
    assert "rate limited" not in fake_llm.calls[-1]["system"]
    assert consumer_operations_for(session, source.id) == ["get_forecast", "get_weather"]
    assert len(match_jobs(session, row.id)) == 1


def test_low_confidence_marks_needs_human(session, fake_llm, settings):
    source = make_source(
        session,
        kind="changelog",
        locator="https://developers.notion.com/page/changelog",
        display_name="Notion changelog",
    )
    row = make_event(
        session,
        source,
        [
            {
                "op": "text_changed",
                "path": "/paragraphs/1",
                "before": None,
                "after": "We changed something.",
            }
        ],
        text="We changed something.",
    )
    session.commit()
    fake_llm.queue_json(
        {
            "severity": "breaking",
            "category": "behaviour",
            "summary": "maybe",
            "affected_paths": [],
            "migration_hint": None,
            "confidence": settings.llm_confidence_threshold - 0.1,
        }
    )
    out = classify_change_event(session, row)
    assert out.severity == "unknown"
    assert out.needs_human is True
    assert out.rule_hits == ["llm", "llm_low_confidence"]
    assert len(match_jobs(session, row.id)) == 1


def test_package_event_runs_rules_and_model_and_keeps_rule_severity(session, fake_llm):
    source = make_source(
        session, kind="npm", locator="@modelcontextprotocol/sdk", display_name="MCP TypeScript SDK"
    )
    row = make_event(
        session,
        source,
        [  # the package watcher's shape: full versions, then the bare majors
            {"op": "text_changed", "path": "/latest", "before": "1.17.5", "after": "2.0.0"},
            {"op": "deprecated", "path": "/latest", "before": "1", "after": "2"},
        ],
        from_version="1.17.5",
        to_version="2.0.0",
        text="## 2.0.0\n- internal refactor, no API changes",
    )
    session.commit()
    fake_llm.queue_json(
        {
            "severity": "safe",
            "category": "behaviour",
            "summary": "internal refactor",
            "affected_paths": [],
            "migration_hint": None,
            "confidence": 0.9,
        }
    )
    out = classify_change_event(session, row)
    assert out.severity == "degraded" and out.category == "deprecation"
    assert out.summary == "@modelcontextprotocol/sdk 1.x -> 2.0.0: major version bump"
    assert out.rule_hits == ["major_bump", "llm"]
    assert out.llm_confidence == 0.9 and out.llm_model == "fake"
    assert out.needs_human is False
    assert "<rules_result>" in fake_llm.calls[-1]["user"]
    assert "display_name: @modelcontextprotocol/sdk" in fake_llm.calls[-1]["user"]


def test_package_event_promoted_to_breaking_by_confident_model(session, fake_llm):
    source = make_source(session, kind="pypi", locator="stripe", display_name="stripe")
    make_consumer(session, source, ["stripe.Charge.create", "stripe.Customer.list"])
    row = make_event(
        session,
        source,
        [{"op": "added", "path": "/versions/12.0.0", "after": {}}],
        from_version="11.6.0",
        to_version="12.0.0",
        text="## 12.0.0\n### Breaking\n- `stripe.Charge.create` now requires `payment_method_types`",
    )
    session.commit()
    fake_llm.queue_json(
        {
            "severity": "breaking",
            "category": "required_param",
            "summary": "stripe 12.0.0: Charge.create now requires payment_method_types",
            "affected_paths": ["stripe.Charge.create"],
            "migration_hint": "Pass payment_method_types=['card']",
            "confidence": 0.88,
        }
    )
    out = classify_change_event(session, row)
    assert out.severity == "breaking" and out.category == "required_param"
    assert out.summary.startswith("stripe 12.0.0")
    assert out.rule_hits == ["major_bump", "llm", "llm_promoted"]
    assert out.affected_paths == ["package:stripe", "stripe.Charge.create"]
    assert out.migration_hint == "Pass payment_method_types=['card']"


def test_minor_package_release_without_notes_skips_model(session, fake_llm):
    source = make_source(session, kind="pypi", locator="openai", display_name="openai")
    row = make_event(
        session,
        source,
        [{"op": "added", "path": "/versions/1.52.0", "after": {}}],
        from_version="1.51.0",
        to_version="1.52.0",
    )
    session.commit()
    out = classify_change_event(session, row)
    assert out.severity == "safe" and out.rule_hits == ["version_bump"]
    assert fake_llm.calls == []


def test_model_refusal_falls_back_to_needs_human(session, fake_llm, monkeypatch):
    from kintsua.core.llm import LLMRefused

    source = make_source(
        session, kind="changelog", locator="https://x/changelog", display_name="X changelog"
    )
    row = make_event(
        session,
        source,
        [{"op": "text_changed", "path": "/paragraphs/1", "before": None, "after": "t"}],
        text="t",
    )
    session.commit()

    def refuse(*a, **k):
        raise LLMRefused("policy")

    monkeypatch.setattr(fake_llm, "complete_json", refuse)
    out = classify_change_event(session, row)
    assert out.severity == "unknown" and out.needs_human is True
    assert out.rule_hits == ["llm", "llm_refused"]


def test_provider_outage_propagates_so_the_job_retries(session, fake_llm, monkeypatch):
    source = make_source(
        session, kind="changelog", locator="https://y/changelog", display_name="Y changelog"
    )
    row = make_event(
        session,
        source,
        [{"op": "text_changed", "path": "/paragraphs/1", "before": None, "after": "t"}],
        text="t",
    )
    session.commit()

    def boom(*a, **k):
        raise ConnectionError("provider down")

    monkeypatch.setattr(fake_llm, "complete_json", boom)
    with pytest.raises(ConnectionError):
        classify_change_event(session, row)
    session.rollback()
    assert (
        session.scalar(select(ClassificationRow).where(ClassificationRow.change_event_id == row.id))
        is None
    )


def test_build_change_event_tolerates_sparse_diff(session):
    source = make_source(session)
    row = ChangeEventRow(
        source_id=source.id, diff={"ops": [{"op": "removed", "path": "/paths/~1v1~1orders"}]}
    )
    session.add(row)
    session.flush()
    ev = build_change_event(row, source)
    assert ev.source_kind == "openapi" and ev.source_id == source.id
    assert ev.ops[0].op == "removed" and ev.ops[0].before is None
    empty = ChangeEventRow(source_id=source.id, diff={})
    assert build_change_event(empty, source).ops == []


# ---- handler -----------------------------------------------------------------------------


def test_handler_registered_and_runs_through_queue(session, db, fake_llm):
    from kintsua.core.db import new_session
    from kintsua.core.queue import enqueue

    assert HANDLERS["classify_event"] is handle_classify_event
    source = make_source(session)
    row = make_event(session, source, RULES_ONLY_OPS)
    enqueue(session, "classify_event", {"change_event_id": str(row.id)})
    session.commit()
    assert drain(new_session, ["classify_event"]) == 1
    s2 = new_session()
    try:
        c = s2.scalar(select(ClassificationRow).where(ClassificationRow.change_event_id == row.id))
        assert c is not None and c.severity == "breaking"
        jobs = s2.scalars(select(Job).where(Job.kind == "match_event")).all()
        assert len(jobs) == 1 and jobs[0].status == "queued"
        assert s2.scalars(select(Job).where(Job.kind == "classify_event")).one().status == "done"
    finally:
        s2.close()


def test_handler_nonretryable_on_missing_or_bad_payload(session, fake_llm):
    with pytest.raises(NonRetryable):
        handle_classify_event(session, {})
    with pytest.raises(NonRetryable):
        handle_classify_event(session, {"change_event_id": "not-a-uuid"})
    with pytest.raises(NonRetryable):
        handle_classify_event(session, {"change_event_id": str(uuid.uuid4())})


def test_handler_with_existing_classification_only_reenqueues(session, fake_llm):
    source = make_source(session)
    row = make_event(session, source, RULES_ONLY_OPS)
    session.commit()
    first = handle_classify_event(session, {"change_event_id": str(row.id)})
    for j in match_jobs(session, row.id):
        j.status = "done"
    session.commit()
    second = handle_classify_event(session, {"change_event_id": str(row.id)})
    assert first["classification_id"] == second["classification_id"]
    assert len([j for j in match_jobs(session, row.id) if j.status == "queued"]) == 1


# ---- batch -------------------------------------------------------------------------------


def test_classify_batch_processes_unclassified_oldest_first(session, fake_llm):
    source = make_source(session)
    rows = [make_event(session, source, RULES_ONLY_OPS) for _ in range(3)]
    already = make_event(session, source, RULES_ONLY_OPS)
    session.commit()
    classify_change_event(session, already)
    out = classify_batch(session, limit=10)
    assert {c.change_event_id for c in out} == {r.id for r in rows}
    assert len(session.scalars(select(ClassificationRow)).all()) == 4
    assert classify_batch(session, limit=10) == []
    # one match job per event, deduped
    assert len(session.scalars(select(Job).where(Job.kind == "match_event")).all()) == 4


def test_classify_batch_continues_after_a_failure(session, fake_llm, monkeypatch):
    source = make_source(session, kind="changelog", locator="https://z/changelog", display_name="Z")
    bad = make_event(
        session,
        source,
        [{"op": "text_changed", "path": "/paragraphs/1", "before": None, "after": "t"}],
        text="t",
    )
    good = make_event(session, make_source(session), RULES_ONLY_OPS)
    session.commit()

    def boom(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr(fake_llm, "complete_json", boom)
    out = classify_batch(session, limit=10)
    assert [c.change_event_id for c in out] == [good.id]
    assert (
        session.scalar(select(ClassificationRow).where(ClassificationRow.change_event_id == bad.id))
        is None
    )
