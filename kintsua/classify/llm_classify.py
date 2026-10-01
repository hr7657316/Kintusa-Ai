"""The model residue of the classifier.

Only events the rules could not decide (text-only diffs), changelog events and package
release notes reach the model. Structured output (``LLM.complete_json``) means parsing
never fails; below ``Settings.llm_confidence_threshold`` the severity is forced to
``unknown`` so the event lands in the weekly report under "needs a human look" instead
of opening issues.
"""

from __future__ import annotations

import logging

from kintsua.classify.prompts import SYSTEM_PROMPT, build_user_message
from kintsua.classify.schema import Classification, LLMClassification
from kintsua.core.events import ChangeEvent
from kintsua.core.llm import LLM, LLMUsage, get_llm

log = logging.getLogger(__name__)

RULE_LLM = "llm"
RULE_LLM_LOW_CONFIDENCE = "llm_low_confidence"
MAX_AFFECTED_PATHS = 100


def needs_human_review(classification: Classification) -> bool:
    """True when the event must not open issues on its own: the severity is ``unknown``
    (forced by low confidence, or because neither rules nor the model could tell)."""
    return classification.severity == "unknown"


def classify_with_llm(
    event: ChangeEvent,
    *,
    consumer_operations: list[str],
    display_name: str | None = None,
    llm: LLM | None = None,
    rules_result: Classification | None = None,
) -> tuple[Classification, LLMUsage]:
    """Ask the model for a ``LLMClassification`` and turn it into a ``Classification``.

    ``consumer_operations`` is the anonymised list of operations / tool names consumers
    call (no repo names). ``rules_result`` (when the rules already decided) is shown to the
    model as a floor it may confirm or raise. ``rule_hits`` is ``["llm"]`` plus
    ``"llm_low_confidence"`` when the confidence fell below the threshold.
    """
    llm = llm or get_llm()
    settings = llm.settings
    user = build_user_message(
        event,
        consumer_operations=consumer_operations,
        display_name=display_name,
        rules_result=rules_result,
    )
    parsed, usage = llm.complete_json(
        SYSTEM_PROMPT,
        user,
        LLMClassification,
        model=settings.llm_classifier_model,
        effort=settings.llm_classifier_effort,
    )
    threshold = settings.llm_confidence_threshold
    severity = parsed.severity
    rule_hits = [RULE_LLM]
    if parsed.confidence < threshold:
        severity = "unknown"
        rule_hits.append(RULE_LLM_LOW_CONFIDENCE)
    affected = [p.strip() for p in parsed.affected_paths if isinstance(p, str) and p.strip()]
    affected = list(dict.fromkeys(affected))[:MAX_AFFECTED_PATHS]
    hint = (parsed.migration_hint or "").strip() or None
    summary = " ".join(parsed.summary.split()) or "Upstream change needs review"
    result = Classification(
        severity=severity,
        category=parsed.category,
        summary=summary,
        affected_paths=affected,
        migration_hint=hint,
        rule_hits=rule_hits,
        llm_confidence=float(parsed.confidence),
    )
    log.info(
        "llm classify source_kind=%s model=%s prompt_hash=%s confidence=%.2f severity=%s(model=%s) category=%s",
        event.source_kind,
        usage.model,
        usage.prompt_hash[:12],
        parsed.confidence,
        result.severity,
        parsed.severity,
        result.category,
    )
    return result, usage
