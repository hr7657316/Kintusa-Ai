"""Server side of the classifier: ``classify_event`` job handler and the daily batch.

Flow per change event row:

1. Build the ``ChangeEvent`` from ``row.diff`` (plus the text that travelled with it).
2. Rules first (``classify_by_rules``).
3. The model runs when the rules could not decide, for every ``changelog`` event, and
   for ``pypi``/``npm`` events that carry release notes. When both ran, the rules'
   verdict is kept unless the model *escalates* (``breaking``, or ``degraded`` over
   ``safe``) with confidence at or above ``Settings.llm_confidence_threshold``; the model
   never lowers a rule severity. Both outcomes are recorded in ``rule_hits``
   (``"llm"``, ``"llm_promoted"``, ``"llm_low_confidence"``).
4. Write the ``classification`` row (``needs_human`` when the final severity is
   ``unknown``), commit, enqueue ``match_event`` with dedupe key ``match_event:<id>``.

Consumer operations shown to the model are the distinct ``call_site.operation`` values
of dependencies on the same source: operation / tool names only, never repo names.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kintsua.classify.llm_classify import (
    RULE_LLM,
    RULE_LLM_LOW_CONFIDENCE,
    classify_with_llm,
    needs_human_review,
)
from kintsua.classify.rules import classify_by_rules
from kintsua.classify.schema import Classification
from kintsua.core.config import get_settings
from kintsua.core.events import ChangeEvent
from kintsua.core.llm import LLMRefused, LLMUsage, get_llm
from kintsua.core.models import CallSite, ChangeEventRow, Dependency, Source
from kintsua.core.models import Classification as ClassificationRow
from kintsua.core.queue import (
    JOB_CLASSIFY_EVENT,
    JOB_MATCH_EVENT,
    NonRetryable,
    enqueue,
    job_handler,
)
from kintsua.core.storage import get_document

log = logging.getLogger(__name__)

RULE_LLM_PROMOTED = "llm_promoted"
RULE_LLM_REFUSED = "llm_refused"
RULE_LLM_INVALID = "llm_invalid_output"
SEVERITY_RANK = {"breaking": 0, "degraded": 1, "safe": 2, "unknown": 3}
MAX_CONSUMER_OPERATIONS = 100


# --------------------------------------------------------------------------------------
# Building blocks (each testable on its own)
# --------------------------------------------------------------------------------------


def build_change_event(row: ChangeEventRow, source: Source | None = None) -> ChangeEvent:
    """``row.diff`` is the dumped ``ChangeEvent``; tolerate a missing ``source_id`` /
    ``source_kind`` and a diff whose ops live behind a storage ref."""
    data: dict[str, Any] = dict(row.diff or {})
    data["source_id"] = row.source_id
    if not data.get("source_kind") and source is not None:
        data["source_kind"] = source.kind
    ops = data.get("ops")
    if not ops and isinstance(data.get("storage_ref"), dict):
        try:
            stored = get_document(data["storage_ref"])
        except Exception as e:  # noqa: BLE001
            log.warning("change_event %s: could not load stored diff: %s", row.id, e)
            stored = None
        if isinstance(stored, dict) and isinstance(stored.get("ops"), list):
            data["ops"] = stored["ops"]
            data.setdefault("text", stored.get("text"))
    data.pop("storage_ref", None)
    if data.get("ops") is None:
        data["ops"] = []
    return ChangeEvent.model_validate(data)


def display_name_for(source: Source | None) -> str | None:
    if source is None:
        return None
    if source.kind in ("pypi", "npm"):
        return source.locator or source.display_name or None
    return source.display_name or source.vendor or None


def consumer_operations_for(
    session: Session, source_id: UUID, limit: int = MAX_CONSUMER_OPERATIONS
) -> list[str]:
    """Distinct operations consumers call on this source, anonymised (no repo names)."""
    rows = session.execute(
        select(CallSite.operation)
        .join(Dependency, Dependency.id == CallSite.dependency_id)
        .where(Dependency.source_id == source_id)
        .distinct()
        .order_by(CallSite.operation)
        .limit(limit)
    ).scalars()
    return [op for op in rows if op]


def should_call_model(event: ChangeEvent, rules_result: Classification | None) -> bool:
    if rules_result is None:
        return True
    if event.source_kind == "changelog":
        return True
    return bool(event.source_kind in ("pypi", "npm") and (event.text or "").strip())


def merge_rules_and_model(
    rules_result: Classification | None,
    llm_result: Classification | None,
    *,
    threshold: float,
) -> Classification:
    """Combine the two verdicts. Rules win unless the model escalates with confidence."""
    if rules_result is None and llm_result is None:
        raise ValueError("nothing to merge")
    if rules_result is None:
        assert llm_result is not None
        return llm_result
    if llm_result is None:
        return rules_result
    confidence = llm_result.llm_confidence if llm_result.llm_confidence is not None else 0.0
    rule_hits = list(rules_result.rule_hits) + [
        h for h in llm_result.rule_hits if h not in rules_result.rule_hits
    ]
    affected = list(
        dict.fromkeys(list(rules_result.affected_paths) + list(llm_result.affected_paths))
    )
    escalates = (
        llm_result.severity in ("breaking", "degraded")
        and SEVERITY_RANK[llm_result.severity] < SEVERITY_RANK[rules_result.severity]
        and confidence >= threshold
    )
    if escalates:
        rule_hits.append(RULE_LLM_PROMOTED)
        return Classification(
            severity=llm_result.severity,
            category=llm_result.category,
            summary=llm_result.summary,
            affected_paths=affected,
            migration_hint=llm_result.migration_hint or rules_result.migration_hint,
            rule_hits=rule_hits,
            llm_confidence=llm_result.llm_confidence,
        )
    return Classification(
        severity=rules_result.severity,
        category=rules_result.category,
        summary=rules_result.summary,
        affected_paths=affected,
        migration_hint=rules_result.migration_hint or llm_result.migration_hint,
        rule_hits=rule_hits,
        llm_confidence=llm_result.llm_confidence,
    )


def _fallback_unknown(event: ChangeEvent, reason_rule: str, detail: str) -> Classification:
    return Classification(
        severity="unknown",
        category="behaviour",
        summary=f"{event.source_kind} change could not be classified by the model; needs review",
        affected_paths=[],
        migration_hint=None,
        rule_hits=[RULE_LLM, reason_rule],
        llm_confidence=None,
    )


def _enqueue_match(session: Session, change_event_id: UUID) -> None:
    enqueue(
        session,
        JOB_MATCH_EVENT,
        {"change_event_id": str(change_event_id)},
        dedupe_key=f"match_event:{change_event_id}",
    )


# --------------------------------------------------------------------------------------
# The seam: classify_change_event
# --------------------------------------------------------------------------------------


def classify_change_event(session: Session, change_event_row: ChangeEventRow) -> ClassificationRow:
    """Classify one change event row, write the ``classification`` row, commit and enqueue
    ``match_event``. Idempotent: an existing classification is returned (and
    ``match_event`` re-enqueued, deduped) without calling the model again."""
    row = change_event_row
    existing = session.scalar(
        select(ClassificationRow).where(ClassificationRow.change_event_id == row.id)
    )
    if existing is not None:
        _enqueue_match(session, row.id)
        session.commit()
        return existing

    source = session.get(Source, row.source_id)
    event = build_change_event(row, source)
    display_name = display_name_for(source)
    settings = get_settings()

    rules_result = classify_by_rules(event, display_name=display_name)
    llm_result: Classification | None = None
    usage: LLMUsage | None = None
    if should_call_model(event, rules_result):
        consumers = consumer_operations_for(session, row.source_id)
        try:
            llm_result, usage = classify_with_llm(
                event,
                consumer_operations=consumers,
                display_name=display_name,
                llm=get_llm(),
                rules_result=rules_result,
            )
        except LLMRefused as e:
            log.warning("change_event %s: model refused: %s", row.id, e)
            llm_result = _fallback_unknown(event, RULE_LLM_REFUSED, str(e))
        except ValidationError as e:
            log.warning("change_event %s: model output invalid: %s", row.id, e)
            llm_result = _fallback_unknown(event, RULE_LLM_INVALID, str(e))
        # any other error (network, provider outage) propagates so the job retries

    final = merge_rules_and_model(
        rules_result, llm_result, threshold=settings.llm_confidence_threshold
    )
    needs_human = needs_human_review(final)
    if needs_human and RULE_LLM_LOW_CONFIDENCE not in final.rule_hits and llm_result is None:
        # rules and model both silent (should not happen: the model always answers)
        log.warning("change_event %s: unknown without a model answer", row.id)

    out = ClassificationRow(
        change_event_id=row.id,
        severity=final.severity,
        category=final.category,
        summary=final.summary[:1000],
        affected_paths=list(final.affected_paths),
        migration_hint=final.migration_hint,
        rule_hits=list(final.rule_hits),
        llm_model=usage.model if usage else None,
        llm_confidence=final.llm_confidence,
        prompt_hash=usage.prompt_hash if usage else None,
        needs_human=needs_human,
    )
    try:
        with session.begin_nested():
            session.add(out)
            session.flush()
    except IntegrityError:
        # lost a race with a concurrent classify of the same event: use the winner
        winner = session.scalar(
            select(ClassificationRow).where(ClassificationRow.change_event_id == row.id)
        )
        if winner is None:
            raise
        out = winner
    _enqueue_match(session, row.id)
    session.commit()
    log.info(
        "classified change_event=%s source=%s severity=%s category=%s rules=%s needs_human=%s",
        row.id,
        display_name or row.source_id,
        out.severity,
        out.category,
        ",".join(out.rule_hits),
        out.needs_human,
    )
    return out


@job_handler(JOB_CLASSIFY_EVENT)
def handle_classify_event(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("change_event_id")
    if not raw:
        raise NonRetryable("classify_event: payload has no change_event_id")
    try:
        change_event_id = UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        raise NonRetryable(f"classify_event: bad change_event_id {raw!r}")
    row = session.get(ChangeEventRow, change_event_id)
    if row is None:
        raise NonRetryable(f"classify_event: change_event {change_event_id} does not exist")
    out = classify_change_event(session, row)
    return {
        "classification_id": str(out.id),
        "severity": out.severity,
        "category": out.category,
        "needs_human": out.needs_human,
    }


# --------------------------------------------------------------------------------------
# Daily batch (``python -m kintsua.classify.run``)
# --------------------------------------------------------------------------------------


def unclassified_change_events(session: Session, limit: int = 200) -> list[ChangeEventRow]:
    return list(
        session.scalars(
            select(ChangeEventRow)
            .outerjoin(ClassificationRow, ClassificationRow.change_event_id == ChangeEventRow.id)
            .where(ClassificationRow.id.is_(None))
            .order_by(ChangeEventRow.detected_at)
            .limit(limit)
        )
    )


def classify_batch(session: Session, limit: int = 200) -> list[ClassificationRow]:
    """Classify every change event that has no classification yet (oldest first). A
    failure on one event is logged and does not stop the batch."""
    out: list[ClassificationRow] = []
    for row in unclassified_change_events(session, limit):
        try:
            out.append(classify_change_event(session, row))
        except Exception:  # noqa: BLE001
            session.rollback()
            log.exception("classify_batch: change_event %s failed", row.id)
    return out


def main(argv: list[str] | None = None) -> int:
    from kintsua.core.db import session_scope
    from kintsua.log import setup_logging

    parser = argparse.ArgumentParser(prog="kintsua-classify")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)
    setup_logging()
    with session_scope() as session:
        rows = classify_batch(session, args.limit)
    log.info("classified %s change events", len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
