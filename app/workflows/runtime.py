"""LangGraph 动作工作流运行器：invoke、interrupt 结果归一化与 resume。

实现设计 11.5 与 Task 3 的 ``ActionWorkflowRunner`` 协议：start/resume
使用同一 ``thread_id``（来自 ``action_runs.thread_id``，不接受 HTTP Body、
模型参数或查询参数覆盖）；把 LangGraph 基础设施异常归一化为稳定的
``ActionError`` 错误码。

恢复语义：``thread_id`` 存在中断 checkpoint 时通过 ``Command(resume=...)``
携带 ``decision_id`` 继续；无 checkpoint（首次启动失败重试、可重试失败
恢复、checkpoint 丢失）时从 START 重新运行，由 ``load_run`` 依据业务库
中的 Run 状态与持久化决定路由。
"""

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.actions.base import (
    ActionError,
    ActionRunStatus,
    ActionStore,
    CheckpointUnavailableError,
)
from app.actions.service import ActionWorkflowRunner
from app.core.config import get_action_workflow_version


class LangGraphActionWorkflowRunner:
    """确定性动作工作流运行器（LangGraph 实现）。

    只负责按 Run 推进确定性状态机：首次启动、审批后自动恢复、显式恢复
    与可重试失败恢复；不调用 LLM、不选择工具、不判断调用者权限。
    """

    def __init__(
        self,
        *,
        store: ActionStore,
        graph: CompiledStateGraph,
    ):
        self._store = store
        self._graph = graph

    def start(self, *, organization_id: str, run_id: str) -> None:
        """首次启动：从当前 Run 位置运行图直到 interrupt 或终态。

        业务事务提交后调用；图运行到审批中断或终态即返回，调用方
        重新读取 Run 获取最新状态。
        """
        self._invoke(
            organization_id=organization_id,
            run_id=run_id,
            resume_decision_id=None,
        )

    def resume(self, *, organization_id: str, run_id: str) -> None:
        """恢复：从既有 checkpoint 继续运行图（含首次启动失败后的重试）。

        等待审批的 Run 处于中断点，需要携带持久化的 ``decision_id``
        通过 ``Command(resume=...)`` 推进；其余状态（queued/running/
        可重试失败）没有待恢复的中断，从 START 重新运行并按业务库状态
        路由。``thread_id`` 始终取自 ``action_runs.thread_id``。
        """
        run = self._store.get_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        resume_decision_id = None
        if run.status is ActionRunStatus.AWAITING_APPROVAL:
            # 中断点在审批节点：读取持久化决定作为恢复值；
            # 决定缺失时仍显式恢复，由图的执行分支失败关闭。
            if run.proposal_id is not None:
                proposal = self._store.get_proposal(
                    organization_id=organization_id,
                    proposal_id=run.proposal_id,
                )
                approval = self._store.get_approval_by_proposal(
                    organization_id=organization_id,
                    proposal_id=proposal.proposal_id,
                )
                decision = self._store.get_decision(
                    organization_id=organization_id,
                    approval_id=approval.approval_id,
                )
                if decision is not None:
                    resume_decision_id = decision.decision_id
        self._invoke(
            organization_id=organization_id,
            run_id=run_id,
            resume_decision_id=resume_decision_id,
        )

    # —— 内部实现 ——

    def _invoke(
        self,
        *,
        organization_id: str,
        run_id: str,
        resume_decision_id: str | None,
    ) -> None:
        """执行一次图调用，并把基础设施异常归一化为稳定错误码。"""
        run = self._store.get_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        # 设计 11.4：workflow 版本写入 checkpoint 配置，为将来图拓扑或
        # State schema 不兼容升级提供迁移判定依据（本阶段固定 v1）。
        config = {
            "configurable": {
                "thread_id": run.thread_id,
                "workflow_version": get_action_workflow_version(),
            }
        }
        try:
            if resume_decision_id is None:
                self._graph.invoke(
                    {
                        "run_id": run_id,
                        "organization_id": organization_id,
                    },
                    config,
                )
            else:
                self._graph.invoke(
                    Command(
                        resume={"decision_id": resume_decision_id},
                        update={
                            "run_id": run_id,
                            "organization_id": organization_id,
                        },
                    ),
                    config,
                )
        except ActionError:
            # 领域稳定错误码原样传播，由应用服务统一映射。
            raise
        except Exception as exc:
            # 设计 16.5：checkpoint 或工作流基础设施不可用属于可重试
            # 基础设施失败，归一化为稳定错误码，不泄露内部细节。
            raise CheckpointUnavailableError(
                "checkpoint 或工作流基础设施不可用"
            ) from exc
