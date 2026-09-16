from pydantic import BaseModel, ConfigDict, Field


class SearchKnowledgeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(
        min_length=1,
        max_length=2000,
        description="直接用于传统 RAG 检索的知识库查询内容",
    )
