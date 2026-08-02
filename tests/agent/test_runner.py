import pytest
import os
os.environ["DEEPSEEK_API_KEY"]="test-only-key"
from types import SimpleNamespace
from typing import Any



"""
agent编排测试，覆盖路径：
1.模型直接回答
2.单个工具成功。
3.工具返回字典，结果被转换成字符串。
4.参数 JSON 损坏，只产生 failed，不产生 completed。
5.调用了未注册工具。
6.工具函数抛异常，只产生 failed。
7.一次调用多个工具，其中一个成功、一个失败。
8.工具执行后，模型继续调用第二轮工具。
9.每条路径的 messages 顺序和事件顺序都正确。
"""

from app.agent.runner import AgentToolRoundLimitError, run_one_turn


def test_run_one_turn_uses_requested_model_name():
  received = []
  fake_message = SimpleNamespace(
    content="answer",
    reasoning_content="reasoning",
    tool_calls=None,
  )
  fake_response = SimpleNamespace(
    choices=[SimpleNamespace(message=fake_message)]
  )

  def create(**kwargs):
    received.append(kwargs)
    return fake_response

  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(create=create)
    )
  )

  run_one_turn(
    messages=[{"role": "user", "content": "hello"}],
    client=fake_client,
    tool_definitions=[],
    tool_functions={},
    model_name="test-support-model",
  )

  assert received[0]["model"] == "test-support-model"


def test_run_one_turn_stops_after_max_tool_rounds():
  call_count = 0

  def create(**kwargs):
    nonlocal call_count
    call_count += 1
    tool_call = SimpleNamespace(
      id=f"call-{call_count}",
      type="function",
      function=SimpleNamespace(name="get_food", arguments="{}"),
    )
    message = SimpleNamespace(
      content="",
      reasoning_content="continue calling tools",
      tool_calls=[tool_call],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])

  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(create=create)
    )
  )

  with pytest.raises(
    AgentToolRoundLimitError,
    match="maximum tool rounds exceeded",
  ):
    run_one_turn(
      messages=[{"role": "user", "content": "loop"}],
      client=fake_client,
      tool_definitions=TOOL_DEFINITIONS,
      tool_functions=TOOL_FUNCTIONS,
      max_tool_rounds=2,
    )

  assert call_count == 3


@pytest.mark.parametrize("max_tool_rounds", [0, -1])
def test_run_one_turn_requires_positive_tool_round_limit(max_tool_rounds):
  with pytest.raises(ValueError, match="max_tool_rounds must be positive"):
    run_one_turn(
      messages=[],
      client=SimpleNamespace(),
      tool_definitions=[],
      tool_functions={},
      max_tool_rounds=max_tool_rounds,
    )


#测试工具------------------
def get_food()->Any:
  return "其实啥也没有-v-"

def send_popup_to_user(content:str):
  return f"成功向用户发一个弹窗，弹窗的内容是{content}"

def dict_result_test()->dict:
  return {
    "name":"小明",
    "content":"Hello"
  }

def raise_error_func():
  raise ValueError("执行时异常")


tool_functions = {
  "get_food":get_food,
  "send_popup_to_user":send_popup_to_user,
  "dict_result_test":dict_result_test,
  "raise_error_func":raise_error_func,
}
tool_definitions = [
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
        },
        "required":["content"],
      }
    }
  },
  {
    "type":"function",
    "function":{
      "name":"dict_result_test",
      "description":"这是一个测试工具，用来测试返回字典的工具结果能否被合法的转成字符串",
      "parameters":{
        "type":"object",
        "properties":{}
      }
    }
  },
  {
    "type":"function",
    "function":{
      "name":"raise_error_func",
      "description":"这是一个测试工具，用来测试工具执行时发生异常能否正常处理",
      "parameters":{
        "type":"object",
        "properties":{}
      }
    }
  }
]

TOOL_DEFINITIONS = tool_definitions
TOOL_FUNCTIONS = tool_functions

