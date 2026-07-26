from functools import partial

from fastapi import FastAPI

from app.agent.runner import run_one_turn
from app.api.router import creat_conversation_router
from app.api.auth_router import create_auth_router
from app.api.dependencies import create_current_user_dependency
from app.application.chat_service import ChatService
from app.application.auth_service import AuthService,AccessTokenService
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.core.config import (
  create_llm_client,
  get_chat_db_path,
  get_auth_secret_key,
  get_access_token_ttl_seconds
)
from app.sessions.sqlite_store import SQLiteSessionStore
from app.users.sqlite_store import SQLiteUserStore
from app.tools.registry import TOOL_FUNCTIONS,TOOL_DEFINITIONS



app = FastAPI()
client = create_llm_client() 

database_path = get_chat_db_path()

session_store = SQLiteSessionStore(database_path=database_path)
user_store = SQLiteUserStore(database_path=database_path)

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
token_service = AccessTokenService(
  secret_key=get_auth_secret_key(),
  ttl_seconds=get_access_token_ttl_seconds()
)
auth_service=AuthService(
  user_store=user_store,
  token_service=token_service
)
get_current_user = create_current_user_dependency(auth_service=auth_service)

app.include_router(
  creat_conversation_router(
    chat_service=chat_service,
    get_current_user=get_current_user
  )
)
app.include_router(
  create_auth_router(
    auth_service=auth_service,
    get_current_user=get_current_user
  )
)
