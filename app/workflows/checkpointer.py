"""SQLite checkpointer 生命周期与配置。

设计 11.4：本地 MVP 使用官方 ``langgraph-checkpoint-sqlite`` 持久化器；
checkpoint 使用独立 SQLite 文件（通过 ``LANGGRAPH_CHECKPOINT_DB_PATH``
配置），避免与 Alembic 管理的业务表混杂。checkpointer schema 初始化作为
应用启动步骤完成，测试使用临时数据库。
"""

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver


def create_sqlite_checkpointer(database_path: str | Path) -> SqliteSaver:
    """创建并初始化持久化 checkpointer。

    返回的 ``SqliteSaver`` 持有独立连接，应用生命周期内保持存活；
    进程重启后使用相同文件路径重新创建即可按 ``thread_id`` 恢复。
    """
    connection = sqlite3.connect(str(database_path), check_same_thread=False)
    checkpointer = SqliteSaver(connection)
    checkpointer.setup()
    return checkpointer
