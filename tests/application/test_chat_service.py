import sqlite3

import pytest

from app.application.chat_service import (
  ChatService,
  citation_to_stored_dict,
  derive_conversation_title,
  stored_citation_from_dict,
)
from app.application.organization_service import TenantContext
from app.agent.prompts import SUPPORT_SYSTEM_PROMPT
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.knowledge.results import Citation
from app.organizations.base import MembershipRole
from app.schemas.chat import LLMResponse
from app.sessions.base import ConversationNotFoundError
from app.sessions.sqlite_store import SQLiteSessionStore
from threading import Barrier, Event, Lock, Thread


CONTEXT = TenantContext(
    user_id="user-a",
    organization_id="org-a",
    role=MembershipRole.AGENT,
)


def _seed_tenant_scope(database_path):
    """Insert matching user, org, and membership rows so FK constraints pass."""
    with sqlite3.connect(database_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO users(id, username, password_hash) VALUES (?, ?, ?)",
            ("user-a", "testuser", "hash"),
        )
        conn.execute(
            "INSERT INTO organizations(id, name) VALUES (?, ?)",
            ("org-a", "Test Org"),
        )
        conn.execute(
            "INSERT INTO memberships(organization_id, user_id, role) VALUES (?, ?, ?)",
            ("org-a", "user-a", "agent"),
        )


def _seed_second_user_and_org(database_path):
    """插入第二个用户与企业（与 CONTEXT 不同租户），用于隔离性测试。"""
    with sqlite3.connect(database_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO users(id, username, password_hash) VALUES (?, ?, ?)",
            ("user-b", "seconduser", "hash"),
        )
        conn.execute(
            "INSERT INTO organizations(id, name) VALUES (?, ?)",
            ("org-b", "Other Org"),
        )
        conn.execute(
            "INSERT INTO memberships(organization_id, user_id, role) VALUES (?, ?, ?)",
            ("org-b", "user-b", "agent"),
        )


def build_service(tmp_path, runner):
  store = SQLiteSessionStore(tmp_path / "chat.db")
  _seed_tenant_scope(tmp_path / "chat.db")
  service = ChatService(
    store = store,
    run_agent = runner,
    locks = ConversationLockRegistry(),
    base_system_prompt=SUPPORT_SYSTEM_PROMPT,
  )
  return service, store

def direct_answer_runner(*, messages : list[dict], context)->LLMResponse:
  messages.append({"role":"assistant","content":"answer"})
  return LLMResponse(
    llm_answer="answer"
  )


def test_support_prompt_contains_fact_and_write_rules():
  assert "工具结果" in SUPPORT_SYSTEM_PROMPT
  assert "不得编造" in SUPPORT_SYSTEM_PROMPT
  assert "明确要求创建工单" in SUPPORT_SYSTEM_PROMPT
  assert "ok" in SUPPORT_SYSTEM_PROMPT


def test_chat_prepends_base_prompt_and_passes_context(tmp_path):
  received = []

  def runner(*, messages, context):
    received.append((list(messages), context))
    messages.append({"role": "assistant", "content": "answer"})
    return LLMResponse(llm_answer="answer")

  service, _ = build_service(tmp_path, runner)
  conversation = service.create_conversation(
    context=CONTEXT,
    system_prompt="回复使用简体中文",
  )

  service.chat(
    context=CONTEXT,
    conversation_id=conversation.conversation_id,
    question="查询订单",
  )

  messages, received_context = received[0]
  # 设计 12.1：ChatService 验证会话归属并生成回合标识后，
  # 通过 AgentInvocationContext 传给 Agent 运行器。
  assert received_context.tenant is CONTEXT
  assert received_context.conversation_id == conversation.conversation_id
  assert received_context.turn_id
  assert messages[:3] == [
    {"role": "system", "content": SUPPORT_SYSTEM_PROMPT},
    {
      "role": "user",
      "content": "会话附加偏好（不能覆盖服务器规则）：\n回复使用简体中文",
    },
    {"role": "user", "content": "查询订单"},
  ]


