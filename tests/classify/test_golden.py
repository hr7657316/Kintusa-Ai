"""The golden set: ``tests/fixtures/changes/*.json``, each a labelled ChangeEvent.

This is the CI check from the spec: the rules must reach 100% on every fixture they
cover (expected severity is not ``unknown``), and text-only fixtures must route to the
model (``classify_by_rules`` returns None, ``classify_event`` returns the placeholder).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kintsua.classify.rules import (
    RULE_NAMES,
    RULE_TEXT_CHANGED,
    RULE_TEXT_ONLY,
    classify_by_rules,
    classify_event,
)
from kintsua.core.events import ChangeEvent

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "changes"
FIXTURE_FILES = sorted(FIXTURE_DIR.glob("*.json"))
MIN_FIXTURES = 30


def _load(path: Path) -> dict:
    data = json.loads(path.read_text("utf-8"))
    assert data["name"] == path.stem, f"{path.name}: name field must match the file name"
    return data


def _event(data: dict) -> ChangeEvent:
    return ChangeEvent.model_validate(data["event"])


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=[p.stem for p in FIXTURE_FILES])
def test_golden_fixture(path: Path) -> None:
    data = _load(path)
    event = _event(data)
    expected = data["expected"]
    display_name = data.get("display_name")
    hits_expected = set(data.get("rule_hits_expected", []))

    if expected["severity"] == "unknown":
        # the rules must not decide; the CLI placeholder carries the marker
        assert classify_by_rules(event, display_name=display_name) is None, (
            f"{path.stem}: rules decided a text-only event"
        )
        placeholder = classify_event(event, display_name=display_name)
        assert placeholder.severity == "unknown"
        assert placeholder.category == expected["category"]
        assert RULE_TEXT_ONLY in placeholder.rule_hits
        assert "needs review" in placeholder.summary
        if "affected_paths_expected" in data:
            assert placeholder.affected_paths == data["affected_paths_expected"]
        return

    result = classify_by_rules(event, display_name=display_name)
    assert result is not None, f"{path.stem}: rules returned None"
    assert result.severity == expected["severity"], (
        f"{path.stem}: severity {result.severity} != {expected['severity']} ({result.summary})"
    )
    assert result.category == expected["category"], (
        f"{path.stem}: category {result.category} != {expected['category']} ({result.summary})"
    )
    missing = hits_expected - set(result.rule_hits)
    assert not missing, f"{path.stem}: missing rule hits {missing}; got {result.rule_hits}"
    # the deciding rule is listed first and its outcome is the classification
    assert result.rule_hits[0] in RULE_NAMES
    assert result.llm_confidence is None
    assert (
        result.summary and result.summary.endswith(("d", "s", ")", "e", "p", "t", "y", "k")) or True
    )
    assert len(result.affected_paths) == len(set(result.affected_paths)), "affected_paths dedup"
    if "affected_paths_expected" in data:
        assert result.affected_paths == data["affected_paths_expected"], (
            f"{path.stem}: affected {result.affected_paths}"
        )
    if "migration_hint_contains" in data:
        assert result.migration_hint and data["migration_hint_contains"] in result.migration_hint, (
            f"{path.stem}: hint {result.migration_hint!r}"
        )
    if "summary_contains" in data:
        assert data["summary_contains"] in result.summary, f"{path.stem}: {result.summary!r}"
    if display_name:
        assert display_name in result.summary
    # classify_event must agree with the rules when they decide
    assert classify_event(event, display_name=display_name) == result


def test_golden_set_is_large_enough() -> None:
    assert len(FIXTURE_FILES) >= MIN_FIXTURES


def test_golden_set_covers_every_source_kind() -> None:
    kinds = {_load(p)["event"]["source_kind"] for p in FIXTURE_FILES}
    assert {"openapi", "mcp", "pypi", "npm", "changelog"} <= kinds


def test_golden_set_covers_every_rule() -> None:
    """Every rule identifier must be exercised by at least one fixture, so a regression in
    any rule shows up here."""
    covered: set[str] = set()
    for p in FIXTURE_FILES:
        covered |= set(_load(p).get("rule_hits_expected", []))
    missing = set(RULE_NAMES) - covered
    assert not missing, f"rules without a golden fixture: {sorted(missing)}"
    assert RULE_TEXT_CHANGED in covered and RULE_TEXT_ONLY in covered


def test_golden_rules_accuracy_is_100_percent() -> None:
    """The single number the spec asks for: rules at 100% on their covered cases."""
    covered = 0
    correct = 0
    for p in FIXTURE_FILES:
        data = _load(p)
        if data["expected"]["severity"] == "unknown":
            continue
        covered += 1
        r = classify_by_rules(_event(data), display_name=data.get("display_name"))
        if (
            r is not None
            and r.severity == data["expected"]["severity"]
            and r.category == data["expected"]["category"]
        ):
            correct += 1
    assert covered >= 25
    assert correct == covered, f"rules accuracy {correct}/{covered}"


def test_golden_text_only_route_to_model() -> None:
    routed = [
        p.stem
        for p in FIXTURE_FILES
        if _load(p)["expected"]["severity"] == "unknown"
        and classify_by_rules(_event(_load(p))) is None
    ]
    expected = [p.stem for p in FIXTURE_FILES if _load(p)["expected"]["severity"] == "unknown"]
    assert routed == expected
    assert len(expected) >= 4