#模型直接回答
def test_1(monkeypatch):
  # 1. 构造假的模型消息
  fake_message = SimpleNamespace(
    content="你好，我是假模型的回答",
    reasoning_content="这是假的推理内容",
    tool_calls=None,
  )

  # 2. 构造假的模型响应
  fake_response = SimpleNamespace(
    choices=[
        SimpleNamespace(message=fake_message)
    ]
  )

  # 3. 构造假的客户端
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=lambda **kwargs: fake_response
      )
    )
  )

  messages = [
    {
      "role": "user",
      "content": "Hello",
    }
  ]

  # 这里执行的是真实 run_one_turn，但调用的是假客户端
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  # 5. 验证 Agent 的真实行为
  assert result.llm_answer == "你好，我是假模型的回答"
  assert result.llm_reasoning_content == "这是假的推理内容"
  assert result.events == []

  # run_one_turn 应当把最终回答加入消息历史
  assert messages == [
    {
      "role": "user",
      "content": "Hello",
    },
    {
      "role": "assistant",
      "content": "你好，我是假模型的回答",
      "reasoning_content": "这是假的推理内容",
    },
  ]



#调用单次工具
def test_2(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="get_food",
      arguments="{}"
    ),
  )
  fake_tool_message = SimpleNamespace(
    content="",
    reasoning_content="我需要调用get_food工具",
    tool_calls=[fake_tool_calls]
  )
  fake_final_message = SimpleNamespace(
    content="调用工具完成",
    reasoning_content="调用了get_food",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你调用get_food工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  #测试AgentEvent是否正确
  events = result.events
  assert len(events)==3
  assert events[0].type=="tool_call.requested" and events[1].type=="tool_call.started" and events[2].type=="tool_call.completed"
  assert events[0].tool_call_id=="tool_call_id_1" and events[1].tool_call_id=="tool_call_id_1" and events[2].tool_call_id=="tool_call_id_1"
  assert events[0].tool_call_name=="get_food" and events[1].tool_call_name=="get_food" and events[2].tool_call_name=="get_food"

  assert messages==[
    {
      "role":"user",
      "content":"请你调用get_food工具"
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"我需要调用get_food工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"get_food",
            "arguments":"{}"
          },
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":"其实啥也没有-v-"
    },
    {
      "role":"assistant",
      "content":"调用工具完成",
      "reasoning_content":"调用了get_food",
    }
  ]
  


def test_3(monkeypatch):
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="dict_result_test",
      arguments="{}"
    ),
  )
  fake_tool_message = SimpleNamespace(
    content="",
    reasoning_content="我需要调用dict_result_test工具",
    tool_calls=[fake_tool_calls]
  )
  fake_final_message = SimpleNamespace(
    content="调用工具完成",
    reasoning_content="调用了dict_result_test",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你调用dict_result_test工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  assert messages==[
    {
      "role":"user",
      "content":"请你调用dict_result_test工具"
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"我需要调用dict_result_test工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"dict_result_test",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":'{"name": "小明", "content": "Hello"}'
    },
    {
      "role":"assistant",
      "content":"调用工具完成",
      "reasoning_content":"调用了dict_result_test"
    }
  ]


#参数 JSON 损坏，只产生 failed，不产生 completed。
def test_4(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="get_food",
      arguments="{"
    ),
  )
  fake_tool_calls_turn_1 = SimpleNamespace(
    id="tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="get_food",
      arguments="{}"
    )
  )
  fake_tool_message = SimpleNamespace(
    content="",
    reasoning_content="我需要调用get_food工具",
    tool_calls=[fake_tool_calls]
  )
  fake_tool_message_turn_1 = SimpleNamespace(
    content="",
    reasoning_content="工具参数错误，让我传入正确的工具参数",
    tool_calls=[fake_tool_calls_turn_1]
  )
  fake_final_message = SimpleNamespace(
    content="调用工具完成",
    reasoning_content="调用了get_food",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_tool_message_turn_1_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message_turn_1)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_tool_message_turn_1_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你调用get_food工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  #测试AgentEvent是否正确,尝试调用(错误参数)->收到错误消息->再次调用(正确参数)->进行回答
  #faild request start complete
  events = result.events
  assert len(events)==4
  assert events[0].type=="tool_call.failed" and events[1].type=="tool_call.requested" and events[2].type=="tool_call.started" and events[3].type=="tool_call.completed"
  
  print(messages)

  assert messages==[
    {
      "role":"user",
      "content":"请你调用get_food工具"
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"我需要调用get_food工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"get_food",
            "arguments":"{"
          },
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":'{"OK": false, "error": "工具参数解析失败"}'
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"工具参数错误，让我传入正确的工具参数",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"get_food",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":"其实啥也没有-v-"
    },
    {
      "role":"assistant",
      "content":"调用工具完成",
      "reasoning_content":"调用了get_food",
    }
  ]

