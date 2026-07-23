from functools import partial

from fastapi import FastAPI

from app.agent.runner import run_one_turn
from app.api.router import creat_router
from app.application.chat_service import ChatService
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.core.config import create_llm_client,get_chat_db_path
from app.sessions.sqlite_store import SQLiteSessionStore
from app.tools.registry import TOOL_FUNCTIONS,TOOL_DEFINITIONS



app = FastAPI()
client = create_llm_client() 

session_store = SQLiteSessionStore(database_path=get_chat_db_path())
run_agent = partial(
  run_one_turn,
  client = client,
  tool_definitions = TOOL_DEFINITIONS,
  tool_functions = TOOL_FUNCTIONS,
  on_event = None
)

chat_service = ChatService(
  store=session_store,
  run_agent=run_agent,
  locks=ConversationLockRegistry()
)

app.include_router(
  creat_router(
    chat_service=chat_service
  )
)
