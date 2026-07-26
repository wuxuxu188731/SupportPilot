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