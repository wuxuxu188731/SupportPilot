import sqlite3
import json
import logging
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
  MessageDisplay,
  MessageRecord,
)

logger = logging.getLogger(__name__)

# message_citations 的业务列白名单（不含主键 id 与 created_at）。
# 必须与 Citation.public_dict() 的键**完全相等**：写入前用它校验 dict 的键，
# 缺列报错、多余键同样报错——多余键意味着 Citation 加了字段却没加列，
# 必须显式失败，而不是静默丢掉一个字段。顺序即引用在回答中的展示次序。
MESSAGE_CITATION_COLUMNS = (
  "citation_id",
  "document_id",
  "version_id",
  "chunk_id",
  "title",
  "heading_path",
  "content",
  "start_offset",
  "end_offset",
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

  @staticmethod
  def _citation_row_to_dict(row : sqlite3.Row)->dict:
    """把一条 message_citations 行投影成引用 dict（键与 Citation 字段一一对应）。"""
    return {column: row[column] for column in MESSAGE_CITATION_COLUMNS}

  @staticmethod
  def _citation_dict_to_row(
    citation : dict,
    *,
    conversation_id : str,
    seq : int,
    ordinal : int,
  )->tuple:
    """校验引用 dict 的键并把一行 message_citations 打包成待插入元组。

    缺列与多余键都抛 ValueError：多余键说明 Citation 加了字段却没加库列，
    静默丢字段会让历史引用与实时引用长期不一致且难以察觉。
    """
    keys = set(citation)
    expected = set(MESSAGE_CITATION_COLUMNS)
    missing = expected - keys
    unexpected = keys - expected
    if missing or unexpected:
      raise ValueError(
        "引用字段与 message_citations 列不一致："
        f"缺少 {sorted(missing)}，多余 {sorted(unexpected)}"
      )
    return (
      conversation_id,
      seq,
      ordinal,
      citation["citation_id"],
      citation["document_id"],
      citation["version_id"],
      citation["chunk_id"],
      citation["title"],
      citation["heading_path"],
      citation["content"],
      citation["start_offset"],
      citation["end_offset"],
    )

  def load_message_records(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str,
  )->list[MessageRecord]:
    """读取会话的全部原始消息记录（seq 升序），归属不符时抛 NotFound。

    记录包含完整 payload 供服务层做安全过滤，禁止绕过服务层直接暴露。
    同时按 seq 分组带回该会话的持久化引用（message_citations，ordinal 升序）。
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
        SELECT seq, role, payload_json, created_at, answer_incomplete
        FROM messages
        WHERE conversation_id = ?
        ORDER BY seq ASC
        """,(conversation_id,)
      ).fetchall()
      citation_rows = connection.execute(
        """
        SELECT seq, citation_id, document_id, version_id, chunk_id,
               title, heading_path, content, start_offset, end_offset, ordinal
        FROM message_citations
        WHERE conversation_id = ?
        ORDER BY seq ASC, ordinal ASC
        """,(conversation_id,)
      ).fetchall()

    citations_by_seq : dict[int, list[dict]] = {}
    for citation_row in citation_rows:
      citations_by_seq.setdefault(citation_row["seq"], []).append(
        self._citation_row_to_dict(citation_row)
      )

    return [
      MessageRecord(
        sequence=row["seq"],
        role=row["role"],
        payload=json.loads(row["payload_json"]),
        created_at=row["created_at"],
        citations=tuple(citations_by_seq.get(row["seq"], ())),
        # NULL 表示「没有记录」（中间消息或升级前的历史行），与 0 一样当 False。
        answer_incomplete=bool(row["answer_incomplete"]),
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
    messages : list[dict],
    display : list[MessageDisplay | None] | None = None,
  )->None:
    """追加一轮消息，并在同一事务内写入可选的展示数据（引用与完整性标记）。

    ``display`` 必须与 ``messages`` 等长（第 i 项对应第 i 条消息）；为 None 表示
    该消息没有展示数据。``messages`` 为空时 ``display`` 只能为 None 或全 None。
    展示数据不进入 ``payload_json``，模型上下文因此零变化。
    """
    if display is not None:
      if len(display) != len(messages):
        raise ValueError("display must have the same length as messages")
      if not messages and any(item is not None for item in display):
        raise ValueError("display must be empty or all None when messages is empty")
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
            json.dumps(message,ensure_ascii=False),
            self._answer_incomplete_value(display,offset-1),
          )
        )

      connection.executemany(
        """
        INSERT INTO messages(conversation_id,seq,role,payload_json,answer_incomplete)
        VALUES (?,?,?,?,?)
        """,rows
      )
      self._insert_citation_rows(
        connection=connection,
        conversation_id=conversation_id,
        last_seq=last_seq,
        display=display,
      )
      connection.execute(
        """
        UPDATE conversations
        SET updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND organization_id=? AND user_id=?
        """,(conversation_id,organization_id,user_id)
      )

  @staticmethod
  def _answer_incomplete_value(
    display : list[MessageDisplay | None] | None,
    index : int,
  )->int | None:
    """取第 index 条消息的 answer_incomplete 列值。

    只有「本轮产生了最终回答」的消息才有记录（True/False → 1/0），
    其余消息写 NULL——这样 NULL 就只剩一个含义：「没有记录」。
    展示数据形态异常（长度不符、不是 MessageDisplay）时按「没有记录」处理，
    绝不因为展示数据而让消息落库失败。
    """
    if display is None or index >= len(display):
      return None
    item = display[index]
    if not isinstance(item, MessageDisplay):
      return None
    return 1 if item.answer_incomplete else 0

  def _insert_citation_rows(
    self,
    *,
    connection : sqlite3.Connection,
    conversation_id : str,
    last_seq : int,
    display : list[MessageDisplay | None] | None,
  )->None:
    """在同一事务内写入引用行；写入失败只丢弃引用行，绝不让整轮对话失败。

    引用是展示数据，消息与提案才是业务事实：一次 IntegrityError 若向上传播，
    事务会整体回滚，已经生成的回答甚至已经创建的提案都会丢失（用户看到 500），
    那比「引用没存上」严重得多。因此这里降级为丢弃该条引用并留诊断日志。

    每条引用用 SAVEPOINT 单独保护：库级约束被触发时只回滚这一条引用，
    不牵连同一批次里的其它引用，更不会波及消息本身。
    """
    if display is None:
      return
    for offset, item in enumerate(display, start=1):
      if not isinstance(item, MessageDisplay) or not item.citations:
        continue
      seq = last_seq + offset
      for ordinal, citation in enumerate(item.citations, start=1):
        self._insert_one_citation_row(
          connection=connection,
          conversation_id=conversation_id,
          seq=seq,
          ordinal=ordinal,
          citation=citation,
        )

  def _insert_one_citation_row(
    self,
    *,
    connection : sqlite3.Connection,
    conversation_id : str,
    seq : int,
    ordinal : int,
    citation : dict,
  )->None:
    """写入一条引用行；任何一步失败都只丢弃这一条引用并留诊断日志。"""
    savepoint = "sp_message_citation"
    try:
      row = self._citation_dict_to_row(
        citation,
        conversation_id=conversation_id,
        seq=seq,
        ordinal=ordinal,
      )
    except (ValueError, KeyError, TypeError):
      logger.warning(
        "引用字段不合法，已丢弃该条引用（会话 %s，消息 seq %s，序号 %s）",
        conversation_id,
        seq,
        ordinal,
      )
      return
    try:
      connection.execute(f"SAVEPOINT {savepoint}")
      try:
        connection.execute(
          """
          INSERT INTO message_citations(
            conversation_id, seq, ordinal, citation_id, document_id,
            version_id, chunk_id, title, heading_path, content,
            start_offset, end_offset
          ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
          """,
          row,
        )
      except sqlite3.DatabaseError:
        # 库级约束（CHECK / 唯一约束 / 外键）不该拖垮整轮对话，见方法说明。
        connection.execute(f"ROLLBACK TO {savepoint}")
        logger.warning(
          "引用行写入失败，已丢弃该条引用（会话 %s，消息 seq %s，序号 %s）",
          conversation_id,
          seq,
          ordinal,
          exc_info=True,
        )
      else:
        connection.execute(f"RELEASE {savepoint}")
    except sqlite3.DatabaseError:
      # 连 SAVEPOINT 都建立/释放不了（库异常）时同样不能向上抛。
      logger.warning(
        "引用行保护失败，已跳过该条引用（会话 %s，消息 seq %s，序号 %s）",
        conversation_id,
        seq,
        ordinal,
        exc_info=True,
      )
