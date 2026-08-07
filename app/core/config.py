from openai import OpenAI

import os
from dataclasses import dataclass

MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

def create_llm_client()-> OpenAI:
  return OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
  )

def get_chat_db_path()->str:
  return os.getenv("CHAT_DB_PATH","chat_history.db")

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
    dashscope_api_key: str
    dashscope_base_url: str
    qdrant_url: str
    qdrant_collection: str = "supportpilot_knowledge_te4_1024_v1"
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024


def get_knowledge_settings() -> KnowledgeSettings:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required")
    return KnowledgeSettings(
        dashscope_api_key=api_key,
        dashscope_base_url=os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/api/v1",
        ).rstrip("/"),
        qdrant_url=os.getenv(
            "QDRANT_URL", "http://localhost:6333"
        ).rstrip("/"),
    )