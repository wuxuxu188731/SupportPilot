import json

def read_history_chat(path : str = "chat_history.json")->list[dict]:
  try:
    with open(path,mode="r",encoding="utf-8") as f:
      return json.load(f)
  except FileNotFoundError as e:
    print("file of chat history does not exist")
    return []

def save_history_chat(messages:list[dict],path : str = "chat_history.json"):
  with open(path,mode="w",encoding="utf-8") as f:
    json.dump(messages,f,indent=2,ensure_ascii=False,)