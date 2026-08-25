import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from app.db.migrations import upgrade_database
from app.users.base import(
  User,
  UserAlreadyExistError,
  UserNotFoundError,
)


class SQLiteUserStore():
  def __init__(self, database_path : str | Path):
    self._database_path = database_path
    upgrade_database(self._database_path)

  def _connect(self) -> sqlite3.Connection:
    connection = sqlite3.Connection(self._database_path,timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")  #pragma foreign_key = on
    connection.execute("PRAGMA journal_mode = WAL")
    return connection

  @contextmanager
  def _connection(self) -> Iterator[sqlite3.Connection]:
    connection = self._connect()
    try:
      with connection:
        yield connection
    finally:
      connection.close()

  @staticmethod
  def _to_row(row : sqlite3.Row) -> User:
    return User(
      user_id=row["id"],
      username=row["username"],
      password_hash=row["password_hash"],
      created_at=row["created_at"]
    )

  def create_user(self, *, username : str, password_hash : str) -> User:
    try:
      user_existed = self.get_by_username(username=username)
      if user_existed is not None:
        raise UserAlreadyExistError("username already exist")
    except UserNotFoundError as exc:
      
      user_id = str(uuid4())
      with self._connection() as connection:
        connection.execute(
          """
          INSERT INTO users(id, username, password_hash)
          VALUES (?, ?, ?)
          """,
          (user_id, username, password_hash)
        )
      return self.get_by_id(user_id=user_id)

  def get_by_username(self, *, username : str) -> User:
    with self._connection() as connection:
      row = connection.execute(
        """
        SELECT id, username, password_hash, created_at
        FROM users
        WHERE username = ? COLLATE NOCASE
        """,
        (username,),
      ).fetchone()
      if row is None:
        raise UserNotFoundError("user not found")
      return self._to_row(row)

  def get_by_id(self, *, user_id : str) -> User:
    with self._connection() as connection:
      row = connection.execute(
        """
        SELECT id, username, password_hash, created_at
        FROM users
        WHERE id=?
        """,
        (user_id,),
      ).fetchone()
      if row is None:
        raise UserNotFoundError("user not found")
      return self._to_row(row)
