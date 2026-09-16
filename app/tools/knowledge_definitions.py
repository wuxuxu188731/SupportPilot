from copy import deepcopy

from app.tools.knowledge_arguments import SearchKnowledgeArguments


def get_knowledge_tool_definitions() -> list[dict]:
    schema = deepcopy(SearchKnowledgeArguments.model_json_schema())
    schema.pop("title", None)
    return [
        {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": (
                    "检索当前企业可信知识库。将你要查询的内容直接作为 question；"
                    "简单问题调用 1 次，涉及多个独立主题或条件的复杂问题可拆分为"
                    "不同 query 调用 2-3 次，再综合各次检索结果回答。"
                ),
                "parameters": schema,
            },
        }
    ]
