### 16:50～17:20：梳理当前执行流程（30 分钟）

目标：先理解代码，再修改代码。

- [ ] 从 `/chat` 开始，沿着 `run_one_turn()` 追踪一次正常请求。
- [ ] 写出以下流程：用户消息 → 模型 → tool_calls → 参数解析 → 工具执行 → tool 消息 → 模型最终回答。
- [ ] 标出两个循环：外层模型循环与内层工具循环。
- [ ] 写清楚 `continue`、`return`、异常分别会跳到哪里。
- [ ] 标出所有可能产生异常的位置。

- 用户发送问题(string),问题被封装为UserQuestion提交给接口,接口将用户问题拼接到messages,接口调用run_one_turn函数
- run_one_turn:
  - 定义事件列表events，用于记录agent调用工具的全流程，同时events最终也将放入LLMResponse里面返回给接口
  - 定义emit函数，将每个事件event加入events
  - 向模型发起请求，response = client.chat.completions.create ， 将携带用户问题的message发给模型
  - 判断模型回复response里面是否含有工具调用，情况1：模型没有进行工具调用，说明模型已经给出了最终回答，将模型的回复append进入messages里面，进行return；情况2：模型进行了工具调用(模型一次性可能调用一个工具，可能调用多个工具，我们默认模型一次性调用多个工具)，先将模型包含tool_call的回复append进入messages里面，循环遍历模型需要调用的工具，**对于每个工具：提取模型输出内容中需要调用的工具名(function name)，调用参数，本地执行对应的工具函数，得到函数结果，将对应的工具结果发回给模型**，模型得到工具调用的结果后，决定继续调用工具(循环情况2)或生成最终回复(进入情况1,return最终结果)
- 两个循环
  - 外层模型循环：判断模型在回答中是否调用了工具，没有调用工具：说明已经生成了最终回复，退出循环；调用了工具：进入工具循环
  - 内层工具循环：遍历工具，获取每一个工具的结果并将工具信息append进入messages
- 'continue' 'return' 异常分别会跳到哪里
  - continue:当前的run_one_turn函数有两个continue,在遍历工具循环中,1.当模型给出的工具参数无法解析时，进行continue,记录这次事件并将错误信息append进入message，然后continue跳过这个参数错误的工具，进行下一个工具调用； 2.当模型想调用的工具不存在时，进行continue，记录这次事件并将错误信息append进入message，然后continue跳过这个不存在的工具
  - 异常处理：1.工具参数解析失败异常；2.工具不存在/未注册异常；3.工具执行异常
  - return：run_one_turn()函数的return只在模型不调用工具中，模型不调用工具的时候说明模型作出了最终回复，将模型的最终回复append进入messages然后包装成类LLMResponse进行返回
- 所有异常可能产生的位置
  - 1.用户发送消息的时候，网络断开，也就是进入run_one_turn函数的while True里面向模型发送消息的时候response = client.chat.completions.create
  - 2.模型进行回复的时候，网络断开。这里要分两种情况，情况1：模型此时没有在调用工具，已经在生成回复，而此时客户端网络断开，模型的回复没有收到；情况2：模型进行了工具调用，此时网络断开，工具调用的结果没有发送给模型
  - 3.用户发送空问题
  - 4.模型调用工具时，使用了错误的工具名
  - 5.模型调用工具时，给出了错误的工具参数
  - 6.客户端本地执行工具函数时，工具函数内出现其他异常
  


### 17:20～18:10：做 JSON 与异常路径实验（50 分钟）

目标：真正理解异常发生在赋值前后的区别。

- [ ] 用正常对象字符串测试 `json.loads('{"content":"hi"}')`。
- [ ] 用损坏字符串测试 `json.loads('{')`。
- [ ] 用 `None`、空字符串和空字典测试 `json.loads()`。
- [ ] 分别记录异常类型。
- [ ] 验证变量在赋值完成前发生异常时能否在 `except` 中使用。
- [ ] 验证 `continue` 控制的是内层 `for`，不是外层 `while`。

完成标志：能独立解释 `JSONDecodeError`、`TypeError` 和 `ValueError` 的来源。

- 正常测试：输出{'content': 'Hi'},json.loads()是将字符串里面的json数据提出，返回值是一个字典dict
- 损坏测试：报错，报错内容json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes
- None,空字符串,空字典测试。None:TypeError: the JSON object must be str, bytes or bytearray, not NoneType;空字符串:json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0);空字典：TypeError
- 异常类型：1.JSONDecodeError 2.TypeError
- 变量赋值完成前发生异常的时候不能在except里面使用
- 已验证


### 18:20～19:20：学习 pytest，先写失败测试（60 分钟）

目标：修改实现以前，先固定预期行为。

- [ ] 创建 `tests/test_tool_execution.py`。
- [ ] 编写正常 JSON Object 测试。
- [ ] 编写损坏 JSON 测试。
- [ ] 编写空参数测试。
- [ ] 编写 JSON Array 测试。
- [ ] 运行测试并确认当前实现确实失败。
- [ ] 阅读失败信息，记录异常类型与代码行。

建议测试名称：

```text
test_parse_valid_object
test_parse_malformed_json
test_parse_none_as_empty_object
test_reject_json_array
```

完成标志：至少有 4 个测试，并能说明每个测试保护什么行为。
完成


### 19:40～20:40：最小化修复工具执行路径（60 分钟）

目标：一次只修复一个根因，每次修改后运行测试。

- [ ] 初始化参数变量，避免异常分支引用未赋值变量。
- [ ] 将空参数默认值改成 JSON 字符串 `"{}"`。
- [ ] 将 Object 类型判断改成正确方向。
- [ ] 使用 `.get()` 查找工具函数。
- [ ] 修正异常名称格式化。
- [ ] 修正 `perf_counter()` 调用。
- [ ] 修正 failed 事件名称。
- [ ] 使用统一序列化函数生成 tool 消息的字符串结果。
- [ ] 每完成一个小修改就运行相关测试。

完成标志：前一时段的 4 个参数解析测试全部通过。

### 20:50～21:40：补齐工具执行测试（50 分钟）

目标：让成功和失败路径都可以重复验证。

- [ ] 测试未注册工具返回 failed 事件。
- [ ] 测试工具内部抛异常时 Agent 不崩溃。
- [ ] 测试字符串结果能够生成 tool 消息。
- [ ] 测试字典结果被序列化成字符串。
- [ ] 检查事件顺序。
- [ ] 运行完整测试集。