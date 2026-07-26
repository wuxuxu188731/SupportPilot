import sqlite3

import pytest

from app.application.chat_service import ChatService
from app.application.organization_service import TenantContext
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.organizations.base import MembershipRole
from app.schemas.chat import LLMResponse
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


def build_service(tmp_path, runner):
  store = SQLiteSessionStore(tmp_path / "chat.db")
  _seed_tenant_scope(tmp_path / "chat.db")
  service = ChatService(
    store = store,
    run_agent = runner,
    locks = ConversationLockRegistry()
  )
  return service, store

def direct_answer_runner(*, messages : list[dict])->LLMResponse:
  messages.append({"role":"assistant","content":"answer"})
  return LLMResponse(
    llm_answer="answer"
  )


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

  def recording_runner(*, messages):
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
    { "role":"system", "content":"你是测试助手"},
    { "role":"user", "content":"first"},
    { "role":"assistant", "content":"answer"},
    { "role":"user", "content":"second"}
  ]


def test_failed_runner_does_not_persist_partial_turn(tmp_path):
  def fail_runner(*, messages : list[dict]):
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

  def controlled_runner(*, messages):
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

  def synchronized_runner(*, messages):
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
