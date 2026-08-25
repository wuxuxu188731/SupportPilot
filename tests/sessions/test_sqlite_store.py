import pytest

from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.sessions.base import ConversationNotFoundError
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
