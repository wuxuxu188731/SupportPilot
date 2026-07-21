from app.tools.builtin import (
  get_food,
  send_popup_to_user,
)

TOOL_FUNCTIONS = {
  "get_food":get_food,
  "send_popup_to_user":send_popup_to_user,
}

TOOL_DEFINITIONS = [
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
]