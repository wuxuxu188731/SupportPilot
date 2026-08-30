"""提案与状态工具参数模型测试（设计 12.2 / 12.3 / 12.4 / 18.6）。

覆盖：合法参数通过；未知字段、非法枚举、空白必填字段被拒绝；金额严格
整数（浮点、字符串、布尔值一律 422 语义）；模型可见 Schema 不包含任何
租户、会话、回合或 thread_id 字段。
"""

import json

import pytest
from pydantic import ValidationError

from app.tools.action_arguments import (
    ACTION_ARGUMENT_MODELS,
    GetActionStatusArguments,
    ProposeCompensationArguments,
    ProposeRefundArguments,
)
from app.tools.action_definitions import get_action_tool_definitions


def test_propose_refund_accepts_valid_arguments():
    # 保护行为：合法退款参数通过校验并去除首尾空白。
    parsed = ProposeRefundArguments.model_validate(
        {
            "order_no": " ORD-A-001 ",
            "amount_cents": 6000,
            "currency": "CNY",
            "refund_scope": "partial",
            "reason_code": "quality_issue",
            "reason_text": " 商品存在质量问题 ",
        }
    )
    assert parsed.order_no == "ORD-A-001"
    assert parsed.amount_cents == 6000
    assert parsed.refund_scope.value == "partial"
    assert parsed.reason_text == "商品存在质量问题"


@pytest.mark.parametrize(
    "invalid_amount",
    [0, -1, 800.0, "800", True],
)
def test_propose_refund_rejects_non_strict_integer_amount(invalid_amount):
    # 边界情况：金额必须是严格正整数分，浮点、字符串、布尔与非正数一律拒绝。
    with pytest.raises(ValidationError):
        ProposeRefundArguments.model_validate(
            {
                "order_no": "ORD-A-001",
                "amount_cents": invalid_amount,
                "currency": "CNY",
                "refund_scope": "partial",
                "reason_code": "quality_issue",
                "reason_text": "商品存在质量问题",
            }
        )


def test_propose_refund_rejects_unknown_fields_and_bad_enums():
    # 边界情况：未知字段与非法枚举值被拒绝，不静默忽略。
    with pytest.raises(ValidationError):
        ProposeRefundArguments.model_validate(
            {
                "order_no": "ORD-A-001",
                "amount_cents": 6000,
                "currency": "CNY",
                "refund_scope": "partial",
                "reason_code": "quality_issue",
                "reason_text": "商品存在质量问题",
                "organization_id": "org-hacked",
            }
        )
    with pytest.raises(ValidationError):
        ProposeRefundArguments.model_validate(
            {
                "order_no": "ORD-A-001",
                "amount_cents": 6000,
                "currency": "CNY",
                "refund_scope": "full",
                "reason_code": "customer_request",
                "reason_text": "客户要求退款",
            }
        )
    with pytest.raises(ValidationError):
        ProposeRefundArguments.model_validate(
            {
                "order_no": "ORD-A-001",
                "amount_cents": 6000,
                "currency": "CNY",
                "refund_scope": "everything",
                "reason_code": "quality_issue",
                "reason_text": "商品存在质量问题",
            }
        )


def test_propose_compensation_accepts_valid_arguments():
    # 保护行为：合法补偿参数通过校验，原因码使用补偿固定枚举。
    parsed = ProposeCompensationArguments.model_validate(
        {
            "order_no": "ORD-A-001",
            "amount_cents": 3000,
            "currency": "CNY",
            "reason_code": "delayed_shipment",
            "reason_text": "发货延迟补偿",
        }
    )
    assert parsed.amount_cents == 3000
    assert parsed.reason_code.value == "delayed_shipment"


def test_propose_compensation_rejects_refund_enum_and_unknown_fields():
    # 边界情况：补偿原因使用退款枚举或携带未知字段时被拒绝。
    with pytest.raises(ValidationError):
        ProposeCompensationArguments.model_validate(
            {
                "order_no": "ORD-A-001",
                "amount_cents": 3000,
                "currency": "CNY",
                "reason_code": "quality_issue",
                "reason_text": "错误原因码",
            }
        )
    with pytest.raises(ValidationError):
        ProposeCompensationArguments.model_validate(
            {
                "order_no": "ORD-A-001",
                "amount_cents": 3000,
                "currency": "CNY",
                "reason_code": "delayed_shipment",
                "reason_text": "发货延迟补偿",
                "coupon_valid_days": 30,
            }
        )


def test_get_action_status_requires_non_blank_run_id():
    # 边界情况：run_id 为空字符串时被拒绝。
    with pytest.raises(ValidationError):
        GetActionStatusArguments.model_validate({"run_id": "  "})


def test_action_definitions_expose_exactly_three_tools():
    # 保护行为：模型可见定义恰好是三个工具，
    # 不暴露审批、恢复或执行工具（设计 12.6）。
    definitions = get_action_tool_definitions()
    assert [
        item["function"]["name"] for item in definitions
    ] == [
        "propose_refund",
        "propose_compensation",
        "get_action_status",
    ]


def test_action_definitions_do_not_leak_trusted_fields():
    # 边界情况：模型可见 Schema 不包含租户、会话、回合或 thread_id 字段
    # （设计 12.1 / 12.6）。
    forbidden = {
        "organization_id",
        "user_id",
        "role",
        "conversation_id",
        "turn_id",
        "thread_id",
        "idempotency_key",
        "proposal_id",
        "approval_id",
    }
    for definition in get_action_tool_definitions():
        serialized = json.dumps(definition, ensure_ascii=False)
        for field in forbidden:
            assert field not in serialized, (
                f"{definition['function']['name']} 泄露了 {field}"
            )


def test_action_argument_models_are_extra_forbid():
    # 保护行为：所有动作工具参数模型都禁止未知字段（设计 14）。
    for model in ACTION_ARGUMENT_MODELS.values():
        assert model.model_config.get("extra") == "forbid"
