"""演示业务数据初始化接口（前端「生成测试数据」按钮的服务端入口）。

只做一件事：把请求交给现成的 :class:`DemoDataSeeder`。租户与调用者身份全部
来自认证链（``X-Organization-ID`` 头 + Bearer Token），**请求体不接受任何
企业、用户或角色字段**；能否初始化由 seeder 自己按数据库中的成员角色判定，
客服调用得到 403。
"""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.application.organization_service import TenantContext
from app.demo_data.seeder import (
    DemoDataConflictError,
    DemoDataSeeder,
    DemoSeedAdminRequiredError,
    DemoSeedResult,
)


def serialize_seed_result(result: DemoSeedResult) -> dict[str, Any]:
    """把初始化结果投影成前端可直接展示的结构。

    ``counts`` 只含 ``COUNTED_TABLES`` 中的固定表名，数值为本次真正新增的
    行数：首次初始化为满额，重复点击除业务写入外都应为 0，前端据此区分
    「新增」与「已存在」。
    """
    return {
        "organization_id": result.organization_id,
        "reference_at": result.reference_at,
        "counts": dict(result.created_counts),
        "customer_nos": list(result.customer_nos),
        "order_nos": list(result.order_nos),
        "shipment_nos": list(result.shipment_nos),
        "ticket_nos": list(result.ticket_nos),
    }


def create_demo_data_router(
    *,
    seeder: DemoDataSeeder,
    get_current_tenant: Callable[..., TenantContext],
) -> APIRouter:
    router = APIRouter(
        prefix="/demo-data",
        tags=["demo-data"],
    )

    @router.post("/", status_code=201)
    def generate_demo_data(
        tenant: TenantContext = Depends(get_current_tenant),
    ) -> dict[str, Any]:
        """为当前企业生成演示业务数据（幂等，可重复点击）。"""
        try:
            result = seeder.seed(
                organization_id=tenant.organization_id,
                actor_user_id=tenant.user_id,
                # 按钮的语义是「现在就能拿去对话」，因此默认把会过期的日期
                # 对齐到当前时间；批量脚本仍走 CLI 的默认值（不刷新）。
                refresh_times=True,
            )
        except DemoSeedAdminRequiredError as exc:
            raise HTTPException(
                status_code=403,
                detail="organization admin required",
            ) from exc
        except DemoDataConflictError as exc:
            # 演示数据被业务操作改过（例如订单已退款）：如实返回冲突，
            # 不静默覆盖参与过审批的业务记录。
            raise HTTPException(
                status_code=409,
                detail=str(exc),
            ) from exc
        return serialize_seed_result(result)

    return router
