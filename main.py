from fastapi import FastAPI
from functools import partial

from app.tools.registry import TOOL_FUNCTIONS,TOOL_DEFINITIONS
from app.agent.runner import run_one_turn
from app.core.config import create_llm_client
from app.sessions.legacy_file import read_history_chat,save_history_chat
from app.api.router import creat_router

app = FastAPI()

client = create_llm_client() 
function_map = TOOL_FUNCTIONS
tools = TOOL_DEFINITIONS
messages = read_history_chat()

run_agent = partial(
  run_one_turn,
  client = client,
  tool_definitions = TOOL_DEFINITIONS,
  tool_functions = TOOL_FUNCTIONS,
  on_event = None
)

app.include_router(
  creat_router(
    messages=messages,
    run_agent=run_agent,
    save_history=save_history_chat
  )
)


  