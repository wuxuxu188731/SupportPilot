import pytest
import json

def test_parse_valid_object():
  """验证合法的json object 会被转成dict"""
  args = '{"name":"get_food"}'
  results = json.loads(args)
  assert isinstance(results,dict)
  assert results=={"name":"get_food"}

def test_parse_malformed_json():
  args = '{'
  with pytest.raises(json.JSONDecodeError):
    result = json.loads(args)

def test_parse_none_as_empty_object():
  args = None
  args_1 = {}
  with pytest.raises(TypeError):
    result = json.loads(args)
    result_1 = json.loads(args_1)

def test_reject_json_array():
  args = '[1,2,3]'
  result = json.loads(args)
  print(result)
  assert isinstance(result,list)
  if not isinstance(result,dict):
    with pytest.raises(ValueError):
      raise(ValueError("工具参数必须是JSON object"))