from fastapi.testclient import TestClient
from app.api.router import creat_router
from fastapi import FastAPI

def fake_run_agent(messages : list[dict]):
  from app.schemas.chat import LLMResponse
  return LLMResponse(llm_answer=None,llm_reasoning_content=None,events=[])

def fake_save_history(messages : list[dict]):
  pass

def test_router_smoke():
  app = FastAPI()
  messages=[]
  router = creat_router(
    messages=messages,
    run_agent=fake_run_agent,
    save_history=fake_save_history
  )
  app.include_router(router=router)

  client = TestClient(app)

  response = client.post("/setSys/",json={"prompt":"你是一个助手"})
  assert response.status_code != 404
  response_1 = client.post("/chat/",json={"question":"Hello"})
  assert response_1.status_code != 404