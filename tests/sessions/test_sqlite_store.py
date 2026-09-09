import pytest

from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.sessions.base import ConversationNotFoundError, MessageRecord
from app.sessions.sqlite_store import SQLiteSessionStore
from app.users.sqlite_store import SQLiteUserStore


def build_scope(tmp_path):
    database_path = tmp_path / "chat.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    sessions = SQLiteSessionStore(database_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    org_a = organizations.create_with_admin(
        name="Organization A",
        admin_user_id=alice.user_id,
    )
    org_b = organizations.create_with_admin(
        name="Organization B",
        admin_user_id=bob.user_id,
    )
    return sessions, alice, bob, org_a, org_b


def build_scope_with_shared_org(tmp_path):
    """构造 alice 与 bob 同属企业 A、bob 另有企业 B 的测试环境。"""
    sessions, alice, bob, org_a, org_b = build_scope(tmp_path)
    organizations = SQLiteOrganizationStore(tmp_path / "chat.db")
    organizations.add_membership(
        organization_id=org_a.organization_id,
        user_id=bob.user_id,
        role=MembershipRole.AGENT,
    )
    return sessions, alice, bob, org_a, org_b


def test_conversation_survives_store_recreation(tmp_path):
    store, alice, _, org_a, _ = build_scope(tmp_path)
    first_store = store

    created = first_store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        system_prompt="你是助手 A",
    )

    second_store = SQLiteSessionStore(tmp_path / "chat.db")
    loaded = second_store.get_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        conversation_id=created.conversation_id,
    )

    assert loaded == created


def test_get_conversation_hides_wrong_owner(tmp_path):
    store, alice, bob, org_a, _ = build_scope(tmp_path)
    created = store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
    )

    with pytest.raises(ConversationNotFoundError):
        store.get_conversation(
            organization_id=org_a.organization_id,
            user_id=bob.user_id,
            conversation_id=created.conversation_id,
        )


def test_get_conversation_hides_wrong_organization(tmp_path):
    store, alice, _, org_a, org_b = build_scope(tmp_path)
    conversation = store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
    )

    with pytest.raises(ConversationNotFoundError):
        store.get_conversation(
            organization_id=org_b.organization_id,
            user_id=alice.user_id,
            conversation_id=conversation.conversation_id,
        )


def test_get_conversation_still_hides_wrong_user(tmp_path):
    store, alice, bob, org_a, _ = build_scope(tmp_path)
    conversation = store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
    )

    with pytest.raises(ConversationNotFoundError):
        store.get_conversation(
            organization_id=org_a.organization_id,
            user_id=bob.user_id,
            conversation_id=conversation.conversation_id,
        )


def test_update_system_prompt_is_persistent(tmp_path):
    store, alice, _, org_a, _ = build_scope(tmp_path)
    created = store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
    )

    store.update_system_prompt(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        conversation_id=created.conversation_id,
        system_prompt="新的系统提示词",
    )

    reopened = SQLiteSessionStore(tmp_path / "chat.db")
    loaded = reopened.get_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        conversation_id=created.conversation_id,
    )
    assert loaded.system_prompt == "新的系统提示词"


def test_messages_round_trip_in_order(tmp_path):
    store, alice, _, org_a, _ = build_scope(tmp_path)
    conversation = store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
    )

    messages = [
      {
        "role":"user",
        "content":"调用工具"
      },
      {
        "role":"assistant",
        "content":"",
        "resoning_content":"需要调用工具",
        "tool_calls":[
          {
            "id":"call-1",
            "type":"function",
            "function":{
              "name":"get_food",
              "auguments":"{}"
            }
          }
        ]
      },
      {
        "role":"tool",
        "tool_call_id":"call-1",
        "content":"result"
      }
    ]

    store.append_messages(
      conversation_id=conversation.conversation_id,
      organization_id=org_a.organization_id,
      user_id=alice.user_id,
      messages=messages
    )

    assert store.load_messages(
      organization_id=org_a.organization_id,
      user_id=alice.user_id,
      conversation_id=conversation.conversation_id,
    ) == messages


def test_messages_are_isolated_by_conversation(tmp_path):
    store, alice, _, org_a, _ = build_scope(tmp_path)
    first = store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
    )
    second = store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
    )
    messages = [{"role":"user","content":"only first"}]
    store.append_messages(
      conversation_id=first.conversation_id,
      organization_id=org_a.organization_id,
      user_id=alice.user_id,
      messages=messages
    )
    assert store.load_messages(
      organization_id=org_a.organization_id,
      user_id=alice.user_id,
      conversation_id=first.conversation_id,
    ) == messages
    assert store.load_messages(
      organization_id=org_a.organization_id,
      user_id=alice.user_id,
      conversation_id=second.conversation_id,
    ) == []


