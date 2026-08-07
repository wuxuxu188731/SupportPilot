import pytest

from app.core.config import (
  get_access_token_ttl_seconds,
  get_auth_secret_key
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
  monkeypatch.setenv(name="ACCESS_TOKEN_TTL_SECONDS",value=-100)
  with pytest.raises(RuntimeError):
    get_access_token_ttl_seconds()

  monkeypatch.delenv(name="ACCESS_TOKEN_TTL_SECONDS",raising=False)
  monkeypatch.setenv(name="ACCESS_TOKEN_TTL_SECONDS",value="abc")
  with pytest.raises(RuntimeError):
    get_access_token_ttl_seconds()

  monkeypatch.delenv(name="ACCESS_TOKEN_TTL_SECONDS",raising=False)
  monkeypatch.setenv(name="ACCESS_TOKEN_TTL_SECONDS",value=1800)
  assert get_access_token_ttl_seconds()==1800


from app.core.config import get_knowledge_settings


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