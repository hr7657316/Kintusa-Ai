"""Classifier output schema. One ``Classification`` per change event."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["breaking", "degraded", "safe", "unknown"]
Category = Literal[
    "removal",
    "rename",
    "required_param",
    "type_change",
    "auth",
    "deprecation",
    "behaviour",
    "docs_only",
]


class Classification(BaseModel):
    severity: Severity
    category: Category
    summary: str  # one sentence, plain English, for the issue title
    affected_paths: list[str] = Field(
        default_factory=list
    )  # operations / tool names a consumer could call
    migration_hint: str | None = None  # what a fix looks like, if obvious
    rule_hits: list[str] = Field(default_factory=list)
    llm_confidence: float | None = None


class LLMClassification(BaseModel):
    """What the model is asked to produce (no rule_hits; the caller fills those)."""

    severity: Severity
    category: Category
    summary: str
    affected_paths: list[str] = Field(default_factory=list)
    migration_hint: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
