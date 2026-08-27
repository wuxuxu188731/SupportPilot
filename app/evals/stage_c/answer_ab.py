"""Stage C 最终答案 Baseline/Adaptive A/B 评测。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from uuid import uuid4

from pydantic import ValidationError

from app.agent.prompts import SUPPORT_SYSTEM_PROMPT
from app.agent.runner import run_one_turn
from app.application.organization_service import TenantContext
from app.evals.stage_c.checkpoint import sha256_corpus, sha256_file
from app.evals.stage_c.fixtures import StageCFixtureManager, corpus_specs
from app.evals.stage_c.models import StageCCase, StageCVariant, TenantKey, load_stage_c_cases
from app.knowledge.chunking import KnowledgeChunker
from app.knowledge.document_loader import DocumentLoader
from app.knowledge.factory import KnowledgeServices, create_knowledge_services
from app.knowledge.results import AdaptiveSearchResult, BaselineSearchResult
from app.organizations.base import MembershipRole
from app.tools.composite_gateway import CompositeToolGateway
from app.tools.knowledge_definitions import get_knowledge_tool_definitions
from app.tools.knowledge_arguments import SearchKnowledgeArguments
from app.tools.support_definitions import get_support_tool_definitions
from app.tools.support_results import tool_failure, tool_success


ANSWER_RUNNER_SCHEMA_VERSION = "stage-c-answer-ab-v1"
ANSWER_PROMPT_VERSION = "support-system-prompt-stage-c-answer-v1"
JUDGE_PROMPT_VERSION = "stage-c-pairwise-judge-v1"
_READ_ONLY_BUSINESS_TOOLS = {"get_order", "get_logistics"}


class KnowledgeSearchService(Protocol):
    def search(
        self, *, organization_id: str, question: str, conversation_id: str | None = None
    ) -> BaselineSearchResult | AdaptiveSearchResult:
        raise NotImplementedError


@dataclass(frozen=True)
class AnswerRunConfig:
    model_name: str  # 两个变体共同使用的最终回答模型名称
    prompt_version: str = ANSWER_PROMPT_VERSION  # 固定系统提示词的版本标识
    judge_model_name: str | None = None  # 盲化成对评审模型；为空时复用回答模型
    max_tool_rounds: int = 8  # 单条回答允许的最大工具调用轮数


class EvaluationKnowledgeToolGateway:
    """把两类检索结果归一成完全相同的公开知识工具契约。"""

    def __init__(
        self,
        *,
        service: KnowledgeSearchService,
        conversation_id: str,
    ) -> None:
        self._service = service
        self._conversation_id = conversation_id

    @property
    def definitions(self) -> list[dict]:
        return get_knowledge_tool_definitions()

    def bind(self, *, context: TenantContext) -> dict[str, Any]:
        used = False

        def search_knowledge(**arguments: Any) -> dict:
            nonlocal used
            try:
                parsed = SearchKnowledgeArguments.model_validate(arguments)
            except ValidationError:
                return tool_failure(
                    code="INVALID_ARGUMENTS", message="tool arguments are invalid"
                )
            if used:
                return tool_failure(
                    code="SEARCH_BUDGET_EXCEEDED",
                    message="knowledge search budget exceeded",
                )
            used = True
            result = self._service.search(
                organization_id=context.organization_id,
                question=parsed.question,
                conversation_id=self._conversation_id,
            )
            return normalize_knowledge_result(result)

        return {"search_knowledge": search_knowledge}


class FixedBusinessToolGateway:
    """为两组答案提供同一份冻结、只读业务事实。"""

    def __init__(self, case: StageCCase) -> None:
        self._case = case

    @property
    def definitions(self) -> list[dict]:
        return [
            item
            for item in get_support_tool_definitions()
            if item["function"]["name"] in _READ_ONLY_BUSINESS_TOOLS
        ]

    def bind(self, *, context: TenantContext) -> dict[str, Any]:
        del context
        business = self._case.business_context

        def get_order(**arguments: Any) -> dict:
            if business is None or business.order is None:
                return tool_failure(code="ORDER_NOT_FOUND", message="order not found")
            if arguments.get("order_no") != business.order.get("order_id"):
                return tool_failure(code="ORDER_NOT_FOUND", message="order not found")
            return tool_success(
                {
                    "as_of_date": business.as_of_date.isoformat(),
                    "order": dict(business.order),
                }
            )

        def get_logistics(**arguments: Any) -> dict:
            order_result = get_order(**arguments)
            if not order_result["ok"]:
                return order_result
            return tool_success(
                {
                    **order_result["data"],
                    "availability": (
                        "available" if business and business.logistics else "not_created"
                    ),
                    "logistics": (
                        dict(business.logistics)
                        if business and business.logistics is not None
                        else None
                    ),
                }
            )

        return {"get_order": get_order, "get_logistics": get_logistics}


def normalize_knowledge_result(
    result: BaselineSearchResult | AdaptiveSearchResult,
) -> dict[str, Any]:
    """消除 Baseline/Adaptive 原有 public_dict 外形差异。"""

    summary = result.retrieval_summary.public_dict()
    citations = [item.public_dict() for item in result.citations]
    data = {
        "result_code": (
            "SEARCH_NOT_NEEDED"
            if summary["evidence_status"] == "not_needed"
            else "KNOWLEDGE_FOUND" if result.ok and citations else "KNOWLEDGE_INSUFFICIENT"
        ),
        "strategy": summary["strategy"],
        "evidence_status": summary["evidence_status"],
        "citations": citations,
        "retrieval_summary": summary,
    }
    if result.ok:
        return {"ok": True, "data": data}
    return {
        "ok": False,
        "error": {
            "code": result.error.code if result.error else "SEARCH_INTERNAL_ERROR",
            "message": (
                result.error.safe_message
                if result.error
                else "knowledge search could not be completed"
            ),
        },
        "data": data,
    }


class _RecordingCompletions:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        response = self._delegate.create(**kwargs)
        usage = getattr(response, "usage", None)
        self.calls.append(
            {
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            }
        )
        return response


class _RecordingClient:
    def __init__(self, delegate: Any) -> None:
        self.completions = _RecordingCompletions(delegate.chat.completions)
        self.chat = type("RecordingChat", (), {"completions": self.completions})()


class AnswerCheckpoint:
    """原子保存每个答案变体和每条成对评审。"""

    def __init__(self, path: str | Path, metadata: Mapping[str, Any]) -> None:
        self.path = Path(path)
        canonical = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self._metadata = dict(metadata)
        self.state = self._load_or_initialize()

    def _load_or_initialize(self) -> dict[str, Any]:
        if not self.path.exists():
            state = {
                "schema_version": ANSWER_RUNNER_SCHEMA_VERSION,
                "fingerprint": self.fingerprint,
                "run_id": uuid4().hex,
                "metadata": self._metadata,
                "answers": {},
                "judgments": {},
            }
            self._write(state)
            return state
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if state.get("schema_version") != ANSWER_RUNNER_SCHEMA_VERSION:
            raise ValueError("answer checkpoint schema version mismatch")
        if state.get("fingerprint") != self.fingerprint:
            raise ValueError("answer checkpoint fingerprint mismatch")
        return state

    def answer(self, case_id: str, variant: StageCVariant) -> dict[str, Any] | None:
        return self.state["answers"].get(f"{case_id}:{variant.value}")

    def record_answer(self, payload: Mapping[str, Any]) -> None:
        key = f"{payload['case_id']}:{payload['variant']}"
        self.state["answers"][key] = dict(payload)
        self._write(self.state)

    def judgment(self, case_id: str) -> dict[str, Any] | None:
        return self.state["judgments"].get(case_id)

    def record_judgment(self, case_id: str, payload: Mapping[str, Any]) -> None:
        self.state["judgments"][case_id] = dict(payload)
        self._write(self.state)

    def _write(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as target:
            json.dump(state, target, ensure_ascii=False, sort_keys=True, indent=2)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, self.path)


class StageCAnswerABRunner:
    """在同一场景快照内依次生成 Baseline 和 Adaptive 最终答案。"""

    def __init__(
        self,
        *,
        cases: Sequence[StageCCase],
        services: KnowledgeServices,
        client: Any,
        checkpoint: AnswerCheckpoint,
        config: AnswerRunConfig,
        fixture_manager: StageCFixtureManager | None = None,
        fixture_specs: Sequence[Any] = (),
        organization_ids: Mapping[TenantKey, str] | None = None,
    ) -> None:
        self._cases = tuple(cases)
        self._services = services
        self._client = client
        self._checkpoint = checkpoint
        self._config = config
        self._fixture_manager = fixture_manager
        self._fixture_specs = tuple(fixture_specs)
        self._organization_ids = dict(
            organization_ids or {tenant: tenant.value for tenant in TenantKey}
        )

    def run(self) -> dict[str, Any]:
        if self._fixture_manager is not None:
            # Answer checkpoint intentionally does not own corpus IDs; restoring the
            # answer database is deterministic because fixture preparation is idempotent.
            self._fixture_manager.prepare(cases=self._cases, specs=self._fixture_specs)
        for case in self._cases:
            scenario = (
                self._fixture_manager.scenario(case)
                if self._fixture_manager is not None
                else nullcontext()
            )
            with scenario:
                for variant in (StageCVariant.BASELINE, StageCVariant.ADAPTIVE):
                    if self._checkpoint.answer(case.case_id, variant) is None:
                        self._checkpoint.record_answer(self._generate(case, variant))
                if self._checkpoint.judgment(case.case_id) is None:
                    self._checkpoint.record_judgment(case.case_id, self._judge_pair(case))
        return compile_answer_report(self._cases, self._checkpoint.state)

    def _generate(self, case: StageCCase, variant: StageCVariant) -> dict[str, Any]:
        organization_id = self._organization_ids[case.tenant_key]
        context = TenantContext(
            user_id="stage-c-fixture",
            organization_id=organization_id,
            role=MembershipRole.ADMIN,
        )
        service = (
            self._services.baseline
            if variant is StageCVariant.BASELINE
            else self._services.adaptive
        )
        conversation_id = (
            f"stage-c-answer:{self._checkpoint.state['run_id']}:{case.case_id}:{variant.value}"
        )
        gateway = CompositeToolGateway(
            [
                FixedBusinessToolGateway(case),
                EvaluationKnowledgeToolGateway(
                    service=service, conversation_id=conversation_id
                ),
            ]
        )
        recorder = _RecordingClient(self._client)
        started = time.perf_counter()
        response = run_one_turn(
            messages=[
                {"role": "system", "content": SUPPORT_SYSTEM_PROMPT},
                {"role": "user", "content": case.question},
            ],
            client=recorder,
            tool_definitions=gateway.definitions,
            tool_functions=gateway.bind(context=context),
            model_name=self._config.model_name,
            max_tool_rounds=self._config.max_tool_rounds,
        )
        tool_trace = [
            {
                "type": event.type,
                "tool_call_id": event.tool_call_id,
                "tool_name": event.tool_call_name,
                "arguments": event.tool_call_arguments,
                "result": event.result,
                "error": event.error,
                "duration_ms": event.duration_ms,
            }
            for event in response.events
            if event.type in {"tool_call.completed", "tool_call.failed", "citation.invalid"}
        ]
        return {
            "case_id": case.case_id,
            "variant": variant.value,
            "status": "completed",
            "answer": response.llm_answer or "",
            "citations": [item.public_dict() for item in response.citations],
            "retrieval_summary": (
                response.retrieval_summary.public_dict()
                if response.retrieval_summary is not None
                else None
            ),
            "tool_trace": tool_trace,
            "answer_incomplete": response.answer_incomplete,
            "model": self._config.model_name,
            "prompt_version": self._config.prompt_version,
            "model_calls": len(recorder.completions.calls),
            "usage": {
                name: sum(call[name] for call in recorder.completions.calls)
                for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            },
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    def _judge_pair(self, case: StageCCase) -> dict[str, Any]:
        baseline = self._checkpoint.answer(case.case_id, StageCVariant.BASELINE)
        adaptive = self._checkpoint.answer(case.case_id, StageCVariant.ADAPTIVE)
        assert baseline is not None and adaptive is not None
        swapped = int(hashlib.sha256(case.case_id.encode()).hexdigest(), 16) % 2 == 1
        candidates = [adaptive, baseline] if swapped else [baseline, adaptive]
        prompt = _judge_prompt(case, candidates[0], candidates[1])
        started = time.perf_counter()
        raw: dict[str, Any] | None = None
        last_error: Exception | None = None
        for judge_attempt in range(2):
            retry_instruction = (
                "\n上一次输出无法解析。不要解释、不要使用 Markdown，只输出一个完整 JSON 对象。"
                if judge_attempt
                else ""
            )
            response = self._client.chat.completions.create(
                model=self._config.judge_model_name or self._config.model_name,
                messages=[
                    {"role": "system", "content": "你是严格、盲化的客服答案评审器，只输出 JSON。"},
                    {"role": "user", "content": prompt + retry_instruction},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            try:
                candidate_payload = parse_judge_json(response.choices[0].message.content)
                _validate_judge_payload(candidate_payload)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                last_error = error
                continue
            raw = candidate_payload
            break
        if raw is None:
            raise RuntimeError("judge did not return valid JSON") from last_error
        label = raw["winner"]
        winner = (
            "tie"
            if label == "tie"
            else (candidates[0] if label == "A" else candidates[1])["variant"]
        )
        scores = raw["scores"]
        mapped_scores = {
            candidates[0]["variant"]: scores["A"],
            candidates[1]["variant"]: scores["B"],
        }
        return {
            "case_id": case.case_id,
            "winner": winner,
            "scores": mapped_scores,
            "reason_codes": list(raw.get("reason_codes") or []),
            "judge_model": self._config.judge_model_name or self._config.model_name,
            "judge_prompt_version": JUDGE_PROMPT_VERSION,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }


def parse_judge_json(content: str | None) -> dict[str, Any]:
    """兼容供应商偶发的 Markdown 围栏，同时仍只接受一个 JSON 对象。"""

    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().lower() in {"```", "```json"}:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        payload, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(payload, dict):
        raise TypeError("judge response must be a JSON object")
    return payload


def _validate_judge_payload(payload: Mapping[str, Any]) -> None:
    if payload.get("winner") not in {"A", "B", "tie"}:
        raise ValueError("judge winner is invalid")
    scores = payload.get("scores")
    if not isinstance(scores, dict) or set(scores) != {"A", "B"}:
        raise ValueError("judge scores are invalid")
    expected_dimensions = {"factuality", "completeness", "groundedness", "behavior"}
    for label in ("A", "B"):
        values = scores[label]
        if not isinstance(values, dict) or set(values) != expected_dimensions:
            raise ValueError("judge score dimensions are invalid")
        if any(
            type(value) is not int or value < 0 or value > 4
            for value in values.values()
        ):
            raise ValueError("judge scores must be integers from 0 to 4")


def _judge_prompt(
    case: StageCCase, candidate_a: Mapping[str, Any], candidate_b: Mapping[str, Any]
) -> str:
    def public_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "answer": candidate["answer"],
            "citations": candidate["citations"],
            "answer_incomplete": candidate["answer_incomplete"],
        }

    payload = {
        "question": case.question,
        "reference_answer": case.reference_answer,
        "key_answer_facts": [item.model_dump() for item in case.key_answer_facts],
        "expected_behavior": case.expected_behavior.value,
        "security_expectations": (
            case.security_expectations.model_dump(mode="json")
            if case.security_expectations is not None
            else None
        ),
        "forbidden_tenants": [item.value for item in case.forbidden_tenant_keys],
        "case_notes": case.notes,
        "candidate_A": public_candidate(candidate_a),
        "candidate_B": public_candidate(candidate_b),
    }
    return (
        "比较两个候选最终答案。按事实正确性、关键事实完整性、引用对结论的支持程度、"
        "以及是否符合预期回答/拒答/澄清/跨租户拒绝行为评分。不得因文风或答案长短偏袒。"
        "每个候选的 scores 必须包含 factuality、completeness、groundedness、behavior 四个"
        "0 到 4 的整数。winner 只能是 A、B 或 tie；reason_codes 只写简短稳定代码。\n"
        "输出格式：{\"winner\":\"A|B|tie\",\"scores\":{\"A\":{...},\"B\":{...}},"
        "\"reason_codes\":[\"...\"]}\n评测输入："
        + json.dumps(payload, ensure_ascii=False)
    )


def compile_answer_report(
    cases: Sequence[StageCCase], state: Mapping[str, Any]
) -> dict[str, Any]:
    answers = list(state["answers"].values())
    judgments = list(state["judgments"].values())
    variants: dict[str, dict[str, Any]] = {}
    for variant in ("baseline", "adaptive"):
        selected = [item for item in answers if item["variant"] == variant]
        total_tokens = sum(item["usage"]["total_tokens"] for item in selected)
        judged_scores = [
            sum(
                value
                for value in item["scores"][variant].values()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            )
            for item in judgments
            if variant in item["scores"]
        ]
        variants[variant] = {
            "completed_answers": len(selected),
            "answer_incomplete_count": sum(bool(item["answer_incomplete"]) for item in selected),
            "answers_with_valid_citations": sum(bool(item["citations"]) for item in selected),
            "average_latency_ms": (
                round(sum(item["latency_ms"] for item in selected) / len(selected), 3)
                if selected
                else None
            ),
            "total_answer_model_calls": sum(
                item["model_calls"] for item in selected
            ),
            "total_answer_tokens": total_tokens,
            "pairwise_wins": sum(item["winner"] == variant for item in judgments),
            "average_judge_score": (
                round(sum(judged_scores) / len(judged_scores), 3)
                if judged_scores
                else None
            ),
        }
    case_by_id = {case.case_id: case for case in cases}
    categories: dict[str, dict[str, int]] = {}
    for judgment in judgments:
        category = case_by_id[judgment["case_id"]].category.value
        counters = categories.setdefault(
            category, {"baseline_wins": 0, "adaptive_wins": 0, "ties": 0}
        )
        key = "ties" if judgment["winner"] == "tie" else f"{judgment['winner']}_wins"
        counters[key] += 1
    return {
        "run": {
            "run_id": state["run_id"],
            "fingerprint": state["fingerprint"],
            "metadata": state["metadata"],
        },
        "completion": {
            "case_count": len(cases),
            "expected_answers": len(cases) * 2,
            "completed_answers": len(answers),
            "completed_pairs": len(judgments),
        },
        "variants": variants,
        "pairwise": {
            "baseline_wins": sum(item["winner"] == "baseline" for item in judgments),
            "adaptive_wins": sum(item["winner"] == "adaptive" for item in judgments),
            "ties": sum(item["winner"] == "tie" for item in judgments),
            "by_category": categories,
        },
        "answers": answers,
        "judgments": judgments,
    }


def run_stage_c_answer_ab_eval(
    *,
    database_path: str | Path,
    cases_path: str | Path,
    checkpoint_path: str | Path,
    model_name: str,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """装配真实依赖并运行完整的 48 条最终答案 A/B 对照。"""

    from app.core.config import create_llm_client, get_knowledge_settings
    from app.evals.stage_c.runner import _ensure_fixture_tenants

    root = Path(repo_root).resolve() if repo_root else Path(__file__).resolve().parents[3]
    cases_file = Path(cases_path).resolve()
    cases = load_stage_c_cases(cases_file)
    specs = corpus_specs(root)
    settings = get_knowledge_settings()
    client = create_llm_client()
    services = create_knowledge_services(
        database_path, settings, llm_client=client, model_name=model_name
    )
    _ensure_fixture_tenants(database_path)
    fixture_manager = StageCFixtureManager(
        store=services.store,
        ingestion=services.ingestion,
        loader=DocumentLoader(),
        chunker=KnowledgeChunker(),
        vector_store=services.vector_store,
    )
    metadata = {
        "case_dataset_sha256": sha256_file(cases_file),
        "corpus_sha256": sha256_corpus(spec.path for spec in specs),
        "answer_model": model_name,
        "answer_prompt_version": ANSWER_PROMPT_VERSION,
        "judge_model": model_name,
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "business_tool_names": sorted(_READ_ONLY_BUSINESS_TOOLS),
        "knowledge_tool_name": "search_knowledge",
        "collection_name": settings.qdrant_collection,
        "baseline_rerank_enabled": True,
        "baseline_rerank_model": settings.rerank_model,
        "adaptive_rerank_model": settings.rerank_model,
        "rerank_instruct": settings.rerank_instruct,
        "retrieval_comparison_version": "baseline-rerank-v1-adaptive-rerank-v1",
    }
    checkpoint = AnswerCheckpoint(checkpoint_path, metadata)
    return StageCAnswerABRunner(
        cases=cases,
        services=services,
        client=client,
        checkpoint=checkpoint,
        config=AnswerRunConfig(model_name=model_name),
        fixture_manager=fixture_manager,
        fixture_specs=specs,
    ).run()
