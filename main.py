from fastapi import FastAPI,HTTPException
from pydantic import BaseModel,Field
from openai import OpenAI
from datetime import datetime,timezone
from typing import Literal,Callable,Any
import os
import json
import time

def utc_now()->datetime:
  return datetime.now(timezone.utc)

def stringify_tool_result(result : Any)->str:
  if isinstance(result,str):
    return result
  else:
    return json.dumps(result,ensure_ascii=False,default=str)

class AgentEvent(BaseModel):
  type : Literal[
    "tool_call.requested",
    "tool_call.started",
    "tool_call.completed",
    "tool_call.failed"
  ]
  timestamp : datetime = Field(default_factory=datetime.now)
  tool_call_id : str
  tool_call_name : str
  tool_call_arguments : dict[str,Any] = Field(default_factory=dict)
  result : Any | None = None
  error : str | None = None
  duration_ms : float | None = None

class LLMResponse(BaseModel):
  """llm_tool_call is function name which llm called"""
  llm_answer : str
  llm_reasoning_content : str
  events : list[AgentEvent] = Field(default_factory=list) 

class SystemPrompt(BaseModel):
  prompt : str

class UserQuestion(BaseModel):
  question : str

app = FastAPI()


DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]

client = OpenAI(api_key=DEEPSEEK_API_KEY,base_url="https://api.deepseek.com")

def read_history_chat()->list[dict]:
  try:
    with open("chat_history.json",mode="r",encoding="utf-8") as f:
      return json.load(f)
  except FileNotFoundError as e:
    print("file of chat history does not exist")
    return []

def save_history_chat(messages:list[dict]):
  with open("chat_history.json",mode="w",encoding="utf-8") as f:
    json.dump(messages,f,indent=2,ensure_ascii=False,)

def get_food()->Any:
  return "其实啥也没有喵-v-"

def send_popup_to_user(content:str):
  return f"成功向用户发一个弹窗，弹窗的内容是{content}"

"""解析LLM调用工具时传入的参数"""
def parse_tool_arguments():
  

