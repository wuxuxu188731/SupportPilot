from openai import OpenAI

import os

MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

def create_llm_client()-> OpenAI:
  return OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com"
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