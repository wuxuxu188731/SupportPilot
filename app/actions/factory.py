"""动作工作流组件装配（设计 7 模块边界）。

本模块负责把 Store、执行器（以及后续 Task 5 的 LangGraph 工作流）装配为
可直接使用的组件组合，隔离组装细节，方便应用入口（``main.py``）与测试
统一构造。
"""

from app.actions.base import ActionStore
from app.actions.executor import (
    IdempotentActionExecutor,
    RetryableFailureInjector,
    SimulatedCompensationAdapter,
    SimulatedRefundAdapter,
)


def build_action_executor(
    store: ActionStore,
    *,
    refund_adapter: SimulatedRefundAdapter | None = None,
    compensation_adapter: SimulatedCompensationAdapter | None = None,
    failure_injector: RetryableFailureInjector | None = None,
) -> IdempotentActionExecutor:
    """装配幂等模拟执行器。

    未显式提供适配器时使用默认模拟适配器；传入 ``failure_injector`` 时
    同时注入到退款与补偿适配器，用于测试或演练可重试失败路径。
    """
    return IdempotentActionExecutor(
        store=store,
        refund_adapter=refund_adapter
        or SimulatedRefundAdapter(failure_injector=failure_injector),
        compensation_adapter=compensation_adapter
        or SimulatedCompensationAdapter(failure_injector=failure_injector),
    )
