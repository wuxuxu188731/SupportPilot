from typing import Any

from pydantic import ValidationError

from app.agent.invocation_context import (
    AgentInvocationContext,
    tenant_of,
)
from app.application.organization_service import TenantContext
from app.knowledge.service import AdaptiveKnowledgeSearchService
from app.tools.knowledge_arguments import SearchKnowledgeArguments
from app.tools.knowledge_definitions import get_knowledge_tool_definitions
from app.tools.support_results import tool_failure


class KnowledgeToolGateway:
    def __init__(self, *, service: AdaptiveKnowledgeSearchService) -> None:
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
        used = False

        def search_knowledge(**arguments: Any) -> dict:
            nonlocal used
            try:
                parsed = SearchKnowledgeArguments.model_validate(arguments)
            except ValidationError:
                return tool_failure(
                    code="INVALID_ARGUMENTS",
                    message="tool arguments are invalid",
                )
            if used:
                return tool_failure(
                    code="SEARCH_BUDGET_EXCEEDED",
                    message="knowledge search budget exceeded",
                )
            used = True
            result = self._service.search(
                organization_id=tenant.organization_id,
                question=parsed.question,
                conversation_id=None,
            )
            return result.public_dict()

        return {"search_knowledge": search_knowledge}
