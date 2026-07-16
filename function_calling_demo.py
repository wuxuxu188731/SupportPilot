from openai import OpenAI
import os
import json

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]

client = OpenAI(api_key=DEEPSEEK_API_KEY,base_url="https://api.deepseek.com")



def read_history_chat()->list[dict]:
  with open("chat_history.json",mode="r",encoding="utf-8") as f:
    return json.load(f)

def save_history_chat(messages:list[dict]):
  with open("chat_history.json",mode="w",encoding="utf-8") as f:
    json.dump(messages,f,indent=2,ensure_ascii=False,)

def get_food()->any:
  return "其实啥也没有喵-v-"

def send_popup_to_user(content:str):
  return f"成功向用户发一个弹窗，弹窗的内容是{content}"

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
      "description":"当你想提醒用户某件事情的时候就可以调用喵，直接向用户发出弹窗提醒用户喵",
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

print(messages)


def run_one_turn(messages : list[dict]):
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

    if not response.choices[0].message.tool_calls:
      print(f"LLM answer:{llm_res}")
      print(f"LLM reasoning content:{reason_content}")
      print()
      print(f"tool_calls:{response.choices[0].message.tool_calls}")
      messages.append(
        {
          "role":"assistant",
          "content":llm_res,
        }
      )
      break 
    
    messages.append(
      {
        "role":"assistant",
        "content":llm_res,
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
      func_arguments = json.loads(tool_call.function.arguments)
      func = function_map[func_name]
      func_result = func(**func_arguments)
      print(f"tool_call: {func_name},result:{func_result}")
      messages.append(
        {
          "role":"tool",
          "name":func_name,
          "tool_call_id":tool_call.id,
          "content":func_result
        }
      )
    
    


while True:
  print("-"*50)
  user_question = input("输入问题：").strip()
  print("-"*50)
  if not user_question:
    continue
  messages.append(
      {
        "role":"user",
        "content":user_question
      }
    )
  run_one_turn(messages=messages)
  save_history_chat(messages=messages)

