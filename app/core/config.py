from openai import OpenAI
from dotenv import load_dotenv

import os
import math
from dataclasses import dataclass

load_dotenv()

MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

def create_llm_client()-> OpenAI:
  return OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
  )

def get_chat_db_path()->str:
  return os.getenv("CHAT_DB_PATH","chat_history.db")

def get_checkpoint_db_path()->str:
  """返回 LangGraph checkpoint 数据库路径。

  设计 11.4：checkpoint 使用独立 SQLite 文件，避免与 Alembic 管理的
  业务表混杂。
  """
  return os.getenv("LANGGRAPH_CHECKPOINT_DB_PATH","langgraph_checkpoint.db")

def get_action_workflow_version()->str:
  """返回动作工作流版本标识。

  设计 11.4：图拓扑与 State schema 的不兼容修改必须伴随 workflow
  版本迁移策略；本阶段固定为 refund-compensation-v1。
  """
  return os.getenv("ACTION_WORKFLOW_VERSION","refund-compensation-v1")

def get_auth_secret_key()->str:
  secret_key = os.getenv("AUTH_SECRET_KEY","")
  if len(secret_key)<32 :
    raise RuntimeError("AUTH_SECRET_KEY must contain at least 32 characters")
  return secret_key

def get_access_token_ttl_seconds()->int:
  raw_value = os.getenv("ACCESS_TOKEN_TTL_SECONDS", "1800")
  try:
    ttl_seconds = int(raw_value)
  except ValueError as exc:
    raise RuntimeError(
      "ACCESS_TOKEN_TTL_SECONDS must be an integer"
    ) from exc
  if ttl_seconds <= 0:
    raise RuntimeError(
      "ACCESS_TOKEN_TTL_SECONDS must be positive"
    )
  return ttl_seconds


@dataclass(frozen=True)
class KnowledgeSettings:
    dashscope_api_key: str  # DashScope 服务鉴权密钥，仅从服务端环境变量读取
    dashscope_base_url: str  # Embedding 模型使用的 DashScope API 基础地址
    qdrant_url: str  # Qdrant 向量数据库的服务地址
    qdrant_collection: str = "supportpilot_knowledge_te4_1024_v1"  # 知识向量集合名称
    embedding_model: str = "text-embedding-v4"  # 文档与查询使用的向量模型名称
    embedding_dimensions: int = 1024  # 稠密向量维度，必须与集合定义一致
    min_fused_score: float = 0.0  # Agentic Search 证据进入评估器的最低融合分数
    search_timeout_seconds: float = 30.0  # 单次 Agentic Search 的总超时时间（秒）
    rerank_model: str = "qwen3-rerank"  # Baseline 与 Agentic Search 共享的重排序模型名称
    rerank_base_url: str = (  # 百炼北京业务空间的重排序 API 基础地址
        "https://ws-tocwkn1wc3xhur1f.cn-beijing.maas.aliyuncs.com/api/v1"
    )
    rerank_instruct: str = (  # 英文重排序指令，默认强调寻找能回答问题的段落
        "Given a web search query, retrieve relevant passages that answer the query."
    )


def get_knowledge_settings() -> KnowledgeSettings:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required")
    min_fused_score = _finite_float(
        "KNOWLEDGE_MIN_FUSED_SCORE", default="0.0"
    )
    search_timeout_seconds = _finite_float(
        "KNOWLEDGE_SEARCH_TIMEOUT_SECONDS", default="30"
    )
    if min_fused_score < 0:
        raise RuntimeError("KNOWLEDGE_MIN_FUSED_SCORE must be non-negative")
    if search_timeout_seconds <= 0:
        raise RuntimeError(
            "KNOWLEDGE_SEARCH_TIMEOUT_SECONDS must be positive"
        )
    return KnowledgeSettings(
        dashscope_api_key=api_key,
        dashscope_base_url=os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/api/v1",
        ).rstrip("/"),
        qdrant_url=os.getenv(
            "QDRANT_URL", "http://localhost:6333"
        ).rstrip("/"),
        min_fused_score=min_fused_score,
        search_timeout_seconds=search_timeout_seconds,
        rerank_base_url=os.getenv(
            "KNOWLEDGE_RERANK_BASE_URL",
            "https://ws-tocwkn1wc3xhur1f.cn-beijing.maas.aliyuncs.com/api/v1",
        ).rstrip("/"),
        rerank_instruct=os.getenv(
            "KNOWLEDGE_RERANK_INSTRUCT",
            "Given a web search query, retrieve relevant passages that answer the query.",
        ).strip(),
    )


def _finite_float(name: str, *, default: str) -> float:
    raw_value = os.getenv(name, default)
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        raise RuntimeError(f"{name} must be finite")
    return value
