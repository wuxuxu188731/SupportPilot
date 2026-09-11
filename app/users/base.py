from dataclasses import dataclass
from typing import Protocol

@dataclass
class User:
  user_id : str   # 用户唯一标识（主键，uuid4 字符串）
  username : str  # 登录用户名，全局唯一且大小写不敏感
  password_hash : str  # 密码加盐哈希值，禁止存储或对外返回明文密码
  created_at : str  # 用户创建时间（UTC 文本）


class UserAlreadyExistError(ValueError):
  pass

class UserNotFoundError(LookupError):
  pass

class UserStore(Protocol):
  def create_user(self, *, username : str, password_hash : str) -> User:
    raise NotImplementedError

  def get_by_username(self, *, username : str) -> User:
    raise NotImplementedError

  def get_by_id(self, *, user_id : str) -> User:
    raise NotImplementedError

  def get_by_ids(self, *, user_ids : list[str]) -> list[User]:
    """按 id 批量查询用户；返回顺序不承诺，缺失的 id 直接跳过（不报错）。"""
    raise NotImplementedError