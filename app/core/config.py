from openai import OpenAI

import os

MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

def create_llm_client()-> OpenAI:
  return OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com"
  )