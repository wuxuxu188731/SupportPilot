import pytest
import json
from app.agent.runner import parse_tool_arguments

#正常测试
def test_parse_valid_object():
  """验证合法的json object 会被转成dict"""
  args = '{"name":"get_food"}'
  results = parse_tool_arguments(args)
  assert isinstance(results,dict)
  assert results=={"name":"get_food"}

#异常测试,json.JSONDecodeError
def test_parse_malformed_json():
  args = '{'
  with pytest.raises(ValueError):
    result = parse_tool_arguments(args)

#异常测试，输入为None和空字典
def test_parse_none_as_empty_object():
  args = None
  args_1 = {}
  result = parse_tool_arguments(args)
  result_1 = parse_tool_arguments(args_1)
  assert isinstance(result,dict)
  assert isinstance(result_1,dict)

#异常测试，输入为列表list
def test_reject_json_array():
  args = '[1,2,3]'
  with pytest.raises(ValueError):
    result = parse_tool_arguments(args)
  