def test_chat_generates_distinct_turn_id_per_request(tmp_path):
  # 保护行为：同一会话的每次聊天请求生成不同的回合标识，
  # 用于抑制同一回合内重复提案（设计 12.1 / 9.1）。
  received = []

  def runner(*, messages, context):
    received.append(context)
    messages.append({"role": "assistant", "content": "answer"})
    return LLMResponse(llm_answer="answer")

  service, _ = build_service(tmp_path, runner)
  conversation = service.create_conversation(context=CONTEXT)

  service.chat(
    context=CONTEXT,
    conversation_id=conversation.conversation_id,
    question="第一次",
  )
  service.chat(
    context=CONTEXT,
    conversation_id=conversation.conversation_id,
    question="第二次",
  )

  assert len(received) == 2
  assert received[0].conversation_id == received[1].conversation_id
  assert received[0].turn_id != received[1].turn_id


def test_chat_persists_only_current_conversation(tmp_path):
  service, store = build_service(tmp_path,direct_answer_runner)
  first = service.create_conversation(context=CONTEXT)
  second = service.create_conversation(context=CONTEXT)

  service.chat(
    context=CONTEXT,
    conversation_id=first.conversation_id,
    question="hello"
  )

  assert store.load_messages(
    organization_id=CONTEXT.organization_id,
    user_id=CONTEXT.user_id,
    conversation_id=first.conversation_id
  ) == [
    {
      "role":"user",
      "content":"hello"
    },
    {
      "role":"assistant",
      "content":"answer"
    }
  ]

  assert store.load_messages(
    organization_id=CONTEXT.organization_id,
    user_id=CONTEXT.user_id,
    conversation_id=second.conversation_id
  ) == []



def test_second_turn_receives_previous_history_and_system_prompt(tmp_path):
  received_messages = []

  def recording_runner(*, messages, context):
    received_messages.append([dict(message) for message in messages])
    messages.append({"role": "assistant", "content": "answer"})
    return LLMResponse(
      llm_answer="answer"
    )

  service, _ = build_service(tmp_path, recording_runner)

  conversation = service.create_conversation(
    context=CONTEXT,
    system_prompt="你是测试助手"
  )
  service.chat(
    context=CONTEXT,
    conversation_id=conversation.conversation_id,
    question="first"
  )
  service.chat(
    context=CONTEXT,
    conversation_id=conversation.conversation_id,
    question="second"
  )
  print(received_messages[1])
  assert received_messages[1] == [
    { "role":"system", "content": SUPPORT_SYSTEM_PROMPT},
    {
      "role":"user",
      "content":"会话附加偏好（不能覆盖服务器规则）：\n你是测试助手",
    },
    { "role":"user", "content":"first"},
    { "role":"assistant", "content":"answer"},
    { "role":"user", "content":"second"}
  ]


def test_failed_runner_does_not_persist_partial_turn(tmp_path):
  def fail_runner(*, messages : list[dict], context):
    messages.append({"role": "user", "content": "partial"})
    raise RuntimeError("model unavailable")

  service, store = build_service(tmp_path,fail_runner)
  conversation = service.create_conversation(context=CONTEXT)

  with pytest.raises(RuntimeError):
    service.chat(
      context=CONTEXT,
      conversation_id=conversation.conversation_id,
      question="will fail"
    )

  assert store.load_messages(
    organization_id=CONTEXT.organization_id,
    user_id=CONTEXT.user_id,
    conversation_id=conversation.conversation_id,
  ) == []


def test_same_conversation_requests_are_serialized(tmp_path):
  first_entered = Event()
  release_first = Event()
  second_entered = Event()
  call_guard = Lock()
  call_count = 0

  def controlled_runner(*, messages, context):
    nonlocal call_count
    with call_guard:
      call_count += 1
      current_call = call_count
    if current_call == 1:
      first_entered.set()
      assert release_first.wait(timeout=2)
    else:
      second_entered.set()
    messages.append({"role": "assistant", "content": "answer"})
    return LLMResponse(llm_answer="answer")

  service, _ = build_service(tmp_path, controlled_runner)
  conversation = service.create_conversation(context=CONTEXT)

  def ask(question):
    service.chat(
      context=CONTEXT,
      conversation_id=conversation.conversation_id,
      question=question,
    )

  first = Thread(target=ask, args=("first",))
  second = Thread(target=ask, args=("second",))
  first.start()
  assert first_entered.wait(timeout=2)
  second.start()
  assert not second_entered.wait(timeout=0.1)
  release_first.set()
  first.join(timeout=2)
  second.join(timeout=2)

  assert second_entered.is_set()


