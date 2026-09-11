from pathlib import Path

import pytest

from app.core.config import (
  DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
  get_access_token_ttl_seconds,
  get_auth_secret_key,
  get_knowledge_settings,
)


#验证get_auth_secret_key函数返回的密钥足够长
def test_auth_secret_is_required_and_long(monkeypatch):
  monkeypatch.delenv(name="AUTH_SECRET_KEY",raising=False)
  with pytest.raises(RuntimeError):
    get_auth_secret_key()

  monkeypatch.setenv(name="AUTH_SECRET_KEY",value="too short")
  with pytest.raises(RuntimeError):
    get_auth_secret_key()


#验证get_access_token_ttl_seconds函数里面从环境变量里面拿到的ttl_seconds必须是正整数
def test_token_ttl_must_be_positive_integer(monkeypatch):
  monkeypatch.delenv(name="ACCESS_TOKEN_TTL_SECONDS",raising=False)
  monkeypatch.setenv(name="ACCESS_TOKEN_TTL_SECONDS",value="-100")
  with pytest.raises(RuntimeError):
    get_access_token_ttl_seconds()

  monkeypatch.delenv(name="ACCESS_TOKEN_TTL_SECONDS",raising=False)
  monkeypatch.setenv(name="ACCESS_TOKEN_TTL_SECONDS",value="abc")
  with pytest.raises(RuntimeError):
    get_access_token_ttl_seconds()

  monkeypatch.delenv(name="ACCESS_TOKEN_TTL_SECONDS",raising=False)
  monkeypatch.setenv(name="ACCESS_TOKEN_TTL_SECONDS",value="1800")
  assert get_access_token_ttl_seconds()==1800


# 保护行为：未配置 ACCESS_TOKEN_TTL_SECONDS 时，登录令牌默认有效期为 1 周
# （604800 秒），避免开发者本地因默认值过短而频繁掉线。
def test_token_ttl_defaults_to_one_week(monkeypatch):
  monkeypatch.delenv(name="ACCESS_TOKEN_TTL_SECONDS",raising=False)
  assert get_access_token_ttl_seconds()==604800


# 边界情况：仓库 .env.example 给出的示例值必须与代码默认值一致，
# 否则按示例配置部署会与本地默认行为产生静默差异。
def test_env_example_ttl_matches_code_default():
  example_text = (
    Path(__file__).resolve().parents[2].joinpath(".env.example").read_text(encoding="utf-8")
  )
  expected_line = f"ACCESS_TOKEN_TTL_SECONDS={DEFAULT_ACCESS_TOKEN_TTL_SECONDS}"
  assert expected_line in example_text


def test_knowledge_settings_require_dashscope_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        get_knowledge_settings()


def test_knowledge_settings_have_fixed_vector_contract(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.test:6333")

    settings = get_knowledge_settings()

    assert settings.embedding_model == "text-embedding-v4"
    assert settings.embedding_dimensions == 1024
    assert settings.qdrant_collection == (
        "supportpilot_knowledge_te4_1024_v1"
    )
    assert settings.qdrant_url == "http://qdrant.test:6333"


def test_stage_b_settings_are_server_owned(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "key")
    monkeypatch.setenv("KNOWLEDGE_MIN_FUSED_SCORE", "0.25")
    monkeypatch.setenv("KNOWLEDGE_SEARCH_TIMEOUT_SECONDS", "15")

    settings = get_knowledge_settings()

    assert settings.min_fused_score == 0.25
    assert settings.search_timeout_seconds == 15.0


# 保护行为：重排序默认使用指定的北京业务空间和英文问答检索策略。
def test_rerank_settings_use_workspace_and_qa_instruction(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "key")
    monkeypatch.delenv("KNOWLEDGE_RERANK_BASE_URL", raising=False)
    monkeypatch.delenv("KNOWLEDGE_RERANK_INSTRUCT", raising=False)

    settings = get_knowledge_settings()

    assert settings.rerank_model == "qwen3-rerank"
    assert settings.rerank_base_url == (
        "https://ws-tocwkn1wc3xhur1f.cn-beijing.maas.aliyuncs.com/api/v1"
    )
    assert settings.rerank_instruct == (
        "Given a web search query, retrieve relevant passages that answer the query."
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KNOWLEDGE_MIN_FUSED_SCORE", "nan"),
        ("KNOWLEDGE_MIN_FUSED_SCORE", "-0.1"),
        ("KNOWLEDGE_SEARCH_TIMEOUT_SECONDS", "0"),
    ],
)
def test_stage_b_settings_reject_invalid_numbers(monkeypatch, name, value):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "key")
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError):
        get_knowledge_settings()
