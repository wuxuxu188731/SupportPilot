from contextlib import contextmanager
from threading import Lock
from typing import Iterator

class ConversationLockRegistry:
  def __init__(self):
    self._registry_guard = Lock() #lock字典本身也会被多个线程进行访问读写，所以需要用一个锁来进行保护防止竞态
    self._locks : dict[str, Lock] = {}

  @contextmanager
  def acquire(self, conversation_id : str)->Iterator[None]:
    with self._registry_guard :
      conversation_lock = self._locks.setdefault(
        conversation_id,
        Lock(),
      )

    with conversation_lock :
      yield