def test_different_conversations_can_run_in_parallel(tmp_path):
  both_runners_entered = Barrier(2)
  errors = []

  def synchronized_runner(*, messages, context):
    both_runners_entered.wait(timeout=2)
    messages.append({"role": "assistant", "content": "answer"})
    return LLMResponse(llm_answer="answer")

  service, _ = build_service(tmp_path, synchronized_runner)
  first = service.create_conversation(context=CONTEXT)
  second = service.create_conversation(context=CONTEXT)

  def ask(conversation_id):
    try:
      service.chat(
        context=CONTEXT,
        conversation_id=conversation_id,
        question="hello",
      )
    except Exception as exc:
      errors.append(exc)

  threads = [
    Thread(target=ask, args=(first.conversation_id,)),
    Thread(target=ask, args=(second.conversation_id,)),
  ]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join(timeout=3)

  assert errors == []
  assert all(not thread.is_alive() for thread in threads)


# —— 会话标题推导 ——

def test_title_from_first_user_message_is_stable_and_trimmed():
  # 保护行为：标题由第一条用户消息正文稳定生成，首尾空白被去除。
  assert derive_conversation_title("  我的订单怎么还没到？  ") == "我的订单怎么还没到？"


def test_title_collapses_whitespace_and_truncates():
  # 边界情况：连续空白（含换行/制表符）折叠为单个空格；
  # 超长内容按最大标题长度截断，且不会返回全空白标题。
  long_content = "行一\n\n行二\t\t行三 " * 6
  title = derive_conversation_title(long_content)
  assert "\n" not in title
  assert "\t" not in title
  assert len(title) == 30  # MAX_CONVERSATION_TITLE_LENGTH
  assert "行一 行二 行三" in title

  from app.application.chat_service import MAX_CONVERSATION_TITLE_LENGTH
  assert MAX_CONVERSATION_TITLE_LENGTH == 30


def test_title_empty_or_blank_content_falls_back_to_default():
  # 边界情况：无消息、空串或全空白内容统一显示默认标题「新会话」。
  assert derive_conversation_title(None) == "新会话"
  assert derive_conversation_title("") == "新会话"
  assert derive_conversation_title("   \n\t ") == "新会话"


def test_list_conversations_builds_titles_from_first_user_message(tmp_path):
  # 保护行为：会话列表标题来自首条用户消息；未发送消息的空会话为「新会话」。
  service, store = build_service(tmp_path, direct_answer_runner)
  empty = service.create_conversation(context=CONTEXT)
  asked = service.create_conversation(context=CONTEXT)
  service.chat(
    context=CONTEXT,
    conversation_id=asked.conversation_id,
    question="  请帮我查询  订单 状态  ",
  )

  items = service.list_conversations(context=CONTEXT, limit=50, offset=0)
  by_id = {item.conversation_id: item for item in items}
  assert by_id[empty.conversation_id].title == "新会话"
  assert by_id[asked.conversation_id].title == "请帮我查询 订单 状态"
  # 列表项不含内部字段，只暴露对外展示字段
  assert set(by_id[asked.conversation_id].model_dump().keys()) == {
    "conversation_id",
    "title",
    "created_at",
    "updated_at",
  }


# —— 安全历史读取 ——

