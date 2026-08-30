"""Agent 请求级调用上下文（设计 12.1）。

``AgentInvocationContext`` 携带服务端认证链产生的可信租户、当前会话标识
与当前聊天回合标识，通过 ``ChatService -> CustomerSupportAgentRunner ->
Gateway.bind()`` 的请求级闭包传递。会话与回合标识不是模型工具参数，
模型不可见、不可构造、不可覆盖。

现有客服与知识 Gateway 只读取其中的 ``tenant`` 部分；动作工具 Gateway
同时读取可信租户、``conversation_id`` 与 ``turn_id``。
"""

from dataclasses import dataclass

from app.application.organization_service import TenantContext


@dataclass(frozen=True)
class AgentInvocationContext:
    """一次 Agent 调用的请求级上下文。"""

    tenant: TenantContext  # 当前认证用户及企业身份，由服务端认证链产生
    conversation_id: str  # 当前会话标识，已经由 ChatService 验证归属
    turn_id: str  # 当前聊天回合标识，由服务端生成，用于追踪及抑制同回合重复提案


def tenant_of(
    context: "AgentInvocationContext | TenantContext",
) -> TenantContext:
    """从绑定上下文提取可信租户部分。

    兼容旧调用方直接传入 ``TenantContext`` 的场景（例如既有测试与评估
    脚本）；生产链路统一传递 ``AgentInvocationContext``，本函数保证
    客服与知识 Gateway 只把租户部分传给原应用服务（设计 12.1）。
    """
    if isinstance(context, AgentInvocationContext):
        return context.tenant
    return context
