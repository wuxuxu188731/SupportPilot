import pytest

from app.sessions.base import ConversationNotFoundError
from app.sessions.sqlite_store import SQLiteSessionStore


def test_conversation_survives_store_recreation(tmp_path):
    database_path = tmp_path / "chat.db"
    first_store = SQLiteSessionStore(database_path)

    created = first_store.create_conversation(
        user_id="user-a",
        system_prompt="你是助手 A",
    )

    second_store = SQLiteSessionStore(database_path)
    loaded = second_store.get_conversation(
        user_id="user-a",
        conversation_id=created.conversation_id,
    )

    assert loaded == created


def test_get_conversation_hides_wrong_owner(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    created = store.create_conversation(user_id="user-a")

    with pytest.raises(ConversationNotFoundError):
        store.get_conversation(
            user_id="user-b",
            conversation_id=created.conversation_id,
        )


def test_update_system_prompt_is_persistent(tmp_path):
    database_path = tmp_path / "chat.db"
    store = SQLiteSessionStore(database_path)
    created = store.create_conversation(user_id="user-a")

    store.update_system_prompt(
        user_id="user-a",
        conversation_id=created.conversation_id,
        system_prompt="新的系统提示词",
    )

    reopened = SQLiteSessionStore(database_path)
    loaded = reopened.get_conversation(
        user_id="user-a",
        conversation_id=created.conversation_id,
    )
    assert loaded.system_prompt == "新的系统提示词"


#按顺序发送测试消息并返回
def test_messages_round_trip_in_order(tmp_path):
    database_path = tmp_path / "chat.db"
    store = SQLiteSessionStore(database_path=database_path)
    conversation = store.create_conversation(user_id="user-a")
    
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
      conversation_id = conversation.conversation_id,
      user_id = conversation.user_id,
      messages = messages
    )

    assert store.load_messages(conversation_id=conversation.conversation_id,user_id="user-a")==messages

#测试消息按对话进行隔离
def test_messages_are_isolated_by_conversation(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    first = store.create_conversation(user_id="user-a")   #用户a的第一个conversation
    second = store.create_conversation(user_id="user-a")  #用户a的第二个conversation
    messages = [{"role":"user","content":"only first"}]
    store.append_messages(
      conversation_id = first.conversation_id,
      user_id = "user-a",
      messages = messages
    )
    assert store.load_messages(conversation_id=first.conversation_id,user_id="user-a")==messages
    assert store.load_messages(conversation_id=second.conversation_id,user_id="user-a")==[]

#验证存储层做了会话所有权隔离,每个用户不能访问处自己之外的会话
def test_message_ownership_is_enforced(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    conversation = store.create_conversation(user_id="user-a")

    with pytest.raises(ConversationNotFoundError):
        store.append_messages(
          conversation_id = conversation.conversation_id,
          user_id = "user-b",
          messages = [{"role":"user","content":"not allowed"}]
        )

    with pytest.raises(ConversationNotFoundError):
        store.load_messages(
          conversation_id = conversation.conversation_id,
          user_id = "user-b"
        )


def test_update_system_prompt_hides_wrong_owner(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    conversation = store.create_conversation(user_id="user-a")

    with pytest.raises(ConversationNotFoundError):
      store.update_system_prompt(
        user_id="user-b",
        conversation_id=conversation.conversation_id,
        system_prompt="not allowed",
      )