def test_history_returns_user_and_final_assistant_in_seq_order(tmp_path):
  # 保护行为：历史消息按 seq 升序只返回 user 问题与最终 assistant 回答。
  service, store = build_service(tmp_path, direct_answer_runner)
  conversation = service.create_conversation(context=CONTEXT)
  store.append_messages(
    organization_id=CONTEXT.organization_id,
    user_id=CONTEXT.user_id,
    conversation_id=conversation.conversation_id,
    messages=[
      {"role": "user", "content": "第一个问题"},
      {
        "role": "assistant",
        "content": "中间思考",
        "reasoning_content": "内部推理",
        "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "get_order", "arguments": "{}"}}],
      },
      {"role": "tool", "tool_call_id": "call-1", "content": "工具原始结果"},
      {"role": "assistant", "content": "这是最终答案", "reasoning_content": "内部推理2"},
      {"role": "user", "content": "第二个问题"},
      {"role": "assistant", "content": "最终回答二"},
    ],
  )

  history = service.get_history(context=CONTEXT, conversation_id=conversation.conversation_id)
  assert [message.role for message in history.messages] == [
    "user",
    "assistant",
    "user",
    "assistant",
  ]
  assert [message.content for message in history.messages] == [
    "第一个问题",
    "这是最终答案",
    "第二个问题",
    "最终回答二",
  ]
  assert [message.sequence for message in history.messages] == [1, 4, 5, 6]
  assert history.conversation_id == conversation.conversation_id


def test_history_excludes_internal_and_empty_messages(tmp_path):
  # 安全边界：reasoning_content、tool_calls、工具结果、system 与 tool 消息
  # 一律不返回；空内容或全空白的 assistant 消息同样被过滤。
  service, store = build_service(tmp_path, direct_answer_runner)
  conversation = service.create_conversation(context=CONTEXT)
  store.append_messages(
    organization_id=CONTEXT.organization_id,
    user_id=CONTEXT.user_id,
    conversation_id=conversation.conversation_id,
    messages=[
      {"role": "system", "content": "内部系统提示词"},
      {"role": "user", "content": "可见问题"},
      {"role": "assistant", "content": "", "reasoning_content": "只有推理没有正文"},
      {"role": "assistant", "content": None},
      {"role": "assistant", "content": "   "},
      {"role": "assistant", "content": "带工具调用的中间消息", "tool_calls": [{"id": "c"}]},
      {"role": "tool", "tool_call_id": "c", "content": "结果"},
      {"role": "assistant", "content": "真实答案", "reasoning_content": "内部推理不暴露"},
    ],
  )

  history = service.get_history(context=CONTEXT, conversation_id=conversation.conversation_id)
  assert [(item.role, item.content) for item in history.messages] == [
    ("user", "可见问题"),
    ("assistant", "真实答案"),
  ]
  # 内部载荷本身仍原样保留在 store 中（模型上下文不受影响）
  raw = store.load_messages(
    organization_id=CONTEXT.organization_id,
    user_id=CONTEXT.user_id,
    conversation_id=conversation.conversation_id,
  )
  assert any("reasoning_content" in message for message in raw)
  assert any("tool_calls" in message for message in raw)


def test_history_empty_conversation_returns_empty_messages(tmp_path):
  # 边界情况：尚未发送任何消息的会话历史应为空数组，头部字段正常返回。
  service, _ = build_service(tmp_path, direct_answer_runner)
  conversation = service.create_conversation(context=CONTEXT, system_prompt="附加偏好")

  history = service.get_history(context=CONTEXT, conversation_id=conversation.conversation_id)
  assert history.messages == []
  assert history.system_prompt == "附加偏好"
  assert history.created_at
  assert history.updated_at


def test_history_cross_user_or_cross_org_raises_not_found(tmp_path):
  # 安全边界：读取他人会话或其它企业会话必须抛 NotFound（与 404 同义），
  # 不得泄露资源真实归属。
  service, _ = build_service(tmp_path, direct_answer_runner)
  _seed_second_user_and_org(tmp_path / "chat.db")
  conversation = service.create_conversation(context=CONTEXT)
  other_tenant = TenantContext(
    user_id="user-b",
    organization_id="org-b",
    role=MembershipRole.AGENT,
  )

  with pytest.raises(ConversationNotFoundError):
    service.get_history(context=other_tenant, conversation_id=conversation.conversation_id)

  with pytest.raises(ConversationNotFoundError):
    service.get_history(
      context=TenantContext(
        user_id="user-b",
        organization_id="org-a",
        role=MembershipRole.AGENT,
      ),
      conversation_id=conversation.conversation_id,
    )


