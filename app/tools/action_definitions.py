"""提案与状态工具的描述（设计 12.2 / 12.3 / 12.4 / 12.6）。

描述文本承载调用规则：只有用户明确提出诉求时才创建提案、创建前先查询
订单、成功只表示「等待审批」；审批、恢复与执行不暴露给模型。
"""

from copy import deepcopy

from app.tools.action_arguments import ACTION_ARGUMENT_MODELS


TOOL_DESCRIPTIONS = {
    "propose_refund": (
        "为当前企业、当前订单创建退款提案并提交人工审批。只有用户明确"
        "提出退款诉求时才调用；调用前必须先通过 get_order 取得订单事实；"
        "涉及政策解释时先调用 search_knowledge，但政策证据不能代替审批。"
        "成功后提案处于 awaiting_approval 等待审批，不是退款成功。"
    ),
    "propose_compensation": (
        "为当前企业、当前订单创建优惠券补偿提案并提交人工审批。只有用户"
        "明确提出补偿诉求时才调用；调用前必须先通过 get_order 取得订单"
        "事实；资格信息（会员等级、不可抗力、用户责任等）缺失时必须在"
        "reason_text 中标注「待人工核实」。成功后提案处于 awaiting_approval"
        "等待审批，不是补偿成功。"
    ),
    "get_action_status": (
        "只读查询当前企业内退款/补偿动作 Run 的状态、审批与执行结果；"
        "不能审批、恢复或执行动作。"
    ),
}


def get_action_tool_definitions() -> list[dict]:
    """返回三个动作工具的模型可见定义（不含任何租户/会话/回合字段）。"""
    definitions: list[dict] = []
    for tool_name, argument_model in ACTION_ARGUMENT_MODELS.items():
        parameters = deepcopy(argument_model.model_json_schema())
        parameters.pop("title", None)
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": TOOL_DESCRIPTIONS[tool_name],
                    "parameters": parameters,
                },
            }
        )
    return definitions
