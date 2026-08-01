from copy import deepcopy

from app.tools.support_arguments import SUPPORT_ARGUMENT_MODELS


TOOL_DESCRIPTIONS = {
    "get_order": (
        "查询当前企业内的订单和对应客户信息。"
    ),
    "get_logistics": (
        "查询当前企业内订单的物流状态；订单存在但未发货时"
        "会返回 not_created。"
    ),
    "create_ticket": (
        "为当前企业创建客服工单；customer_no 和 order_no "
        "至少提供一个，同时提供时必须属于同一客户。"
    ),
    "add_ticket_note": (
        "以当前登录客服身份为当前企业的工单添加内部备注。"
    ),
}


def get_support_tool_definitions() -> list[dict]:
    definitions: list[dict] = []
    for tool_name, argument_model in (
        SUPPORT_ARGUMENT_MODELS.items()
    ):
        parameters = deepcopy(
            argument_model.model_json_schema()
        )
        parameters.pop("title", None)
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": (
                        TOOL_DESCRIPTIONS[tool_name]
                    ),
                    "parameters": parameters,
                },
            }
        )
    return definitions