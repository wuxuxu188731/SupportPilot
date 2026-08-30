"""两个提案工具与只读状态工具的 Pydantic 参数模型（设计 12.2 / 12.3 / 12.4）。

参数模型使用 ``extra='forbid'`` 并去除首尾空白；金额使用严格整数校验，
浮点、字符串和布尔值一律拒绝（设计 14：金额始终使用整数分，禁止浮点金额）。
``organization_id``、``user_id``、``role``、``conversation_id``、``turn_id``
与 ``thread_id`` 都不允许出现在模型可见的参数中，因此本模块不定义这些字段。
"""

from pydantic import BaseModel, ConfigDict, Field

from app.actions.base import (
    CompensationReasonCode,
    RefundReasonCode,
    RefundScope,
)

# 订单号等业务编号的最大长度，与客服工具保持一致
BUSINESS_NUMBER_MAX_LENGTH = 100
# 原因说明的最大长度
REASON_TEXT_MAX_LENGTH = 2000


class ActionToolArguments(BaseModel):
    """动作工具的公共参数配置。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


class ProposeRefundArguments(ActionToolArguments):
    """创建退款提案的参数（设计 12.2）。"""

    order_no: str = Field(  # 订单号，必须先通过 get_order 核实订单事实
        min_length=1,
        max_length=BUSINESS_NUMBER_MAX_LENGTH,
    )
    amount_cents: int = Field(  # 退款金额（分），严格正整数，禁止浮点
        gt=0,
        strict=True,
    )
    currency: str = Field(  # 币种，必须等于订单币种
        min_length=1,
        max_length=16,
    )
    refund_scope: RefundScope  # 退款范围：full 全额 / partial 部分
    reason_code: RefundReasonCode  # 退款原因码，使用固定枚举
    reason_text: str = Field(  # 客服给出的补充说明，other 原因必须详细说明
        min_length=1,
        max_length=REASON_TEXT_MAX_LENGTH,
    )


class ProposeCompensationArguments(ActionToolArguments):
    """创建优惠券补偿提案的参数（设计 12.3）。"""

    order_no: str = Field(  # 订单号，必须先通过 get_order 核实订单事实
        min_length=1,
        max_length=BUSINESS_NUMBER_MAX_LENGTH,
    )
    amount_cents: int = Field(  # 补偿金额（分），严格正整数，禁止浮点
        gt=0,
        strict=True,
    )
    currency: str = Field(  # 币种，必须等于订单币种
        min_length=1,
        max_length=16,
    )
    reason_code: CompensationReasonCode  # 补偿原因码，使用固定枚举
    reason_text: str = Field(  # 客服给出的补充说明，other 原因必须详细说明
        min_length=1,
        max_length=REASON_TEXT_MAX_LENGTH,
    )


class GetActionStatusArguments(ActionToolArguments):
    """查询动作 Run 状态的参数（设计 12.4）。"""

    run_id: str = Field(  # 用户可见的 Run 标识，只作为租户限定查询键
        min_length=1,
        max_length=100,
    )


ACTION_ARGUMENT_MODELS = {
    "propose_refund": ProposeRefundArguments,
    "propose_compensation": ProposeCompensationArguments,
    "get_action_status": GetActionStatusArguments,
}