def test_message_ownership_is_enforced(tmp_path):
    store, alice, bob, org_a, _ = build_scope(tmp_path)
    conversation = store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
    )

    with pytest.raises(ConversationNotFoundError):
        store.append_messages(
          conversation_id=conversation.conversation_id,
          organization_id=org_a.organization_id,
          user_id=bob.user_id,
          messages=[{"role":"user","content":"not allowed"}]
        )

    with pytest.raises(ConversationNotFoundError):
        store.load_messages(
          organization_id=org_a.organization_id,
          user_id=bob.user_id,
          conversation_id=conversation.conversation_id,
        )


def test_update_system_prompt_hides_wrong_owner(tmp_path):
    store, alice, bob, org_a, _ = build_scope(tmp_path)
    conversation = store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
    )

    with pytest.raises(ConversationNotFoundError):
      store.update_system_prompt(
        organization_id=org_a.organization_id,
        user_id=bob.user_id,
        conversation_id=conversation.conversation_id,
        system_prompt="not allowed",
      )


def _set_updated_at(database_path, conversation_id, updated_at_text):
    """测试辅助：直接改写会话的更新时间文本，用于制造确定性的排序场景。"""
    import sqlite3
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (updated_at_text, conversation_id),
        )


def test_list_conversations_scoped_to_current_org_and_user(tmp_path):
    # 保护行为：会话列表必须只返回「当前企业 + 当前用户」自己的会话；
    # 同企业其他用户的会话与其它企业的会话都不可见（跨租户隔离）。
    store, alice, bob, org_a, org_b = build_scope_with_shared_org(tmp_path)
    alice_in_a = store.create_conversation(
        organization_id=org_a.organization_id, user_id=alice.user_id,
    )
    bob_in_a = store.create_conversation(
        organization_id=org_a.organization_id, user_id=bob.user_id,
    )
    bob_in_b = store.create_conversation(
        organization_id=org_b.organization_id, user_id=bob.user_id,
    )

    alice_list = store.list_conversations(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        limit=50,
        offset=0,
    )
    assert [item.conversation_id for item in alice_list] == [alice_in_a.conversation_id]

    bob_list_in_a = store.list_conversations(
        organization_id=org_a.organization_id,
        user_id=bob.user_id,
        limit=50,
        offset=0,
    )
    assert [item.conversation_id for item in bob_list_in_a] == [bob_in_a.conversation_id]

    bob_list_in_b = store.list_conversations(
        organization_id=org_b.organization_id,
        user_id=bob.user_id,
        limit=50,
        offset=0,
    )
    assert [item.conversation_id for item in bob_list_in_b] == [bob_in_b.conversation_id]


def test_list_conversations_orders_by_updated_desc_then_id_desc(tmp_path):
    # 保护行为：会话列表必须按 updated_at 倒序；同一秒（文本相同）时
    # 以会话 id 倒序作为稳定次级排序，保证分页顺序可复现。
    store, alice, _, org_a, _ = build_scope(tmp_path)
    older = store.create_conversation(
        organization_id=org_a.organization_id, user_id=alice.user_id,
    )
    newer = store.create_conversation(
        organization_id=org_a.organization_id, user_id=alice.user_id,
    )
    # 手工改写时间：older 明显更早，newer 明显更晚，便于稳定断言
    _set_updated_at(tmp_path / "chat.db", older.conversation_id, "2026-01-01 08:00:00")
    _set_updated_at(tmp_path / "chat.db", newer.conversation_id, "2026-09-09 08:00:00")

    result = store.list_conversations(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        limit=50,
        offset=0,
    )
    assert [item.conversation_id for item in result] == [
        newer.conversation_id,
        older.conversation_id,
    ]

    # 相同 updated_at 时按会话 id 倒序，与 SQL 排序语义一致（稳定性检查）
    tied_a = store.create_conversation(
        organization_id=org_a.organization_id, user_id=alice.user_id,
    )
    tied_b = store.create_conversation(
        organization_id=org_a.organization_id, user_id=alice.user_id,
    )
    _set_updated_at(tmp_path / "chat.db", tied_a.conversation_id, "2026-09-10 08:00:00")
    _set_updated_at(tmp_path / "chat.db", tied_b.conversation_id, "2026-09-10 08:00:00")
    tied_result = store.list_conversations(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        limit=50,
        offset=0,
    )
    assert [item.conversation_id for item in tied_result[:2]] == sorted(
        [tied_a.conversation_id, tied_b.conversation_id], reverse=True
    )


