from threading import Event,Thread

import pytest

from app.concurrency.conversation_locks import ConversationLockRegistry

#测试同一个会话的操作是串行执行(互斥排队)
def test_same_conversation_is_serialized():
  registry = ConversationLockRegistry()

  first_entered = Event()
  release_first = Event()
  second_entered = Event() 

  def first_request():
    with registry.acquire("conversation-1"):
      first_entered.set()  #True,表示线程1成功拿到了锁
      assert release_first.wait(timeout=2)

  def second_request():
    assert first_entered.wait(timeout=2)
    with registry.acquire("conversation-1"):
      second_entered.set()

  first = Thread(target=first_request)
  second = Thread(target=second_request)

  first.start()
  second.start()

  assert first_entered.wait(timeout=2)
  assert not second_entered.wait(timeout=0.1)
  release_first.set()
  first.join(timeout=2)
  second.join(timeout=2)
  assert second_entered.is_set()

#锁释放后能否正常获取
def test_lock_is_released_when_request_raises():
  registry = ConversationLockRegistry()

  with pytest.raises(RuntimeError):
    with registry.acquire("conversation-1"):
      raise RuntimeError("model failed")

    with registry.acquire("conversation-1"):
      acquired_again = True

    assert acquired_again is True