# —— 引用随回答持久化（写路径 → 历史读路径） ——

def _citation(
  citation_id,
  *,
  document_id="doc-1",
  version_id="ver-1",
  chunk_id=None,
  title="退货政策",
  heading_path="3.2 退货流程",
  content="七天无理由退货",
  start_offset=5,
  end_offset=13,
):
  """构造一条带偏移的引用（默认值与实时响应同形）。"""
  return Citation(
    citation_id=citation_id,
    document_id=document_id,
    version_id=version_id,
    chunk_id=chunk_id or f"chunk-{citation_id}",
    title=title,
    heading_path=heading_path,
    content=content,
    start_offset=start_offset,
    end_offset=end_offset,
  )


def build_citation_runner(answer, citations, *, answer_incomplete=False):
  """构造一个返回固定回答与引用的假 runner（同时写入本轮消息）。"""

  def runner(*, messages : list[dict], context)->LLMResponse:
    messages.append({"role": "assistant", "content": answer})
    return LLMResponse(
      llm_answer=answer,
      citations=list(citations),
      answer_incomplete=answer_incomplete,
    )

  return runner


def test_chat_persists_citations_and_history_keeps_live_order(tmp_path):
  # 保护行为：走完一次 chat() 之后，历史里的 assistant 消息必须带**完整引用**
  # （含证据片段正文与偏移），且顺序与实时响应一致——构造一个 [C3] 先于 [C1]
  # 出现的回答，历史顺序必须是 C3、C1，而不是按编号排序。
  service, store = build_service(
    tmp_path,
    build_citation_runner(
      "先看 [C3] 再看 [C1]",
      [
        _citation("C3", document_id="doc-3", title="退款到账"),
        _citation("C1", document_id="doc-1", title="退货期限"),
      ],
    ),
  )
  conversation = service.create_conversation(context=CONTEXT)

  live = service.chat(
    context=CONTEXT,
    conversation_id=conversation.conversation_id,
    question="退货与退款",
  )
  history = service.get_history(
    context=CONTEXT, conversation_id=conversation.conversation_id
  )

  assert [item.citation_id for item in live.citations] == ["C3", "C1"]
  user_message, assistant_message = history.messages
  assert user_message.citations == []
  assert [item.citation_id for item in assistant_message.citations] == ["C3", "C1"]
  assert assistant_message.citations[0].document_id == "doc-3"
  # 证据片段与偏移一起持久化：刷新后卡片与实时完全一致
  assert assistant_message.citations[0].content == "七天无理由退货"
  assert assistant_message.citations[0].start_offset == 5
  assert assistant_message.citations[0].end_offset == 13
  assert assistant_message.citations[0].heading_path == "3.2 退货流程"
  assert assistant_message.answer_incomplete is False


def test_chat_persists_answer_incomplete_flag_even_without_citations(tmp_path):
  # 边界情况：漏引场景下 citations 为空但 answer_incomplete=true，
  # 刷新后仍必须给出「谨慎采用」提示——该标记不能随引用一起丢失。
  service, _ = build_service(
    tmp_path,
    build_citation_runner("没有引用的回答", [], answer_incomplete=True),
  )
  conversation = service.create_conversation(context=CONTEXT)

  service.chat(
    context=CONTEXT, conversation_id=conversation.conversation_id, question="问题"
  )
  history = service.get_history(
    context=CONTEXT, conversation_id=conversation.conversation_id
  )

  assistant_message = history.messages[1]
  assert assistant_message.citations == []
  assert assistant_message.answer_incomplete is True