def test_list_conversations_pagination_limit_and_offset(tmp_path):
    # 保护行为：limit/offset 分页必须真实生效，不能返回越界记录。
    store, alice, _, org_a, _ = build_scope(tmp_path)
    created = [
        store.create_conversation(
            organization_id=org_a.organization_id, user_id=alice.user_id,
        )
        for _ in range(4)
    ]

    page_one = store.list_conversations(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        limit=2,
        offset=0,
    )
    page_two = store.list_conversations(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        limit=2,
        offset=2,
    )
    assert len(page_one) == 2
    assert len(page_two) == 2
    seen = [item.conversation_id for item in page_one + page_two]
    # 两页合起来恰好覆盖全部会话且不重复
    assert sorted(seen) == sorted(item.conversation_id for item in created)


def test_list_conversations_exposes_first_user_message_content(tmp_path):
    # 保护行为：会话记录应携带首条用户消息正文（标题推导数据源），
    # 尚无消息的空会话该字段为 None，且不暴露内部 payload 其它字段。
    store, alice, _, org_a, _ = build_scope(tmp_path)
    conversation = store.create_conversation(
        organization_id=org_a.organization_id, user_id=alice.user_id,
    )
    store.append_messages(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        conversation_id=conversation.conversation_id,
        messages=[
            {"role": "user", "content": "第一条问题"},
            {"role": "assistant", "content": "回答一"},
            {"role": "user", "content": "第二条问题"},
        ],
    )
    empty_conversation = store.create_conversation(
        organization_id=org_a.organization_id, user_id=alice.user_id,
    )

    result = store.list_conversations(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        limit=50,
        offset=0,
    )
    by_id = {item.conversation_id: item for item in result}
    assert by_id[conversation.conversation_id].first_user_content == "第一条问题"
    assert by_id[empty_conversation.conversation_id].first_user_content is None


def test_get_conversation_record_hides_other_users_and_orgs(tmp_path):
    # 保护行为：单条会话记录读取同样受归属约束，
    # 跨用户或跨企业访问必须抛 NotFound，防止资源枚举。
    store, alice, bob, org_a, org_b = build_scope_with_shared_org(tmp_path)
    conversation = store.create_conversation(
        organization_id=org_a.organization_id, user_id=alice.user_id,
    )

    with pytest.raises(ConversationNotFoundError):
        store.get_conversation_record(
            organization_id=org_a.organization_id,
            user_id=bob.user_id,
            conversation_id=conversation.conversation_id,
        )
    with pytest.raises(ConversationNotFoundError):
        store.get_conversation_record(
            organization_id=org_b.organization_id,
            user_id=alice.user_id,
            conversation_id=conversation.conversation_id,
        )
    # 合法读取应返回记录本身（含时间字段）
    record = store.get_conversation_record(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        conversation_id=conversation.conversation_id,
    )
    assert record.conversation_id == conversation.conversation_id
    assert record.created_at
    assert record.updated_at
    assert record.first_user_content is None


def test_load_message_records_returns_seq_sorted_full_payload(tmp_path):
    # 保护行为：历史消息读取应按 seq 升序且保留完整原始载荷，
    # 保证服务层安全过滤的数据源未被破坏（模型上下文不受影响）。
    store, alice, _, org_a, _ = build_scope(tmp_path)
    conversation = store.create_conversation(
        organization_id=org_a.organization_id, user_id=alice.user_id,
    )
    raw_messages = [
        {"role": "user", "content": "问题", "reasoning_content": "内部推理"},
        {"role": "assistant", "content": "思考过程", "reasoning_content": "推理"},
        {"role": "tool", "tool_call_id": "call-1", "content": "原始工具结果"},
    ]
    store.append_messages(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        conversation_id=conversation.conversation_id,
        messages=raw_messages,
    )

    records = store.load_message_records(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
        conversation_id=conversation.conversation_id,
    )
    assert [record.sequence for record in records] == [1, 2, 3]
    assert [record.role for record in records] == ["user", "assistant", "tool"]
    assert [record.payload for record in records] == raw_messages
    assert all(isinstance(record.created_at, str) and record.created_at for record in records)
    assert isinstance(records[0], MessageRecord)


def test_load_message_records_enforces_ownership(tmp_path):
    # 边界情况：不属于当前用户/企业的会话读取历史必须抛 NotFound，
    # 与聊天时用于模型上下文读取的隔离行为保持一致。
    store, alice, bob, org_a, _ = build_scope_with_shared_org(tmp_path)
    conversation = store.create_conversation(
        organization_id=org_a.organization_id, user_id=alice.user_id,
    )

    with pytest.raises(ConversationNotFoundError):
        store.load_message_records(
            organization_id=org_a.organization_id,
            user_id=bob.user_id,
            conversation_id=conversation.conversation_id,
        )
