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


# Planner prompt v2:
# - Strongly discourages NONE for policy/rule questions, which was a major source
#   of missing evidence in Stage C.
# - Encourages MULTI for multi-condition / mixed questions, while preferring two
#   queries to keep the 15s search budget manageable.
PLANNER_PROMPT_VERSION = "planner-v2"
PLANNER_SYSTEM_PROMPT = """Classify and rewrite the user question for knowledge retrieval.
Return one json object only. Do not answer the question or expose reasoning.
Never output tenant ids, thresholds, budgets, model names, or top-k controls.

Rules:
- NONE is ONLY for questions that are purely about order/logistics/ticket operations and do NOT need enterprise policy/rule knowledge. If the question mentions any policy/rule topic such as return, refund, warranty, compensation, shipping-time, member benefits, coupon, after-sales, or similar, do NOT use NONE.
- SINGLE is for one clear policy question. Use the original question as the query unless a concise rewrite is clearly better.
- MULTI is for questions that need multiple policy sections, multiple conditions/exceptions, or business facts plus policy. Generate 2-3 self-contained retrieval queries. Prefer 2 queries to control latency. Each query should be independently searchable and preserve key terms from the original question.

Allowed reason codes: BUSINESS_ONLY, SIMPLE_POLICY, MULTI_CONDITION,
MIXED_FACT_POLICY.

Examples:
{"strategy":"NONE","queries":[],"reason_code":"BUSINESS_ONLY"}
{"strategy":"SINGLE","queries":["普通会员无理由退货期是多少"],"reason_code":"SIMPLE_POLICY"}
{"strategy":"MULTI","queries":["普通会员退货时限","无理由退货的运费承担"],"reason_code":"MULTI_CONDITION"}
"""

# Terms that almost always indicate the user is asking about enterprise policy.
_STRONG_POLICY_KEYWORDS = (
    "退货", "退款", "换货", "保修", "维修", "换新", "赔偿", "补偿", "赔付",
    "运费", "免运费", "优惠券", "积分", "赠品", "会员", "售后", "政策",
    "规则", "时效", "缺货", "取消", "保留", "审核", "延迟", "超时",
    "大促", "活动", "有效期", "期限", "到账", "门槛", "无理由",
)

# Terms that can be business-only, so they only count as policy intent when the
# user is also asking a policy-style question (how long / how much / can I ...).
_WEAK_POLICY_KEYWORDS = ("发货", "物流")

_POLICY_QUESTION_HINTS = (
    "多久", "多少", "几天", "能不能", "是否可以", "是否", "怎么办",
    "怎么处理", "如何",
)


def _is_policy_question(question: str) -> bool:
    """Return True when the question looks like it needs knowledge retrieval."""
    if any(keyword in question for keyword in _STRONG_POLICY_KEYWORDS):
        return True
    return any(
        keyword in question for keyword in _WEAK_POLICY_KEYWORDS
    ) and any(hint in question for hint in _POLICY_QUESTION_HINTS)


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

        # Deterministic safety net: if the model says "no search needed" for a
        # question that clearly mentions policy/rule terms, fall back to a normal
        # single-query search instead of returning no evidence.
        if plan.strategy is SearchStrategy.NONE and _is_policy_question(clean_question):
            plan = SearchPlan(
                strategy=SearchStrategy.SINGLE,
                queries=(clean_question,),
                reason_code=SearchReasonCode.SIMPLE_POLICY,
            )
            return PlanDecision(
                plan=plan,
                model_calls=1,
                estimated_tokens=completion.estimated_tokens,
                degraded=True,
            )

        # Keep MULTI at two queries for now: three simultaneous retrieval calls
        # are the main cause of Stage C budget exhaustion before the assessor can
        # run. This is a Planner-side safety cap, not a change to Baseline RAG.
        degraded = False
        if plan.strategy is SearchStrategy.MULTI and len(plan.queries) > 2:
            plan = SearchPlan(
                strategy=SearchStrategy.MULTI,
                queries=plan.queries[:2],
                reason_code=plan.reason_code,
            )
            degraded = True

        return PlanDecision(
            plan=plan,
            model_calls=1,
            estimated_tokens=completion.estimated_tokens,
            degraded=degraded,
        )
