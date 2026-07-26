import sqlite3
import json
from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager
from typing import Iterator
from datetime import datetime,timezone

from app.db.migrations import upgrade_database
from app.sessions.base import Conversation,ConversationNotFoundError


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
      user_id=row["user_id"],
      system_prompt=row["system_prompt"]
    )
  
  @staticmethod
  def _get_owned_row(
      connection:sqlite3.Connection,
      *,
      user_id : str,
      conversation_id : str
    )->sqlite3.Row:
    row = connection.execute(
      """
      SELECT id,user_id,system_prompt 
      FROM conversations 
      WHERE id=? AND user_id=?
      """,(conversation_id,user_id)
    ).fetchone()

    if row is None:
      raise ConversationNotFoundError("conversation not found")
    
    return row
  
  #create C
  def create_conversation(
    self,
    *,
    user_id : str,
    system_prompt : str | None = None
  )->Conversation:
    conversation = Conversation(
      conversation_id=str(uuid4()),
      user_id=user_id,
      system_prompt=system_prompt
    )
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with self._connection() as connection:
      connection.execute(
        """
        INSERT INTO conversations (id, user_id, system_prompt, created_at, updated_at)
        values(?,?,?,?,?)
        """,(
          conversation.conversation_id,
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
    user_id : str,
    conversation_id : str
  )->Conversation:
    with self._connection() as connection:
      row = self._get_owned_row(connection,user_id=user_id,conversation_id=conversation_id)
    return self._to_conversation(row)


  #update U
  def update_system_prompt(
    self,
    *,
    user_id : str,
    conversation_id : str,
    system_prompt : str,
    )->None:
    with self._connection() as connection:
      self._get_owned_row(
        connection=connection,
        user_id=user_id,
        conversation_id=conversation_id
      )
      connection.execute(
        """
        UPDATE conversations 
        SET system_prompt=?, updated_at=CURRENT_TIMESTAMP
        where id=? AND user_id=?
        """,(system_prompt,conversation_id,user_id)
      )

  #加载消息列表，从数据库中加载消息列表
  def load_messages(
    self,
    *,
    user_id : str,
    conversation_id : str,
  )->list[dict]:
    with self._connection() as connection:
      #对会话的存在性和所有权进行校验
      self._get_owned_row(
        connection=connection,
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

  #将增加的消息存入数据库：1.判断conversation_id是否存在，2.conversation对应的user_id是否匹配，
  # 3.将增加的messages:list[dict]加入数据库中，
  def append_messages(
    self,
    *,
    user_id : str,
    conversation_id : str,
    messages : list[dict]
  )->None:
    if not messages:
      return
    with self._connection() as connection:
      connection.execute("BEGIN IMMEDIATE")
      self._get_owned_row(connection=connection,user_id=user_id,conversation_id=conversation_id)
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
        WHERE id=? AND user_id=?
        """,(conversation_id,user_id)
      )


  
