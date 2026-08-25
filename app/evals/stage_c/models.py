"""Strict, immutable input contracts for Stage C retrieval evaluation."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class StrictModel(BaseModel):
    """Base model which rejects unrecognised or coerced case data."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, str_strip_whitespace=True
    )


class StageCCategory(str, Enum):
    SIMPLE_POLICY = "simple_policy"
    MULTI_CONDITION_POLICY = "multi_condition_policy"
    MIXED_FACT_POLICY = "mixed_fact_policy"
    SAFETY = "safety"


class StageCScenario(str, Enum):
    MAIN_ACTIVE = "main_active"
    CROSS_TENANT_CONFLICT = "cross_tenant_conflict"
    PROMPT_INJECTION_DOCUMENT = "prompt_injection_document"
    DOCUMENT_DISABLED = "document_disabled"
    OLD_VERSION_INACTIVE = "old_version_inactive"
    BUDGET_ATTACK = "budget_attack"


class ExpectedBehavior(str, Enum):
    ANSWER_GROUNDED = "answer_grounded"
    ABSTAIN = "abstain"
    CLARIFY = "clarify"
    DENY_CROSS_TENANT = "deny_cross_tenant"


class RetrievalStrategy(str, Enum):
    SINGLE = "single"
    MULTI = "multi"


class StageCVariant(str, Enum):
    """Named execution variants used by the Stage C harness."""

    BASELINE = "baseline"
    ADAPTIVE = "adaptive"


class TenantKey(str, Enum):
    ORG_A = "org_a"
    ORG_B = "org_b"


class KeyAnswerFact(StrictModel):
    fact_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)


class EvidenceAlternative(StrictModel):
    document_key: str = Field(min_length=1)
    heading_path: str = Field(min_length=1)


class EvidenceGroup(StrictModel):
    group_id: str = Field(min_length=1)
    supports_fact_ids: tuple[str, ...] = Field(min_length=1)
    any_of: tuple[EvidenceAlternative, ...] = Field(min_length=1)


class StrategyExpectation(StrictModel):
    preferred: RetrievalStrategy
    allowed: tuple[RetrievalStrategy, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def preferred_strategy_must_be_allowed(self) -> StrategyExpectation:
        if self.preferred not in self.allowed:
            raise ValueError("preferred strategy must be included in allowed")
        return self


class BusinessContext(StrictModel):
    """Business facts needed for a mixed fact-and-policy scenario."""

    as_of_date: datetime
    required_tools: tuple[str, ...]
    order: dict[str, JsonValue] | None
    logistics: dict[str, JsonValue] | None


class SecurityExpectations(StrictModel):
    must_not_use_tenants: tuple[TenantKey, ...] = ()
    must_not_follow_document_instructions: bool = False
    must_not_use_inactive_versions: bool = False
    max_search_rounds: int | None = Field(default=None, ge=1)
    max_first_round_queries: int | None = Field(default=None, ge=1)
    max_second_round_queries: int | None = Field(default=None, ge=1)


class StageCCase(StrictModel):
    case_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    category: StageCCategory
    scenario: StageCScenario
    tenant_key: TenantKey
    question: str = Field(min_length=1)
    reference_answer: str = Field(min_length=1)
    key_answer_facts: tuple[KeyAnswerFact, ...]
    required_evidence_groups: tuple[EvidenceGroup, ...]
    should_have_answer: bool
    expected_behavior: ExpectedBehavior
    strategy_expectation: StrategyExpectation | None
    business_context: BusinessContext | None
    forbidden_tenant_keys: tuple[TenantKey, ...]
    security_expectations: SecurityExpectations | None
    notes: str

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> StageCCase:
        fact_ids = [fact.fact_id for fact in self.key_answer_facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("key_answer_facts contain duplicate fact_id values")

        group_ids = [group.group_id for group in self.required_evidence_groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError(
                "required_evidence_groups contain duplicate group_id values"
            )

        known_fact_ids = set(fact_ids)
        dangling_fact_ids = {
            fact_id
            for group in self.required_evidence_groups
            for fact_id in group.supports_fact_ids
            if fact_id not in known_fact_ids
        }
        if dangling_fact_ids:
            raise ValueError(
                "required_evidence_groups reference unknown fact_id values: "
                + ", ".join(sorted(dangling_fact_ids))
            )

        is_mixed = self.category is StageCCategory.MIXED_FACT_POLICY
        if is_mixed != (self.business_context is not None):
            raise ValueError(
                "business_context is required only for mixed_fact_policy cases"
            )

        if (
            self.expected_behavior is ExpectedBehavior.ANSWER_GROUNDED
            and not self.required_evidence_groups
        ):
            raise ValueError(
                "answer_grounded cases require at least one evidence group"
            )
        return self


class StageCCaseLoadError(ValueError):
    """Raised when a JSONL case record cannot be parsed at its source line."""

    def __init__(self, line_number: int, detail: str) -> None:
        super().__init__(f"Invalid Stage C case at line {line_number}: {detail}")
        self.line_number = line_number


def load_stage_c_cases(path: str | Path) -> tuple[StageCCase, ...]:
    """Load strict Stage C cases from a JSONL corpus with line-aware errors."""

    cases: list[StageCCase] = []
    case_ids: set[str] = set()
    for line_number, raw_line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        try:
            case = StageCCase.model_validate_json(raw_line, strict=True)
        except ValueError as exc:
            raise StageCCaseLoadError(line_number, str(exc)) from exc
        if case.case_id in case_ids:
            raise StageCCaseLoadError(
                line_number, f"duplicate case_id: {case.case_id}"
            )
        case_ids.add(case.case_id)
        cases.append(case)
    return tuple(cases)
