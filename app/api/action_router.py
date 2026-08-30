"""退款/补偿审批与 Run 查询/恢复 HTTP API（设计 13，Task 6）。

路由工厂只消费可信依赖：``ActionWorkflowService`` 与 ``get_current_tenant``
（提供可信 ``TenantContext``）。``organization_id`` 与 ``user_id`` 只来自
租户上下文，绝不来自路径、查询参数或请求体，因此客户端无法通过构造
Approval/Run ID 访问其他企业的资源（跨租户统一表现为 404）。

决定与恢复的 RBAC 由应用服务基于当前 Membership 重新读取；本层只负责
参数结构、状态码与稳定错误码映射。错误响应体只携带稳定错误码与安全消息
（``{"code": ..., "message": ...}``），不泄露 SQL、文件路径、堆栈或 Token。
"""

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.actions.base import ActionError, ApprovalStatus
from app.actions.service import (
    ActionWorkflowService,
    ApprovalChanges,
    http_status_for_action_error,
)
from app.application.organization_service import TenantContext
from app.schemas.action import (
    ApprovalDetailResponse,
    ApprovalListItemResponse,
    DecisionRequest,
    DecisionResponse,
    ResumeResponse,
    RunStatusResponse,
)


def _error_payload(code: str, message: str) -> dict[str, str]:
    """构造只含稳定错误码与安全消息的错误响应体。"""
    return {"code": code, "message": message}


def _raise_action_error(error: ActionError) -> None:
    """按稳定错误码映射 HTTP 状态码并抛出响应。"""
    raise HTTPException(
        status_code=http_status_for_action_error(error),
        detail=_error_payload(error.code, str(error)),
    )


def _blank_resource_id(code: str, resource_name: str) -> HTTPException:
    """构造路径标识为空白时的输入错误响应（设计 16.1：输入错误返回 422）。"""
    return HTTPException(
        status_code=422,
        detail=_error_payload(code, f"{resource_name} 标识不能为空"),
    )


def create_action_router(
    *,
    action_service: ActionWorkflowService,
    get_current_tenant: Callable[..., TenantContext],
) -> APIRouter:
    """装配审批与 Run 的查询、决定与恢复路由。"""
    router = APIRouter(tags=["退款/补偿审批与Run"])

    @router.get("/approvals/", response_model=list[ApprovalListItemResponse])
    def list_approvals(
        status: ApprovalStatus | None = None,
        limit: int = Query(default=20, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        context: TenantContext = Depends(get_current_tenant),
    ) -> list[ApprovalListItemResponse]:
        """分页查询当前企业审批列表（agent 与 admin 均可读取，设计 13.1）。"""
        try:
            items = action_service.list_approvals(
                organization_id=context.organization_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        except ActionError as exc:
            _raise_action_error(exc)
        return [
            ApprovalListItemResponse.from_domain(item)
            for item in items
        ]

    @router.get(
        "/approvals/{approval_id}/",
        response_model=ApprovalDetailResponse,
    )
    def get_approval_detail(
        approval_id: str,
        context: TenantContext = Depends(get_current_tenant),
    ) -> ApprovalDetailResponse:
        """查询审批详情：请求版本、历史版本、Run 状态与决定摘要（设计 13.1）。"""
        approval_id = approval_id.strip()
        if not approval_id:
            raise _blank_resource_id("APPROVAL_NOT_FOUND", "审批")
        try:
            detail = action_service.get_approval_detail(
                organization_id=context.organization_id,
                approval_id=approval_id,
            )
        except ActionError as exc:
            _raise_action_error(exc)
        return ApprovalDetailResponse.from_domain(detail)

    @router.post(
        "/approvals/{approval_id}/decisions/",
        response_model=DecisionResponse,
    )
    def decide_approval(
        approval_id: str,
        request: DecisionRequest,
        response: Response,
        context: TenantContext = Depends(get_current_tenant),
    ) -> DecisionResponse:
        """作出审批决定（仅 admin，设计 13.2）。

        状态码语义：首次决定且自动恢复成功为 201；决定已保存但工作流
        恢复失败为 202（``resume_required=true`` 与稳定错误码）；相同
        决定重复提交为 200；不同决定重复提交为 409。
        """
        approval_id = approval_id.strip()
        if not approval_id:
            raise _blank_resource_id("APPROVAL_NOT_FOUND", "审批")
        try:
            # 先读取既有决定，用于区分「首次决定」与「相同决定重复提交」。
            detail = action_service.get_approval_detail(
                organization_id=context.organization_id,
                approval_id=approval_id,
            )
            was_decided = detail.decision is not None
            outcome = action_service.decide_approval(
                organization_id=context.organization_id,
                approval_id=approval_id,
                decided_by_user_id=context.user_id,
                decision=request.decision,
                comment=request.comment,
                changes=(
                    ApprovalChanges(
                        amount_cents=request.changes.amount_cents,
                        reason_code=request.changes.reason_code,
                        reason_text=request.changes.reason_text,
                        refund_scope=request.changes.refund_scope,
                    )
                    if request.changes is not None
                    else None
                ),
            )
        except ActionError as exc:
            _raise_action_error(exc)
        response.status_code = (
            200 if was_decided else (202 if outcome.resume_required else 201)
        )
        return DecisionResponse.from_outcome(outcome)

    @router.get("/action-runs/{run_id}/", response_model=RunStatusResponse)
    def get_run_status(
        run_id: str,
        context: TenantContext = Depends(get_current_tenant),
    ) -> RunStatusResponse:
        """查询当前企业 Action Run 状态（成员可读，设计 13.3 / 12.4）。"""
        run_id = run_id.strip()
        if not run_id:
            raise _blank_resource_id("RUN_NOT_FOUND", "Run")
        try:
            view = action_service.get_run_status(
                organization_id=context.organization_id,
                run_id=run_id,
            )
        except ActionError as exc:
            _raise_action_error(exc)
        return RunStatusResponse.from_view(view)

    @router.post("/action-runs/{run_id}/resume/", response_model=ResumeResponse)
    def resume_run(
        run_id: str,
        context: TenantContext = Depends(get_current_tenant),
    ) -> ResumeResponse:
        """显式恢复 Run（仅 admin，设计 13.3 / 11.5）。

        恢复请求本身幂等：Run 已成功时返回现有稳定结果；非法状态返回
        409 与稳定错误码；checkpoint 或执行基础设施不可用时返回 503，
        业务事实（含审批决定）保持不变，可稍后重试。
        """
        run_id = run_id.strip()
        if not run_id:
            raise _blank_resource_id("RUN_NOT_FOUND", "Run")
        try:
            outcome = action_service.resume_run(
                organization_id=context.organization_id,
                run_id=run_id,
                requested_by_user_id=context.user_id,
            )
        except ActionError as exc:
            _raise_action_error(exc)
        if not outcome.resume_ok:
            raise HTTPException(
                status_code=503,
                detail=_error_payload(
                    outcome.error_code or "CHECKPOINT_UNAVAILABLE",
                    "工作流恢复失败，决定仍然有效，可稍后重试",
                ),
            )
        return ResumeResponse.from_outcome(outcome)

    return router