def test_chat_normalizes_single_or_reversed_offsets_before_writing(tmp_path):
  # 边界情况（写前规整是主防线）：上游给出「只有一个偏移」「start >= end」
  # 「负偏移」的引用时，写库必须成功，落库的是「两个偏移都为 NULL」，
  # 而不是抛 IntegrityError 把整轮对话（含已生成的回答）一起带下水。
  service, store = build_service(
    tmp_path,
    build_citation_runner(
      "看引用 [C1] [C2] [C3]",
      [
        Citation(
          citation_id="C1", document_id="doc-1", version_id="ver-1",
          chunk_id="chunk-1", title="只有起始偏移", heading_path=None,
          content="片段一", start_offset=5, end_offset=None,
        ),
        Citation(
          citation_id="C2", document_id="doc-2", version_id="ver-1",
          chunk_id="chunk-2", title="反向区间", heading_path=None,
          content="片段二", start_offset=20, end_offset=9,
        ),
        Citation(
          citation_id="C3", document_id="doc-3", version_id="ver-1",
          chunk_id="chunk-3", title="负偏移", heading_path=None,
          content="片段三", start_offset=-3, end_offset=10,
        ),
      ],
    ),
  )
  conversation = service.create_conversation(context=CONTEXT)

  service.chat(
    context=CONTEXT, conversation_id=conversation.conversation_id, question="问题"
  )
  history = service.get_history(
    context=CONTEXT, conversation_id=conversation.conversation_id
  )

  citations = history.messages[1].citations
  assert [item.citation_id for item in citations] == ["C1", "C2", "C3"]
  for citation in citations:
    assert citation.start_offset is None
    assert citation.end_offset is None
  # 引用行确实落库了（不是被 CHECK 拒绝后静默丢弃）
  with sqlite3.connect(tmp_path / "chat.db") as connection:
    assert connection.execute(
      "SELECT COUNT(*) FROM message_citations"
    ).fetchone() == (3,)


def test_chat_drops_citation_with_blank_content(tmp_path):
  # 边界情况：空片段正文会被库级 CHECK 拒绝（document_chunks 有同名约束），
  # 因此写前直接丢弃该条引用，而不是写一条空证据行或让整轮对话失败。
  service, _ = build_service(
    tmp_path,
    build_citation_runner(
      "看引用 [C1] [C2]",
      [
        _citation("C1", content="   "),
        _citation("C2", document_id="doc-2", title="退款到账"),
      ],
    ),
  )
  conversation = service.create_conversation(context=CONTEXT)

  service.chat(
    context=CONTEXT, conversation_id=conversation.conversation_id, question="问题"
  )
  history = service.get_history(
    context=CONTEXT, conversation_id=conversation.conversation_id
  )

  assert [item.citation_id for item in history.messages[1].citations] == ["C2"]


def test_history_drops_corrupt_citation_rows_without_failing(tmp_path):
  # 边界情况（坏数据不炸接口）：库里存在非法引用行（空 citation_id、只有一个偏移）
  # 时历史接口仍正常返回，只丢弃坏行并保留合法行。
  service, store = build_service(
    tmp_path,
    build_citation_runner("看引用 [C1]", [_citation("C1")]),
  )
  conversation = service.create_conversation(context=CONTEXT)
  service.chat(
    context=CONTEXT, conversation_id=conversation.conversation_id, question="问题"
  )

  with sqlite3.connect(tmp_path / "chat.db") as connection:
    # 库级 CHECK 已经挡住了这类脏行；这里模拟「历史遗留 / 人工改库」产生的坏数据，
    # 因此临时关掉 CHECK 校验再插入，用来验证读侧兜底。
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(
      """
      INSERT INTO message_citations(
        conversation_id, seq, ordinal, citation_id, document_id,
        version_id, chunk_id, title, heading_path, content,
        start_offset, end_offset
      ) VALUES (?, 2, 2, 'C9', 'doc-9', 'ver-9', 'chunk-9', '坏行', NULL,
                '片段', NULL, 30)
      """,
      (conversation.conversation_id,),
    )
    connection.execute("PRAGMA ignore_check_constraints = OFF")

  history = service.get_history(
    context=CONTEXT, conversation_id=conversation.conversation_id
  )

  assert [item.citation_id for item in history.messages[1].citations] == ["C1"]


