"""Strict query planning for adaptive tenant-scoped retrieval."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.knowledge.structured_llm import StructuredJSONClient


class SearchStrategy(str, Enum):
    NONE = "NONE"
    SINGLE = "SINGLE"
    MULTI = "MULTI"


class SearchReasonCode(str, Enum):
    BUSINESS_ONLY = "BUSINESS_ONLY"
    SIMPLE_POLICY = "SIMPLE_POLICY"
    MULTI_CONDITION = "MULTI_CONDITION"
    MIXED_FACT_POLICY = "MIXED_FACT_POLICY"


class SearchPlan(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, frozen=True
    )

    strategy: SearchStrategy
    queries: tuple[str, ...]
    reason_code: SearchReasonCode

    @model_validator(mode="after")
    def validate_shape(self) -> "SearchPlan":
        if any(not query or len(query) > 1000 for query in self.queries):
            raise ValueError("queries must contain 1..1000 character strings")
        folded = [query.casefold() for query in self.queries]
        if len(folded) != len(set(folded)):
            raise ValueError("queries must be unique after case folding")

        count = len(self.queries)
        if self.strategy is SearchStrategy.NONE:
            valid = count == 0 and self.reason_code is SearchReasonCode.BUSINESS_ONLY
        elif self.strategy is SearchStrategy.SINGLE:
            valid = count == 1 and self.reason_code in {
                SearchReasonCode.SIMPLE_POLICY,
                SearchReasonCode.MIXED_FACT_POLICY,
            }
        else:
            valid = 2 <= count <= 3 and self.reason_code in {
                SearchReasonCode.MULTI_CONDITION,
                SearchReasonCode.MIXED_FACT_POLICY,
            }
        if not valid:
            raise ValueError("strategy, query count, and reason code disagree")
        return self


@dataclass(frozen=True)
class PlanDecision:
    plan: SearchPlan
    model_calls: int
    estimated_tokens: int
    degraded: bool


PLANNER_SYSTEM_PROMPT = """Classify and rewrite the user question for retrieval.
Return one json object only. Do not answer the question or expose reasoning.
Never output tenant ids, thresholds, budgets, model names, or top-k controls.
Allowed reason codes: BUSINESS_ONLY, SIMPLE_POLICY, MULTI_CONDITION,
MIXED_FACT_POLICY.
Examples:
{"strategy":"NONE","queries":[],"reason_code":"BUSINESS_ONLY"}
{"strategy":"SINGLE","queries":["return window"],"reason_code":"SIMPLE_POLICY"}
{"strategy":"MULTI","queries":["packaging requirements","return window"],"reason_code":"MULTI_CONDITION"}
"""


class QueryPlanner:
    def __init__(self, client: StructuredJSONClient) -> None:
        self._client = client

    def plan(self, *, question: str, timeout_seconds: float) -> PlanDecision:
        clean_question = question.strip()
        if not clean_question:
            raise ValueError("question must not be blank")

        completion = self._client.complete(
            system_prompt=PLANNER_SYSTEM_PROMPT,
            user_prompt=clean_question,
            timeout_seconds=timeout_seconds,
        )
        try:
            plan = SearchPlan.model_validate(json.loads(completion.raw_json))
        except (json.JSONDecodeError, ValidationError, TypeError):
            return PlanDecision(
                plan=SearchPlan(
                    strategy=SearchStrategy.SINGLE,
                    queries=(clean_question,),
                    reason_code=SearchReasonCode.SIMPLE_POLICY,
                ),
                model_calls=1,
                estimated_tokens=completion.estimated_tokens,
                degraded=True,
            )
        return PlanDecision(
            plan=plan,
            model_calls=1,
            estimated_tokens=completion.estimated_tokens,
            degraded=False,
        )
