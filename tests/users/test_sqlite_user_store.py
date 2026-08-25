import sqlite3

import pytest

from app.users.base import UserAlreadyExistError, UserNotFoundError
from app.users.sqlite_store import SQLiteUserStore

#测试create_user函数是否有作用
def test_user_round_trip_by_username_and_id(tmp_path):
    database_path = tmp_path / "app.db"
    store = SQLiteUserStore(database_path)

    created = store.create_user(
        username="alice",
        password_hash="$2b$12$test-hash",
    )

    assert store.get_by_username(username="alice") == created
    assert store.get_by_id(user_id=created.user_id) == created


#测试能否用相同的用户名创建账号
def test_username_is_unique_case_insensitively(tmp_path):
    store = SQLiteUserStore(tmp_path / "app.db")
    store.create_user(username="alice", password_hash="first-hash")

    with pytest.raises(UserAlreadyExistError):
        store.create_user(username="ALICE", password_hash="second-hash")


#测试使用不存在的用户名或者用户ID查找用户，查找失败
def test_missing_user_raises_domain_error(tmp_path):
    store = SQLiteUserStore(tmp_path / "app.db")

    with pytest.raises(UserNotFoundError):
        store.get_by_username(username="missing")

    with pytest.raises(UserNotFoundError):
        store.get_by_id(user_id="missing")


#测试数据库里面不出现明文密码
def test_database_never_contains_plain_password(tmp_path):
    database_path = tmp_path / "app.db"
    store = SQLiteUserStore(database_path)
    store.create_user(username="alice", password_hash="stored-hash")

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT username, password_hash FROM users"
        ).fetchone()

    assert row == ("alice", "stored-hash")
    columns = {
        item[1]
        for item in sqlite3.connect(database_path)
        .execute("PRAGMA table_info(users)")
        .fetchall()
    }
    assert "password" not in columns