def test_history_never_returns_citations_for_invisible_messages(tmp_path):
  # 安全边界：白名单不变——带 tool_calls 的中间助手消息、system/tool 消息
  # 即使库里有引用行也一律不返回；用户消息恒为空引用。
  service, store = build_service(tmp_path, direct_answer_runner)
  conversation = service.create_conversation(context=CONTEXT)

  with sqlite3.connect(tmp_path / "chat.db") as connection:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
      """
      INSERT INTO messages(conversation_id, seq, role, payload_json, answer_incomplete)
      VALUES (?,
        1, 'system', '{"role":"system","content":"内部系统提示词"}', NULL),
        (?,
        2, 'user', '{"role":"user","content":"可见问题"}', NULL),
        (?,
        3, 'assistant',
        '{"role":"assistant","content":"中间消息","tool_calls":[{"id":"c"}]}', 1),
        (?,
        4, 'tool', '{"role":"tool","tool_call_id":"c","content":"工具结果"}', NULL),
        (?,
        5, 'assistant', '{"role":"assistant","content":"最终答案"}', 0)
      """,
      (
        conversation.conversation_id,
        conversation.conversation_id,
        conversation.conversation_id,
        conversation.conversation_id,
        conversation.conversation_id,
      ),
    )
    connection.execute(
      """
      INSERT INTO message_citations(
        conversation_id, seq, ordinal, citation_id, document_id,
        version_id, chunk_id, title, heading_path, content,
        start_offset, end_offset
      ) VALUES
        (?, 1, 1, 'C1', 'doc-1', 'ver-1', 'chunk-1', '系统行引用', NULL,
         '片段', 0, 2),
        (?, 2, 1, 'C2', 'doc-2', 'ver-2', 'chunk-2', '用户行引用', NULL,
         '片段', 0, 2),
        (?, 3, 1, 'C3', 'doc-3', 'ver-3', 'chunk-3', '中间消息引用', NULL,
         '片段', 0, 2),
        (?, 4, 1, 'C4', 'doc-4', 'ver-4', 'chunk-4', '工具行引用', NULL,
         '片段', 0, 2)
      """,
      (
        conversation.conversation_id,
        conversation.conversation_id,
        conversation.conversation_id,
        conversation.conversation_id,
      ),
    )

  history = service.get_history(
    context=CONTEXT, conversation_id=conversation.conversation_id
  )

  assert [(item.sequence, item.role) for item in history.messages] == [
    (2, "user"),
    (5, "assistant"),
  ]
  assert [item.citations for item in history.messages] == [[], []]
  assert [item.answer_incomplete for item in history.messages] == [False, False]


def test_citation_to_stored_dict_round_trips_through_stored_citation():
  # 保护行为：写路径（Citation → dict）与读路径（dict → Citation）互为逆运算，
  # 两个方向的映射各只有一处实现，往返必须逐字段相等。
  citation = _citation("C1")

  stored = citation_to_stored_dict(citation)
  restored = stored_citation_from_dict(stored)

  assert restored == citation
  assert set(stored) == set(citation.public_dict())


def test_stored_citation_from_dict_filters_extra_keys_and_rejects_broken_rows():
  # 边界情况：库里将来多一列时不得整条丢弃（先按字段名过滤再构造）；
  # 缺必填字段、类型非法或偏移不成对（只有一端）的行返回 None，由调用方丢弃。
  stored = dict(citation_to_stored_dict(_citation("C1")), future_column="x")
  assert stored_citation_from_dict(stored) == _citation("C1")
  assert stored_citation_from_dict({"citation_id": "C1"}) is None
  assert stored_citation_from_dict("not-a-dict") is None
  assert stored_citation_from_dict(
    dict(citation_to_stored_dict(_citation("C1")), start_offset=None)
  ) is None
  assert stored_citation_from_dict(
    dict(citation_to_stored_dict(_citation("C1")), start_offset=9, end_offset=3)
  ) is None
