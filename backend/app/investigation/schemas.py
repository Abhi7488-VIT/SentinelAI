"""Structured LLM outputs per node (plan §5). Validated by `LLMClient` against these models.

None of them has a status field: the model cannot mark anything confirmed (plan §7). Ids in the
output are only *proposals*; code keeps the ones in the enumerated sets (services) or in the
`evidence` table for this investigation (citations) and drops the rest.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.investigation.taxonomy import Category

_STRICT = ConfigDict(extra="forbid")
EvidenceId = str  # format checked against the table, not trusted from the model


class PlannerOutput(BaseModel):
    model_config = _STRICT
    focus_service_ids: list[str] = Field(min_length=1, max_length=10)
    rationale: str = Field(max_length=800)


class Claim(BaseModel):
    model_config = _STRICT
    statement: str = Field(min_length=1, max_length=600)
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=12)


class InvestigatorFindings(BaseModel):
    model_config = _STRICT
    claims: list[Claim] = Field(max_length=12)


class HypothesisDraft(BaseModel):
    model_config = _STRICT
    category: Category
    subject_service_ids: list[str] = Field(min_length=1, max_length=5)
    statement: str = Field(min_length=1, max_length=800)
    supporting_evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=15)
    contradicting_evidence_ids: list[EvidenceId] = Field(default_factory=list, max_length=15)
    missing_evidence: list[str] = Field(default_factory=list, max_length=5)
    confidence: float = Field(ge=0.0, le=1.0)


class AnalystOutput(BaseModel):
    model_config = _STRICT
    hypotheses: list[HypothesisDraft] = Field(min_length=2, max_length=5)
