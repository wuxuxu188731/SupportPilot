from dataclasses import dataclass
from typing import Protocol

@dataclass
class User:
  user_id : str
  username : str
  password_hash : str
  created_at : str


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