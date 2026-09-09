import sqlite3
import json
from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager
from typing import Iterator
from datetime import datetime,timezone

from app.db.migrations import upgrade_database
from app.sessions.base import (
  Conversation,
  ConversationNotFoundError,
  ConversationRecord,
  MessageRecord,
)


class SQLiteSessionStore:
  def __init__(self, database_path : str|Path):
    self._database_path = database_path
    upgrade_database(self._database_path)


  def _connect(self)->sqlite3.Connection:
    connection  = sqlite3.connect(database=self._database_path,timeout=30)
    connection.row_factory=sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection

  @contextmanager
  def _connection(self)->Iterator[sqlite3.Connection]:
    connection = self._connect()
    try:
      with connection:
        yield connection
    finally:
      connection.close()

  #select
  @staticmethod
  def _to_conversation(row : sqlite3.Row)->Conversation:
    return Conversation(
      conversation_id=row["id"],
      organization_id=row["organization_id"],
      user_id=row["user_id"],
      system_prompt=row["system_prompt"]
    )

  @staticmethod
  def _to_conversation_record(row : sqlite3.Row)->ConversationRecord:
    """把会话行转换为会话记录；同时解析首条用户消息正文供标题推导。"""
    first_user_payload = row["first_user_payload"]
    first_user_content = None
    if first_user_payload:
      try:
        payload = json.loads(first_user_payload)
      except (json.JSONDecodeError, TypeError):
        payload = None
      content = (payload or {}).get("content")
      if isinstance(content, str):
        first_user_content = content
    return ConversationRecord(
      conversation_id=row["id"],
      system_prompt=row["system_prompt"],
      created_at=row["created_at"],
      updated_at=row["updated_at"],
      first_user_content=first_user_content,
    )

  @staticmethod
  def _get_owned_row(
      connection:sqlite3.Connection,
      *,
      organization_id : str,
      user_id : str,
      conversation_id : str
    )->sqlite3.Row:
    row = connection.execute(
      """
      SELECT id, organization_id, user_id, system_prompt
      FROM conversations
      WHERE id=? AND organization_id=? AND user_id=?
      """,(conversation_id, organization_id, user_id)
    ).fetchone()

    if row is None:
      raise ConversationNotFoundError("conversation not found")

    return row

  @staticmethod
  def _conversation_record_sql() -> str:
    """拼接会话记录查询 SQL：主行 + 首条用户消息载荷（相关子查询）。

    首条用户消息 = 属于该会话且 role 为 user 的最小 seq 消息。
    """
    return """
      SELECT
        c.id, c.system_prompt, c.created_at, c.updated_at,
        (
          SELECT m.payload_json
          FROM messages AS m
          WHERE m.conversation_id = c.id AND m.role = 'user'
          ORDER BY m.seq ASC
          LIMIT 1
        ) AS first_user_payload
      FROM conversations AS c
    """

  def list_conversations(
    self,
    *,
    organization_id : str,
    user_id : str,
    limit : int,
    offset : int,
  )->list[ConversationRecord]:
    """列出当前企业与当前用户自己的会话：按 updated_at 倒序、
    同秒时以会话 id 为稳定次级排序，并支持 limit/offset 分页。"""
    with self._connection() as connection:
      rows = connection.execute(
        self._conversation_record_sql()
        + """
        WHERE c.organization_id=? AND c.user_id=?
        ORDER BY c.updated_at DESC, c.id DESC
        LIMIT ? OFFSET ?
        """,
        (organization_id, user_id, limit, offset),
      ).fetchall()
    return [self._to_conversation_record(row) for row in rows]

  def get_conversation_record(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str
  )->ConversationRecord:
    """读取单条会话记录；会话不存在、跨用户或跨企业时抛 NotFound。"""
    with self._connection() as connection:
      row = connection.execute(
        self._conversation_record_sql()
        + """
        WHERE c.id=? AND c.organization_id=? AND c.user_id=?
        """,
        (conversation_id, organization_id, user_id),
      ).fetchone()
      if row is None:
        raise ConversationNotFoundError("conversation not found")
    return self._to_conversation_record(row)

  def load_message_records(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str,
  )->list[MessageRecord]:
    """读取会话的全部原始消息记录（seq 升序），归属不符时抛 NotFound。

    记录包含完整 payload 供服务层做安全过滤，禁止绕过服务层直接暴露。
    """
    with self._connection() as connection:
      self._get_owned_row(
        connection=connection,
        organization_id=organization_id,
        user_id=user_id,
        conversation_id=conversation_id
      )
      rows = connection.execute(
        """
        SELECT seq, role, payload_json, created_at
        FROM messages
        WHERE conversation_id = ?
        ORDER BY seq ASC
        """,(conversation_id,)
      ).fetchall()
    return [
      MessageRecord(
        sequence=row["seq"],
        role=row["role"],
        payload=json.loads(row["payload_json"]),
        created_at=row["created_at"],
      )
      for row in rows
    ]

  #create C
  def create_conversation(
    self,
    *,
    organization_id : str,
    user_id : str,
    system_prompt : str | None = None
  )->Conversation:
    conversation = Conversation(
      conversation_id=str(uuid4()),
      organization_id=organization_id,
      user_id=user_id,
      system_prompt=system_prompt
    )
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with self._connection() as connection:
      connection.execute(
        """
        INSERT INTO conversations (id, organization_id, user_id, system_prompt, created_at, updated_at)
        values(?,?,?,?,?,?)
        """,(
          conversation.conversation_id,
          conversation.organization_id,
          conversation.user_id,
          conversation.system_prompt,
          current_time,
          current_time
        )
      )
    return conversation

  #get R
  def get_conversation(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str
  )->Conversation:
    with self._connection() as connection:
      row = self._get_owned_row(
        connection,
        organization_id=organization_id,
        user_id=user_id,
        conversation_id=conversation_id
      )
    return self._to_conversation(row)


  #update U
  def update_system_prompt(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str,
    system_prompt : str,
    )->None:
    with self._connection() as connection:
      self._get_owned_row(
        connection=connection,
        organization_id=organization_id,
        user_id=user_id,
        conversation_id=conversation_id
      )
      connection.execute(
        """
        UPDATE conversations
        SET system_prompt=?, updated_at=CURRENT_TIMESTAMP
        where id=? AND organization_id=? AND user_id=?
        """,(system_prompt,conversation_id,organization_id,user_id)
      )

  def load_messages(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str,
  )->list[dict]:
    with self._connection() as connection:
      self._get_owned_row(
        connection=connection,
        organization_id=organization_id,
        user_id=user_id,
        conversation_id=conversation_id
      )
      rows = connection.execute(
        """
        SELECT payload_json
        FROM messages
        WHERE conversation_id = ?
        ORDER BY seq ASC
        """,(conversation_id,)
      ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]

  def append_messages(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str,
    messages : list[dict]
  )->None:
    if not messages:
      return
    with self._connection() as connection:
      connection.execute("BEGIN IMMEDIATE")
      self._get_owned_row(
        connection=connection,
        organization_id=organization_id,
        user_id=user_id,
        conversation_id=conversation_id
      )
      last_seq = connection.execute(
        """
        SELECT COALESCE(MAX(seq), 0)
        FROM messages
        WHERE conversation_id = ?
        """,(conversation_id,)
      ).fetchone()[0]

      rows = []
      for offset,message in enumerate(messages,start=1):
        rows.append(
          (
            conversation_id,
            last_seq+offset,
            str(message["role"]),
            json.dumps(message,ensure_ascii=False)
          )
        )

      connection.executemany(
        """
        INSERT INTO messages(conversation_id,seq,role,payload_json)
        VALUES (?,?,?,?)
        """,rows
      )
      connection.execute(
        """
        UPDATE conversations
        SET updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND organization_id=? AND user_id=?
        """,(conversation_id,organization_id,user_id)
      )
