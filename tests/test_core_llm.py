"""LLM adapter: output-budget truncation (core-correctness-6)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from kintsua.core.llm import (
    LLM,
    AnthropicProvider,
    FakeLLM,
    LLMRefused,
    LLMTruncated,
    OpenAIProvider,
)


class Answer(BaseModel):
    severity: str


def _anthropic(resp) -> AnthropicProvider:
    p = AnthropicProvider.__new__(AnthropicProvider)
    p.client = SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: resp))
    )
    return p


def _usage():
    return SimpleNamespace(input_tokens=10, output_tokens=16000)


def test_anthropic_max_tokens_stop_is_truncation_not_invalid_output():
    resp = SimpleNamespace(stop_reason="max_tokens", content=[], usage=_usage(), model="m")
    with pytest.raises(LLMTruncated, match="max_tokens"):
        _anthropic(resp).complete_json(
            "sys", "user", Answer, model="claude-opus-5-5", max_tokens=16000, effort="medium"
        )
    # cut mid-JSON: still truncation, never a pydantic ValidationError
    partial = SimpleNamespace(
        stop_reason="max_tokens",
        content=[SimpleNamespace(type="text", text='{"sever')],
        usage=_usage(),
        model="m",
    )
    with pytest.raises(LLMTruncated):
        _anthropic(partial).complete_json(
            "sys", "user", Answer, model="claude-opus-5-5", max_tokens=16000, effort="medium"
        )
    assert issubclass(LLMTruncated, LLMRefused)  # callers that handle refusals stay safe


def test_anthropic_complete_answer_reports_stop_reason():
    resp = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text='{"severity": "breaking"}')],
        usage=_usage(),
        model="m",
    )
    parsed, usage = _anthropic(resp).complete_json(
        "sys", "user", Answer, model="claude-opus-5-5", max_tokens=16000, effort="medium"
    )
    assert parsed.severity == "breaking" and usage.stop_reason == "end_turn"


def test_openai_length_finish_is_truncation():
    p = OpenAIProvider.__new__(OpenAIProvider)
    resp = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content='{"se'))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        model="gpt",
    )
    p.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: resp))
    )
    with pytest.raises(LLMTruncated):
        p.complete_json("sys", "user", Answer, model="gpt", max_tokens=100, effort="low")


def test_complete_json_default_budget_leaves_room_for_thinking(settings):
    fake = FakeLLM()
    fake.queue_json({"severity": "breaking"})
    llm = LLM(provider=fake, settings=settings)
    llm.complete_json("sys", "user", Answer)
    assert fake.calls[-1]["max_tokens"] >= 16000
    # the fake can simulate a truncated answer for callers' tests
    fake.queue_json(LLMTruncated("hit max_tokens"))
    with pytest.raises(LLMTruncated):
        llm.complete_json("sys", "user", Answer)
