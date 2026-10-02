"""Server side of the classifier: ``classify_event`` job handler and the daily batch.

Flow per change event row:

1. Build the ``ChangeEvent`` from ``row.diff`` (plus the text that travelled with it).
   A large diff is stored truncated (``ops_truncated``) with the full document behind
   ``storage_ref`` / ``raw_diff_ref``; the full ops are loaded first. When that document
   is gone for good the preview is classified, and anything short of ``breaking`` is
   forced to ``unknown`` with the ``diff_truncated`` rule hit (a human looks).
2. Rules first (``classify_by_rules``).
3. The model runs when the rules could not decide, for every ``changelog`` event, and
   for ``pypi``/``npm`` events that carry release notes. When both ran, the rules'
   verdict is kept unless the model *escalates* (``breaking``, or ``degraded`` over
   ``safe``) with confidence at or above ``Settings.llm_confidence_threshold``; the model
   never lowers a rule severity. Both outcomes are recorded in ``rule_hits``
   (``"llm"``, ``"llm_promoted"``, ``"llm_low_confidence"``). A model answer cut off at
   ``max_tokens`` (``LLMTruncated``) is retried once with ``TRUNCATED_RETRY_MAX_TOKENS``
   at one effort step lower; truncated again, the model's verdict is ``unknown`` with the
   ``"llm_truncated"`` rule hit (a refusal: ``"llm_refused"``; invalid output:
   ``"llm_invalid_output"``).
4. Write the ``classification`` row (``needs_human`` when the final severity is
   ``unknown``), commit, enqueue ``match_event`` with dedupe key ``match_event:<id>``.

Consumer operations shown to the model are the distinct ``call_site.operation`` values
of dependencies on the same source (directly, or through ``meta.openapi_source_id`` /
``meta.changelog_source_id`` like the matcher's join): operation / tool names only,
never repo names.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import or_, select
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
from kintsua.core.llm import LLMRefused, LLMTruncated, LLMUsage, get_llm
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
RULE_LLM_TRUNCATED = "llm_truncated"
#: the single retry after a truncated answer: a bigger output budget (still small enough
#: for a plain, non-streaming request) at one effort step lower, so less of it is thinking
TRUNCATED_RETRY_MAX_TOKENS = 20000
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
RULE_DIFF_TRUNCATED = "diff_truncated"
SEVERITY_RANK = {"breaking": 0, "degraded": 1, "safe": 2, "unknown": 3}
MAX_CONSUMER_OPERATIONS = 100
MAX_SUMMARY_CHARS = 1000
MAX_HINT_CHARS = 1000


# --------------------------------------------------------------------------------------
# Building blocks (each testable on its own)
# --------------------------------------------------------------------------------------


class StoredDiffUnavailable(RuntimeError):
    """The full diff behind a truncated ``change_event`` row is gone for good (missing
    file or object, unreadable document). Transient storage errors are not this: they
    propagate so the ``classify_event`` job retries."""


def stored_diff_ref(diff: dict[str, Any]) -> dict[str, Any] | None:
    """Where the full diff of a large change event lives. The watcher writes
    ``storage_ref`` (a storage_ref dict) next to ``raw_diff_ref`` (its string form,
    ``file://<path>`` or ``s3://<bucket>/<key>``); older rows carry only the string."""
    ref = diff.get("storage_ref")
    if isinstance(ref, dict) and ref.get("kind") in ("inline", "file", "s3"):
        return ref
    raw = diff.get("raw_diff_ref")
    if isinstance(raw, str):
        if raw.startswith("file://") and len(raw) > len("file://"):
            return {"kind": "file", "path": raw[len("file://") :]}
        if raw.startswith("s3://"):
            bucket, _, key = raw[len("s3://") :].partition("/")
            if bucket and key:
                return {"kind": "s3", "bucket": bucket, "key": key}
    return None


def _storage_error_is_permanent(e: Exception) -> bool:
    if isinstance(e, FileNotFoundError | NotADirectoryError | IsADirectoryError):
        return True
    if isinstance(e, ValueError | KeyError | TypeError):  # bad JSON, bad ref, bad shape
        return True
    resp = getattr(e, "response", None)  # botocore ClientError
    error = resp.get("Error") if isinstance(resp, dict) else None
    code = str(error.get("Code", "")) if isinstance(error, dict) else ""
    return code in ("NoSuchKey", "NoSuchBucket", "404")


def _load_stored_ops(row: ChangeEventRow, data: dict[str, Any]) -> None:
    """Replace the 200-op preview of a truncated row with the full stored diff."""
    ref = stored_diff_ref(data)
    if ref is None:
        raise StoredDiffUnavailable(f"change_event {row.id}: truncated diff without a storage ref")
    try:
        stored = get_document(ref)
    except Exception as e:
        if not _storage_error_is_permanent(e):
            raise  # transient (S3 outage, I/O error): the job retries
        raise StoredDiffUnavailable(f"change_event {row.id}: stored diff unreadable: {e}") from e
    if not isinstance(stored, dict) or not isinstance(stored.get("ops"), list):
        raise StoredDiffUnavailable(f"change_event {row.id}: stored diff has no ops list")
    data["ops"] = stored["ops"]
    if not data.get("text") and stored.get("text"):
        data["text"] = stored["text"]


def build_change_event(
    row: ChangeEventRow, source: Source | None = None, *, load_stored: bool = True
) -> ChangeEvent:
    """``row.diff`` is the dumped ``ChangeEvent``; tolerate a missing ``source_id`` /
    ``source_kind``. A large diff is stored as a preview (``ops_truncated``, the first
    ``DIFF_OPS_PREVIEW`` ops) plus the full document behind ``storage_ref`` /
    ``raw_diff_ref``: the full ops are loaded so no rule runs on a partial diff. Raises
    ``StoredDiffUnavailable`` when the full diff is gone for good (``load_stored=False``
    builds the event from the preview instead)."""
    data: dict[str, Any] = dict(row.diff or {})
    data["source_id"] = row.source_id
    if not data.get("source_kind") and source is not None:
        data["source_kind"] = source.kind
    truncated = bool(data.get("ops_truncated"))
    has_dict_ref = isinstance(data.get("storage_ref"), dict)
    if load_stored and (truncated or (not data.get("ops") and has_dict_ref)):
        _load_stored_ops(row, data)
    for k in ("storage_ref", "ops_truncated", "ops_total"):
        data.pop(k, None)
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
    sid = str(source_id)
    rows = session.execute(
        select(CallSite.operation)
        .join(Dependency, Dependency.id == CallSite.dependency_id)
        .where(
            # same join as the matcher: SDK packages reach their vendor spec / changelog
            # through dep.meta (the primary source_id stays the package registry)
            or_(
                Dependency.source_id == source_id,
                Dependency.meta["openapi_source_id"].astext == sid,
                Dependency.meta["changelog_source_id"].astext == sid,
            )
        )
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


def flag_partial_diff(c: Classification) -> Classification:
    """The full diff could not be read: a breaking verdict from the preview stands (a
    breaking op is breaking whatever else changed); anything milder may be hiding a
    removal past the preview, so it becomes ``unknown`` (a human looks) instead."""
    rule_hits = list(c.rule_hits)
    if RULE_DIFF_TRUNCATED not in rule_hits:
        rule_hits.append(RULE_DIFF_TRUNCATED)
    if c.severity == "breaking":
        return c.model_copy(update={"rule_hits": rule_hits})
    return c.model_copy(
        update={
            "severity": "unknown",
            "summary": f"{c.summary} (full diff unavailable; needs review)",
            "rule_hits": rule_hits,
        }
    )


def _cap_hint(hint: str | None) -> str | None:
    if not hint:
        return None
    flat = " ".join(hint.split())
    if len(flat) > MAX_HINT_CHARS:
        flat = flat[: MAX_HINT_CHARS - 1] + "…"
    return flat or None


def lower_effort(effort: str) -> str:
    """One step down ``EFFORT_LEVELS`` (``low`` stays ``low``; an unknown value is kept)."""
    if effort not in EFFORT_LEVELS:
        return effort
    return EFFORT_LEVELS[max(0, EFFORT_LEVELS.index(effort) - 1)]


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
    complete = True
    try:
        event = build_change_event(row, source)
    except StoredDiffUnavailable as e:
        # a breaking op in the preview is breaking whatever else changed; anything
        # milder could hide a removal past the preview (see flag_partial_diff)
        log.warning("change_event %s: classifying the truncated preview only: %s", row.id, e)
        event = build_change_event(row, source, load_stored=False)
        complete = False
    display_name = display_name_for(source)
    settings = get_settings()

    rules_result = classify_by_rules(event, display_name=display_name)
    llm_result: Classification | None = None
    usage: LLMUsage | None = None
    if should_call_model(event, rules_result):
        consumers = consumer_operations_for(session, row.source_id)
        llm = get_llm()

        def ask(**budget: Any) -> tuple[Classification, LLMUsage]:
            return classify_with_llm(
                event,
                consumer_operations=consumers,
                display_name=display_name,
                llm=llm,
                rules_result=rules_result,
                **budget,
            )

        try:
            try:
                llm_result, usage = ask()
            except LLMTruncated as e:
                # a subclass of LLMRefused: caught first. Thinking ate the budget; retry
                # once with more room and less thinking before giving up
                retry_effort = lower_effort(llm.settings.llm_classifier_effort)
                log.warning(
                    "change_event %s: model answer truncated (%s); retrying with "
                    "max_tokens=%s effort=%s",
                    row.id,
                    e,
                    TRUNCATED_RETRY_MAX_TOKENS,
                    retry_effort,
                )
                try:
                    llm_result, usage = ask(
                        max_tokens=TRUNCATED_RETRY_MAX_TOKENS, effort=retry_effort
                    )
                except LLMTruncated as e2:
                    log.warning("change_event %s: model answer truncated again: %s", row.id, e2)
                    llm_result = _fallback_unknown(event, RULE_LLM_TRUNCATED, str(e2))
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
    if not complete:
        final = flag_partial_diff(final)
    needs_human = needs_human_review(final)
    if needs_human and RULE_LLM_LOW_CONFIDENCE not in final.rule_hits and llm_result is None:
        # rules and model both silent (should not happen: the model always answers)
        log.warning("change_event %s: unknown without a model answer", row.id)

    out = ClassificationRow(
        change_event_id=row.id,
        severity=final.severity,
        category=final.category,
        summary=final.summary[:MAX_SUMMARY_CHARS],
        affected_paths=list(final.affected_paths),
        migration_hint=_cap_hint(final.migration_hint),
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