#5.调用了未注册工具。
def test_5(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="unknow_tool",
      arguments="{}"
    ),
  )
  fake_tool_message = SimpleNamespace(
    content="",
    reasoning_content="我需要调用unknow_tool工具",
    tool_calls=[fake_tool_calls]
  )
  fake_final_message = SimpleNamespace(
    content="unknow_tool这个工具不存在",
    reasoning_content="没有unknow_tool这个工具",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你调用unknow_tool工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  events = result.events
  assert events[0].type=="tool_call.requested" and events[1].type=="tool_call.failed"
  assert len(events)==2

  print(messages)

  assert messages==[
    {
      "role":"user",
      "content":"请你调用unknow_tool工具"
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"我需要调用unknow_tool工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"unknow_tool",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":'{"OK": false, "error": "未注册的工具"}'
    },
    {
      "role":"assistant",
      "content":"unknow_tool这个工具不存在",
      "reasoning_content":"没有unknow_tool这个工具"
    }
  ]

#6.工具函数抛异常，只产生 failed。
def test_6(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="raise_error_func",
      arguments="{}"
    ),
  )
  fake_tool_message = SimpleNamespace(
    content="",
    reasoning_content="我需要调用raise_error_func工具",
    tool_calls=[fake_tool_calls]
  )
  fake_final_message = SimpleNamespace(
    content="raise_error_func这个工具抛出了异常",
    reasoning_content="让我告诉用户这个工具抛出了异常",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你调用raise_error_func工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  events = result.events
  #request -> start ->faild
  assert events[0].type=="tool_call.requested" and events[1].type=="tool_call.started" and events[2].type=="tool_call.failed"
  assert messages==[
    {
      "role":"user",
      "content":"请你调用raise_error_func工具"
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"我需要调用raise_error_func工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"raise_error_func",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":'{"OK": false, "error": "ValueError:执行时异常"}'
    },
    {
      "role":"assistant",
      "content":"raise_error_func这个工具抛出了异常",
      "reasoning_content":"让我告诉用户这个工具抛出了异常"
    }
  ]

#7.一次调用多个工具，其中一个成功、一个失败。
def test_7(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="raise_error_func",
      arguments="{}"
    ),
  )
  fake_tool_calls_1 = SimpleNamespace(
    id = "tool_call_id_2",
    type="function",
    function=SimpleNamespace(
      name="get_food",
      arguments="{}"
    )
  )
  fake_tool_message = SimpleNamespace(
    content="同时调用两个工具",
    reasoning_content="我需要调用raise_error_func和get_food工具",
    tool_calls=[fake_tool_calls,fake_tool_calls_1]
  )
  fake_final_message = SimpleNamespace(
    content="raise_error_func这个工具抛出了异常,get_food工具执行成功",
    reasoning_content="我得到了两个工具的结果",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你同时调用raise_error_func和get_food工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  events = result.events
  for event in events:
    print(f"tool_call_id:{event.tool_call_id} type:{event.type}")
  assert events[0].tool_call_id=="tool_call_id_1" and events[0].type=="tool_call.requested"
  assert events[1].tool_call_id=="tool_call_id_1" and events[1].type=="tool_call.started"
  assert events[2].tool_call_id=="tool_call_id_1" and events[2].type=="tool_call.failed"
  assert events[3].tool_call_id=="tool_call_id_2" and events[3].type=="tool_call.requested"
  assert events[4].tool_call_id=="tool_call_id_2" and events[4].type=="tool_call.started"
  assert events[5].tool_call_id=="tool_call_id_2" and events[5].type=="tool_call.completed"
  assert messages==[
    {
      "role":"user",
      "content":"请你同时调用raise_error_func和get_food工具"
    },
    {
      "role":"assistant",
      "content":"同时调用两个工具",
      "reasoning_content":"我需要调用raise_error_func和get_food工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"raise_error_func",
            "arguments":"{}"
          }
        },
        {
          "id":"tool_call_id_2",
          "type":"function",
          "function":{
            "name":"get_food",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":'{"OK": false, "error": "ValueError:执行时异常"}'
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_2",
      "content":"其实啥也没有-v-"
    },
    {
      "role":"assistant",
      "content":"raise_error_func这个工具抛出了异常,get_food工具执行成功",
      "reasoning_content":"我得到了两个工具的结果"
    }
  ]


#8.工具执行后，模型继续调用第二轮工具。
def test_8(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="get_food",
      arguments="{}"
    ),
  )
  fake_tool_calls_1 = SimpleNamespace(
    id = "tool_call_id_2",
    type="function",
    function=SimpleNamespace(
      name="send_popup_to_user",
      arguments='{"content":"Hello,hi"}'
    )
  )
  fake_tool_message = SimpleNamespace(
    content="让我先调用get_food，得到结果后继续调用send_popup_to_user",
    reasoning_content="我需要先调用get_food工具",
    tool_calls=[fake_tool_calls]
  )
  fake_tool_message_1 = SimpleNamespace(
    content="我已经获取到了get_food的结果，现在让我调用send_popup_to_user",
    reasoning_content="假的思考过程",
    tool_calls=[fake_tool_calls_1]
  )
  fake_final_message = SimpleNamespace(
    content="先调用了get_food，然后调用了send_popup_to_user",
    reasoning_content="调用结束",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_tool_response_1 = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message_1)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_tool_response_1,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你连续调用工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  events = result.events
  for eve in events:
    print(eve.type)
  #tool_call_id_1:request->start->complete
  #tool_call_id_2:request->start->complete
  assert (
    events[0].type=="tool_call.requested" and 
    events[1].type=="tool_call.started" and 
    events[2].type=="tool_call.completed" and
    events[0].tool_call_id=="tool_call_id_1" and
    events[1].tool_call_id=="tool_call_id_1" and
    events[2].tool_call_id=="tool_call_id_1" 
  )
  assert (
    events[3].type=="tool_call.requested" and 
    events[4].type=="tool_call.started" and 
    events[5].type=="tool_call.completed" and
    events[3].tool_call_id=="tool_call_id_2" and
    events[4].tool_call_id=="tool_call_id_2" and
    events[5].tool_call_id=="tool_call_id_2" 
  )
  assert messages==[
    {
      "role":"user",
      "content":"请你连续调用工具"
    },
    {
      "role":"assistant",
      "content":"让我先调用get_food，得到结果后继续调用send_popup_to_user",
      "reasoning_content":"我需要先调用get_food工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"get_food",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":"其实啥也没有-v-"
    },
    {
      "role":"assistant",
      "content":"我已经获取到了get_food的结果，现在让我调用send_popup_to_user",
      "reasoning_content":"假的思考过程",
      "tool_calls":[
        {
          "id":"tool_call_id_2",
          "type":"function",
          "function":{
            "name":"send_popup_to_user",
            "arguments":'{"content":"Hello,hi"}'
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_2",
      "content":"成功向用户发一个弹窗，弹窗的内容是Hello,hi"
    },
    {
      "role":"assistant",
      "content":"先调用了get_food，然后调用了send_popup_to_user",
      "reasoning_content":"调用结束"
    }
  ]
