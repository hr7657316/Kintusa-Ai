"""One adapter in front of the Anthropic and OpenAI SDKs.

Two operations:

* ``complete_json(system, user, schema)`` - structured output validated against a
  Pydantic model. Used by the classifier. Deterministic settings, prompt hash logged.
* ``complete_tools(system, messages, tools)`` - one turn of a tool-using conversation.
  Used by the fixer's bounded agent loop. Returns a provider-neutral ``LLMTurn``.

The ``fake`` provider is for tests: queue ``FakeLLM`` responses and assert on prompts.
All fetched vendor text is passed to the model as *data* inside the user message, never
as part of the system prompt, which is how the prompt-injection rule in the spec is kept.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from kintsua.core.config import Settings, get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMUsage:
    model: str
    prompt_hash: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    request_id: str | None = None
    stop_reason: str | None = None  # provider stop reason, for the log line

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMTurn:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal | other
    usage: LLMUsage
    raw_assistant_content: Any = None  # provider-native content to echo back


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class ChatMessage:
    """Provider-neutral message. ``content`` is text (user/assistant), a list of
    ``ToolResult`` (role ``tool``) or an ``LLMTurn`` (role ``assistant`` with tool calls)."""

    role: str  # user | assistant | tool
    content: Any


def prompt_hash(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, default=str).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


# --------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------


class Provider:
    name = "base"

    def complete_json(
        self, system: str, user: str, schema: type[T], *, model: str, max_tokens: int, effort: str
    ) -> tuple[T, LLMUsage]:
        raise NotImplementedError

    def complete_tools(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        model: str,
        max_tokens: int,
        effort: str,
    ) -> LLMTurn:
        raise NotImplementedError


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str | None):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def _supports_fallbacks(self, model: str) -> bool:
        return model.startswith(("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5"))

    def complete_json(self, system, user, schema, *, model, max_tokens, effort):
        t0 = time.monotonic()
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": _strict_schema(schema)},
            },
        )
        if self._supports_fallbacks(model):
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        resp = self.client.beta.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            raise LLMRefused(
                getattr(getattr(resp, "stop_details", None), "explanation", None) or "refused"
            )
        if resp.stop_reason == "max_tokens":
            # thinking counts toward max_tokens: the JSON answer is cut short or missing
            raise LLMTruncated(
                f"model output hit max_tokens={max_tokens} before the JSON answer was complete"
            )
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        parsed = schema.model_validate_json(text)
        usage = LLMUsage(
            model=getattr(resp, "model", model),
            prompt_hash=prompt_hash(system, user, schema.model_json_schema()),
            input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            request_id=getattr(resp, "_request_id", None),
            stop_reason=resp.stop_reason,
        )
        return parsed, usage

    def complete_tools(self, system, messages, tools, *, model, max_tokens, effort):
        t0 = time.monotonic()
        native_messages = [self._to_native(m) for m in messages]
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=native_messages,
            tools=[
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "strict": True,
                }
                for t in tools
            ],
            output_config={"effort": effort},
        )
        if self._supports_fallbacks(model):
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        resp = self.client.beta.messages.create(**kwargs)
        text_parts, calls = [], []
        for b in resp.content:
            if b.type == "text":
                text_parts.append(b.text)
            elif b.type == "tool_use":
                inp = b.input if isinstance(b.input, dict) else json.loads(json.dumps(b.input))
                calls.append(ToolCall(id=b.id, name=b.name, input=inp))
        stop = resp.stop_reason or "other"
        usage = LLMUsage(
            model=getattr(resp, "model", model),
            prompt_hash=prompt_hash(system, [t.name for t in tools]),
            input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            request_id=getattr(resp, "_request_id", None),
        )
        return LLMTurn(
            text="\n".join(text_parts),
            tool_calls=calls,
            stop_reason=stop,
            usage=usage,
            raw_assistant_content=resp.content,
        )

    @staticmethod
    def _to_native(m: ChatMessage) -> dict[str, Any]:
        if m.role == "user":
            return {"role": "user", "content": m.content}
        if m.role == "assistant":
            if isinstance(m.content, LLMTurn):
                if m.content.raw_assistant_content is not None:
                    return {"role": "assistant", "content": m.content.raw_assistant_content}
                blocks: list[dict[str, Any]] = []
                if m.content.text:
                    blocks.append({"type": "text", "text": m.content.text})
                for c in m.content.tool_calls:
                    blocks.append(
                        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                    )
                return {"role": "assistant", "content": blocks}
            return {"role": "assistant", "content": str(m.content)}
        if m.role == "tool":
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.tool_call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                    for r in m.content
                ],
            }
        raise ValueError(f"bad role {m.role}")


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, api_key: str | None):
        import openai

        self.client = openai.OpenAI(api_key=api_key) if api_key else openai.OpenAI()

    def complete_json(self, system, user, schema, *, model, max_tokens, effort):
        t0 = time.monotonic()
        resp = self.client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0,
            max_completion_tokens=max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": _strict_schema(schema),
                    "strict": True,
                },
            },
        )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise LLMTruncated(
                f"model output hit max_tokens={max_tokens} before the JSON answer was complete"
            )
        text = choice.message.content or "{}"
        parsed = schema.model_validate_json(text)
        u = resp.usage
        usage = LLMUsage(
            model=resp.model or model,
            prompt_hash=prompt_hash(system, user, schema.model_json_schema()),
            input_tokens=getattr(u, "prompt_tokens", 0) or 0,
            output_tokens=getattr(u, "completion_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            stop_reason=choice.finish_reason,
        )
        return parsed, usage

    def complete_tools(self, system, messages, tools, *, model, max_tokens, effort):
        t0 = time.monotonic()
        native: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "user":
                native.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                if isinstance(m.content, LLMTurn):
                    native.append(
                        {
                            "role": "assistant",
                            "content": m.content.text or None,
                            "tool_calls": [
                                {
                                    "id": c.id,
                                    "type": "function",
                                    "function": {"name": c.name, "arguments": json.dumps(c.input)},
                                }
                                for c in m.content.tool_calls
                            ]
                            or None,
                        }
                    )
                else:
                    native.append({"role": "assistant", "content": str(m.content)})
            elif m.role == "tool":
                for r in m.content:
                    native.append(
                        {"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content}
                    )
        resp = self.client.chat.completions.create(
            model=model,
            messages=native,
            temperature=0,
            max_completion_tokens=max_tokens,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                        "strict": True,
                    },
                }
                for t in tools
            ],
        )
        choice = resp.choices[0]
        calls = [
            ToolCall(
                id=tc.id, name=tc.function.name, input=json.loads(tc.function.arguments or "{}")
            )
            for tc in (choice.message.tool_calls or [])
        ]
        stop = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}.get(
            choice.finish_reason or "", choice.finish_reason or "other"
        )
        u = resp.usage
        usage = LLMUsage(
            model=resp.model or model,
            prompt_hash=prompt_hash(system, [t.name for t in tools]),
            input_tokens=getattr(u, "prompt_tokens", 0) or 0,
            output_tokens=getattr(u, "completion_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return LLMTurn(
            text=choice.message.content or "", tool_calls=calls, stop_reason=stop, usage=usage
        )


class FakeLLM(Provider):
    """Scripted provider for tests.

    ``queue_json(obj_or_model)`` queues the next ``complete_json`` answer (queue an
    exception instance, e.g. ``LLMTruncated(...)``, to have that call raise it);
    ``queue_turn(text=..., tool_calls=[...])`` queues the next ``complete_tools`` answer.
    Every call is recorded in ``calls``.
    """

    name = "fake"

    def __init__(self):
        self.json_responses: deque[Any] = deque()
        self.turn_responses: deque[LLMTurn | dict[str, Any]] = deque()
        self.calls: list[dict[str, Any]] = []
        self.default_json: Any = None

    def queue_json(self, obj: Any) -> None:
        self.json_responses.append(obj)

    def queue_turn(
        self,
        text: str = "",
        tool_calls: list[tuple[str, dict[str, Any]]] | None = None,
        stop_reason: str | None = None,
    ) -> None:
        calls = [
            ToolCall(id=f"call_{i}", name=n, input=i_) for i, (n, i_) in enumerate(tool_calls or [])
        ]
        self.turn_responses.append(
            LLMTurn(
                text=text,
                tool_calls=calls,
                stop_reason=stop_reason or ("tool_use" if calls else "end_turn"),
                usage=LLMUsage(model="fake", prompt_hash="fake", input_tokens=10, output_tokens=5),
            )
        )

    def complete_json(self, system, user, schema, *, model, max_tokens, effort):
        self.calls.append(
            {
                "op": "json",
                "system": system,
                "user": user,
                "schema": schema.__name__,
                "model": model,
                "max_tokens": max_tokens,
                "effort": effort,
            }
        )
        if self.json_responses:
            obj = self.json_responses.popleft()
        elif self.default_json is not None:
            obj = self.default_json
        else:
            raise RuntimeError("FakeLLM: no queued JSON response")
        if isinstance(obj, BaseException):
            raise obj
        parsed = obj if isinstance(obj, schema) else schema.model_validate(obj)
        return parsed, LLMUsage(
            model="fake", prompt_hash=prompt_hash(system, user), input_tokens=10, output_tokens=5
        )

    def complete_tools(self, system, messages, tools, *, model, max_tokens, effort):
        self.calls.append(
            {
                "op": "tools",
                "system": system,
                "messages": list(messages),
                "tools": [t.name for t in tools],
                "model": model,
            }
        )
        if not self.turn_responses:
            raise RuntimeError("FakeLLM: no queued turn response")
        turn = self.turn_responses.popleft()
        if isinstance(turn, dict):
            turn = LLMTurn(**turn)
        turn.usage = LLMUsage(model="fake", prompt_hash="fake", input_tokens=10, output_tokens=5)
        return turn


class LLMRefused(Exception):
    """The model gave no usable answer (a safety refusal)."""


class LLMTruncated(LLMRefused):
    """The output hit ``max_tokens`` before the structured answer was complete (adaptive
    thinking counts toward the cap). A subclass of ``LLMRefused`` so callers that treat
    "no usable answer" alike keep working; catch it first to tell the two apart (and,
    say, retry with a larger ``max_tokens``)."""


# Default output budget for ``complete_json``: thinking plus the JSON answer. ~16k is the
# largest budget that stays a plain (non-streaming) request on the Anthropic SDK.
JSON_MAX_TOKENS = 16000


# --------------------------------------------------------------------------------------
# Facade
# --------------------------------------------------------------------------------------


@dataclass
class LLM:
    provider: Provider
    settings: Settings = field(default_factory=get_settings)

    def classifier_model(self) -> str:
        return self.settings.llm_classifier_model

    def fixer_model(self) -> str:
        return self.settings.llm_fixer_model

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        model: str | None = None,
        max_tokens: int = JSON_MAX_TOKENS,
        effort: str | None = None,
    ) -> tuple[T, LLMUsage]:
        model = model or self.settings.llm_classifier_model
        effort = effort or self.settings.llm_classifier_effort
        try:
            parsed, usage = self.provider.complete_json(
                system, user, schema, model=model, max_tokens=max_tokens, effort=effort
            )
        except LLMTruncated:
            log.warning(
                "llm json model=%s stop=max_tokens max_tokens=%s effort=%s: answer truncated",
                model,
                max_tokens,
                effort,
            )
            raise
        log.info(
            "llm json model=%s stop=%s prompt_hash=%s in=%s out=%s latency_ms=%s",
            usage.model,
            usage.stop_reason or "-",
            usage.prompt_hash[:12],
            usage.input_tokens,
            usage.output_tokens,
            usage.latency_ms,
        )
        return parsed, usage

    def complete_tools(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        model: str | None = None,
        max_tokens: int = 16000,
        effort: str | None = None,
    ) -> LLMTurn:
        model = model or self.settings.llm_fixer_model
        effort = effort or self.settings.llm_fixer_effort
        turn = self.provider.complete_tools(
            system, messages, tools, model=model, max_tokens=max_tokens, effort=effort
        )
        log.info(
            "llm tools model=%s stop=%s calls=%s in=%s out=%s",
            turn.usage.model,
            turn.stop_reason,
            [c.name for c in turn.tool_calls],
            turn.usage.input_tokens,
            turn.usage.output_tokens,
        )
        return turn


_default: LLM | None = None
_lock = threading.Lock()


def get_llm() -> LLM:
    """Process-wide LLM built from settings. Tests call ``set_llm(LLM(FakeLLM()))``."""
    global _default
    with _lock:
        if _default is None:
            s = get_settings()
            if s.llm_provider == "anthropic":
                provider: Provider = AnthropicProvider(s.anthropic_api_key)
            elif s.llm_provider == "openai":
                provider = OpenAIProvider(s.openai_api_key)
            else:
                provider = FakeLLM()
            _default = LLM(provider=provider, settings=s)
        return _default


def set_llm(llm: LLM | None) -> None:
    global _default
    with _lock:
        _default = llm


def _strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema tightened for structured-output mode."""
    js = schema.model_json_schema()
    return _tighten(js)


def _tighten(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _tighten(v) for k, v in node.items() if k not in ("title", "default")}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"].keys())
        return out
    if isinstance(node, list):
        return [_tighten(v) for v in node]
    return node
