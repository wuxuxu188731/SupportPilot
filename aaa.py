import json

test_dict = {
  "name":"小明",
  "content":"Hello",
}

print(json.dumps(test_dict,ensure_ascii=False))