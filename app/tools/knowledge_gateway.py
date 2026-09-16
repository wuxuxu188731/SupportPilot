from typing import Any

from pydantic import ValidationError

from app.agent.invocation_context import (
    AgentInvocationContext,
    tenant_of,
)
from app.application.organization_service import TenantContext
from app.knowledge.retrieval import BaselineKnowledgeSearchService
from app.knowledge.results import BaselineSearchResult
from app.tools.knowledge_arguments import SearchKnowledgeArguments
from app.tools.knowledge_definitions import get_knowledge_tool_definitions
from app.tools.support_results import tool_failure


class KnowledgeToolGateway:
    def __init__(self, *, service: BaselineKnowledgeSearchService) -> None:
        self._service = service

    @property
    def definitions(self) -> list[dict]:
        return get_knowledge_tool_definitions()

    def bind(
        self,
        *,
        context: AgentInvocationContext | TenantContext,
    ):
        # 设计 12.1：知识 Gateway 只读取可信租户部分。
        tenant = tenant_of(context)

        def search_knowledge(**arguments: Any) -> dict:
            try:
                parsed = SearchKnowledgeArguments.model_validate(arguments)
            except ValidationError:
                return tool_failure(
                    code="INVALID_ARGUMENTS",
                    message="tool arguments are invalid",
                )
            result = self._service.search(
                organization_id=tenant.organization_id,
                question=parsed.question,
                conversation_id=None,
            )
            return _baseline_tool_payload(result)

        return {"search_knowledge": search_knowledge}


def _baseline_tool_payload(result: BaselineSearchResult) -> dict:
    """将传统 RAG 结果转换为 Agent 引用校验所需的稳定工具结构。"""
    payload = result.public_dict()
    summary = payload["retrieval_summary"]
    data = {
        "result_code": (
            "KNOWLEDGE_FOUND"
            if summary["evidence_status"] == "sufficient"
            else "INSUFFICIENT_EVIDENCE"
        ),
        "strategy": summary["strategy"],
        "evidence_status": summary["evidence_status"],
        "citations": payload["citations"],
        "retrieval_summary": summary,
    }
    if payload["ok"]:
        return {"ok": True, "data": data}
    return {
        "ok": False,
        "error": payload["error"],
        "data": data,
    }