def run_one_turn(messages : list[dict], on_event : Callable[[AgentEvent],None])->LLMResponse:
  events : list[AgentEvent] = []

  #this function is used to add agent_event into events && extension operation
  def emit(event : AgentEvent):
    events.append(event)
    #database connection,SSE,websocket
    if(on_event is not None):
      on_event(event)


  while True:
    response = client.chat.completions.create(
      model="deepseek-v4-flash",
      messages=messages,
      tools=tools,
      extra_body={
        "thinking":{
          "type":"enabled"
        }
      }
    )

    llm_res = response.choices[0].message.content
    reason_content = response.choices[0].message.reasoning_content

    #no tool was called
    if not response.choices[0].message.tool_calls:
      """调试信息"""
      print(f"LLM answer:{llm_res}")
      print(f"LLM reasoning content:{reason_content}")
      print()
      print(f"tool_calls:{response.choices[0].message.tool_calls}")

      messages.append(
        {
          "role":"assistant",
          "content":llm_res,
          "reasoning_content":reason_content,
        }
      )
      return LLMResponse(llm_answer=llm_res,llm_reasoning_content=reason_content,events=events) 
    
    #tools were called
    """
    将模型发送的调用信息加入messages->遍历工具列表(对每一个工具进行)
    ->参数解析(异常处理:参数解析失败)
    ->从functionMap里面获取函数对象(异常处理:工具函数不存在)
    ->执行函数(异常处理:函数调用失败)
    ->返回工具结果(将函数的结果append进入messages)
    """
    messages.append(
      {
        "role":"assistant",
        "content":llm_res,
        "reasoning_content":reason_content,
        "tool_calls":[
          {
            "id":tool_call.id,
            "type":"function",
            "function":{
              "name":tool_call.function.name,
              "arguments":tool_call.function.arguments
            },
          }
        for tool_call in response.choices[0].message.tool_calls
        ]
      }
    )
    for tool_call in response.choices[0].message.tool_calls:
      func_name = tool_call.function.name
      try :
        func_arguments = json.loads(tool_call.function.arguments or "{}")
        if not isinstance(func_arguments,dict):
          raise ValueError("工具参数必须是JSON Object")
      except (json.JSONDecodeError,ValueError) as exc:
        error_message = f"工具参数解析失败"
        emit(
          AgentEvent(
            type="tool_call.failed",
            tool_call_id=tool_call.id,
            tool_call_name=func_name,
            tool_call_arguments="错误参数",
            error=error_message
          )
        )
        messages.append(
          {
            "role":"tool",
            "tool_call_id":tool_call.id,
            "content":json.dumps(
              {
                "ok":False,
                "error":error_message,
              },ensure_ascii=False,default=str
            )
          }
        )
        continue
      emit(
        AgentEvent(
          type="tool_call.requested",
          tool_call_id=tool_call.id,
          tool_call_name=func_name,
          tool_call_arguments=func_arguments
        )
      )

      func = function_map[func_name]
      #tool is not avaliable
      if func is None:
        error_message = f"未注册的工具"
        emit(
          AgentEvent(
            type="tool_call.failed",
            tool_call_id=tool_call.id,
            tool_call_name=func_name,
            tool_call_arguments=func_arguments
          )
        )
        func_result = json.dumps(
          {
            "OK":False,
            "content":error_message,
          },
          ensure_ascii=False
        )
        messages.append(
          {
            "role":"tool",
            "tool_call_id":tool_call.id,
            "content":func_result,
          }
        )
        continue

      #tool is avaliable
      emit(
        AgentEvent(
          type="tool_call.started",
          tool_call_id=tool_call.id,
          tool_call_name=func_name,
          tool_call_arguments=func_arguments
        )
      )

      start_at = time.perf_counter()
      try:
        func_result = func(**func_arguments)
        duration_ms = (time.perf_counter()-start_at)*1000
        emit(
          AgentEvent(
            type="tool_call.completed",
            tool_call_id=tool_call.id,
            tool_call_name=func_name,
            tool_call_arguments=func_arguments,
            result=func_result,
            duration_ms=duration_ms
          )
        )
      except Exception as exc:
        error_message = f"{type(exc).__name__}:{exc}"
        duration_ms = (time.perf_counter()-start_at)*1000
        emit(AgentEvent(
          type="tool_call.failed", 
          tool_call_id=tool_call.id, 
          tool_call_name=func_name, 
          tool_call_arguments=func_arguments,
          error=error_message,
          duration_ms=duration_ms
          )
        )
        func_result = json.dumps(
          {
            "OK":False,
            "error":error_message,
          },
          ensure_ascii=False
        )
      print(f"tool_call: {func_name},result:{func_result}")
      messages.append(
        {
          "role":"tool",
          "tool_call_id":tool_call.id,
          "content":func_result
        }
      )

function_map = {
  "get_food":get_food,
  "send_popup_to_user":send_popup_to_user
}

tools = [
  {
    "type":"function",
    "function":{
      "name":"get_food",
      "description":"饿了就可以调用喵",
      "parameters":{
        "type":"object",
        "properties":{}
      }
    },
  },
  {
    "type":"function",
    "function":{
      "name":"send_popup_to_user",
      "description":"当你想提醒用户某件事情的时候就可以调用，直接向用户发出弹窗提醒用户",
      "parameters":{
        "type":"object",
        "properties":{
          "content":{
            "type":"string",
            "description":"弹窗中的消息内容，.e.g 你在干什么呀为什么不理我"
          }
        }
      }
    }
  }
]

messages = []
messages = read_history_chat()

@app.post("/setSys",tags=["设置系统提示词"])
async def setSystemPrompt(prompt:SystemPrompt)->bool:
  sysprompt = prompt.prompt.strip()
  if not sysprompt:
    raise HTTPException(status_code=401,detail="系统提示词为空")
  try:
    messages.append(
      {
        "role":"system",
        "content":sysprompt
      }
    )
    return True
  except Exception as e:
    print(f"未知错误:{e}")
  return False

@app.post("/chat",response_model=LLMResponse,tags=["向模型聊天"])
def chat(ques:UserQuestion)->LLMResponse:
  question = ques.question
  messages.append(
    {
      "role":"user",
      "content":question
    }
  )
  resp = run_one_turn(messages=messages,on_event=None)
  save_history_chat(messages=messages)
  return resp
  

