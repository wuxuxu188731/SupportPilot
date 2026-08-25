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
                    "Search the current organization's trusted policy knowledge."
                ),
                "parameters": schema,
            },
        }
    ]
