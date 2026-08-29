# SupportPilot 退款/补偿审批与可靠执行设计

## 1. 文档状态

- 日期：2026-08-28
- 最近更新：2026-08-29
- 状态：已确认，待实施
- 目标阶段：阶段 4——人工审批与可靠执行
- 核心决策：普通聊天继续使用现有 Agent Runtime；LangGraph 只是提案工具背后的确定性业务工作流，不是第二个 Agent Runtime，不调用 LLM；所有执行型动作必须先形成提案并经过人工审批

## 2. 背景

SupportPilot 当前已经具备认证、多租户、`admin` / `agent` 最小 RBAC、会话持久化、订单与物流查询、工单写入、企业知识检索和受控 Tool Gateway。现有 Agent Runtime 适合同步、短时的模型—工具循环，但不能可靠表达以下流程：

1. Agent 生成高风险动作提案后暂停。
2. 等待管理员在另一个 HTTP 请求中审批。
3. 服务重启后从原暂停点继续。
4. 节点重放、重复审批或请求重试时不重复产生退款/补偿。
5. 审批、恢复与执行过程能够独立审计。

因此，本阶段只在 `propose_refund` 和 `propose_compensation` 工具背后引入 LangGraph。现有 Agent Runtime 仍负责模型调用和工具选择；LangGraph 不调用 LLM、不维护聊天消息循环，也不替代现有 `run_one_turn()`。它只负责确定性的运行位置、条件路由、`interrupt` 和 checkpoint。项目自己的业务数据库继续负责提案、审批决定、执行结果、幂等约束和审计事实。

## 3. 已确认的产品决策

1. 退款和补偿在本阶段全部需要人工审批，不设置自动放行额度。
2. 只有当前企业的 `admin` 可以作出审批决定。
3. 为兼容只有一个管理员的演示企业，允许管理员审批自己发起的提案。
4. 审批支持“批准”“修改后批准”“拒绝”。修改范围仅限金额、原因和类型专属参数；退款的 `refund_scope` 属于可修改的类型专属参数。不能更换企业、订单、币种或动作类型。
5. 本阶段只实现模拟退款与模拟优惠券补偿，不连接真实支付、优惠券、CRM 或物流平台。
6. “暂停恢复”指等待审批期间暂停、服务重启后恢复，以及已批准可重试失败的显式恢复；不提供任意节点的操作员暂停功能。
7. 普通聊天、订单查询、物流查询、工单和知识检索继续使用现有 Runtime，不整体迁移到 LangGraph。
8. 审批发生时原聊天请求已经结束。审批 API 直接恢复 Action Run，不唤醒或恢复原来的 Agent while 循环；后续聊天通过只读状态工具查询结果。

## 4. 目标与非目标

### 4.1 目标

- Agent 可以为当前企业、当前订单创建结构化退款或补偿提案。
- 提案创建不产生退款、优惠券或订单状态副作用。
- 管理员可以查看待审批内容，并批准、修改后批准或拒绝。
- 未形成持久化批准决定时，执行器在任何调用路径下都拒绝执行。
- LangGraph 可以在等待审批时持久化暂停，并在进程重启后使用同一个 `thread_id` 恢复。
- 相同审批版本被重复恢复或重放时，只产生一份稳定业务结果。
- 退款累计金额不能超过订单总金额。
- 同一订单、同一补偿原因只能成功补偿一次，累计补偿不能超过订单金额的 50%。
- 所有读取与写入严格限定 `organization_id`，跨租户资源统一表现为不存在。
- 对提案、审批、恢复、执行和终态变化形成可查询的审计记录。
- 明确区分输入错误、业务拒绝、权限错误、并发冲突、可重试失败和不可恢复失败。

### 4.2 非目标

- 不连接真实支付渠道或优惠券系统。
- 不宣称模拟执行等同于真实到账或真实发券。
- 不自动处理商品行、组合优惠、积分、赠品、发票、分期付款和支付渠道分摊。
- 不自动判断 VIP 等级、不可抗力、用户责任或大促特殊时效。
- 不实现前端审批页面、消息推送、邮件或短信通知。
- 不实现 Worker、消息队列、定时重试、超时自动拒绝或多实例分布式调度。
- 不实现任意时刻暂停、回滚已成功执行的动作或撤销审批。
- 不把 LangGraph checkpoint 当作业务审计、业务查询或授权数据源。
- 不改变现有知识检索的规划、召回、引用和评测语义。

## 5. 现有数据能力与 MVP 限制

当前 `orders` 只有订单总金额、币种、状态和商品摘要，没有商品行、真实实付分摊、支付流水、发票、积分、赠品或会员等级。`shipments` 具有基础物流状态与时间，但不足以自动判断全部补偿排除条款。

据此，本阶段采用以下限制：

- 退款提案由客服明确给出退款金额与原因；服务端只验证金额大于零、币种等于订单币种、金额不超过当前可退余额。
- 补偿提案使用固定原因码，并由客服给出金额；服务端验证金额、重复原因和累计上限，人工审批承担无法自动核实的资格判断。
- 知识库检索结果可以帮助 Agent 解释政策和形成建议，但不能成为执行授权。
- 模拟退款写入 `refund_records`；模拟补偿写入 `compensation_records`，不伪造外部渠道流水。
- 订单全额退完后可以将订单状态更新为 `refunded`；部分退款不改变订单主状态，退款事实以退款记录为准。

## 6. 总体架构

```text
Chat API
   |
   v
现有 ChatService / AgentRunner
   |
   +--> 订单、物流、知识查询工具
   |
   +--> propose_refund / propose_compensation
              |
              v
       ActionWorkflowService
        |       |       |
        |       |       +--> 业务 Store / Audit Log
        |       +----------> LangGraph Workflow Runtime + SQLite Checkpointer
        +------------------> Proposal / Approval / ActionRun
                                  |
                                  | interrupt
                                  v
                             awaiting_approval

Approval API --> ApprovalService --> 持久化决定 --> Command(resume=...)
                                                    |
                                                    v
                                            IdempotentActionExecutor
                                              |                |
                                              v                v
                                       refund_records   compensation_records
```

模块职责：

- 现有 Agent 负责理解用户意图、查询业务事实和调用“创建提案”工具。
- `ActionWorkflowService` 负责在业务数据库中原子创建 Run、提案版本和审批请求，然后启动图。
- LangGraph 是工具内部的确定性业务状态机，只负责等待、恢复和流程路由；它不调用 LLM、不选择工具，也不负责判断调用者权限。
- `ApprovalService` 负责 RBAC、审批状态转换和不可变决定。
- `IdempotentActionExecutor` 只接受已经持久化并批准的提案版本。
- Store 和数据库约束负责租户隔离、状态一致性、金额上限与幂等。

现有 Agent Runtime 与 Action Workflow Runtime 之间不存在长期存活的进程内通信：首次创建时，现有 Agent Runtime 通过普通工具函数调用启动 Action Run，并从工具结果取得 `awaiting_approval`；审批时，另一个 HTTP 请求依据业务数据库中的 Run 和 LangGraph checkpoint 独立恢复流程。原聊天请求和原 while 循环不会保持等待状态。

## 7. 模块与文件边界

建议新增：

```text
app/actions/
  base.py                 # 提案、版本、审批、Run、执行记录及 Protocol
  sqlite_store.py         # 事务、状态转换、幂等认领、业务结果与审计
  service.py              # 提案、审批、查询和恢复应用服务
  executor.py             # 框架无关执行器接口与模拟实现
  factory.py              # Store、执行器和工作流装配

app/workflows/
  action_graph.py         # StateGraph、节点、条件边和状态类型
  runtime.py              # invoke、interrupt 结果归一化、resume
  checkpointer.py         # SQLite checkpointer 生命周期与配置

app/agent/
  invocation_context.py   # AgentInvocationContext，携带可信租户、会话和回合标识

app/tools/
  action_arguments.py     # 两个提案工具和只读状态工具的 Pydantic 参数
  action_definitions.py   # 提案与状态工具描述
  action_gateway.py       # AgentInvocationContext 绑定和错误映射

app/api/
  action_router.py        # 提案、审批和 Run 查询/操作 API

app/schemas/
  action.py               # HTTP 请求和响应模型

migrations/versions/
  0011_action_approval_workflow.py

tests/actions/
tests/workflows/
tests/api/test_action_router.py
tests/integration/test_action_workflow.py
```

允许修改：

- `requirement.txt`：增加 LangGraph 及 SQLite checkpointer 依赖。
- `app/core/config.py`：增加 checkpoint 数据库路径配置。
- `app/tools/composite_gateway.py` 和现有 Gateway：统一接收 `AgentInvocationContext`，现有工具只读取其中的可信租户部分。
- `app/agent/prompts.py`：增加高风险动作规则。
- `app/agent/runner.py`、`app/agent/support_runner.py`：传递调用上下文、增加提案结果归一化和待审批摘要，不改写现有 while 循环。
- `app/application/chat_service.py`、`app/schemas/chat.py`：由可信会话生成调用上下文，并在聊天响应中暴露结构化待审批摘要。
- `main.py`：装配工作流、服务和 Router。
- `README.md` 与运维文档：说明能力、限制和恢复方式。

不应修改：

- 现有知识检索、入库和 Stage C 评测算法。
- 现有订单、物流、工单查询语义。
- 认证 Token 和可信 `TenantContext` 的来源。
- 现有会话消息作为聊天历史的职责。

## 8. 领域模型

### 8.1 动作类型

```text
refund
compensation
```

### 8.2 退款参数

```text
amount_cents             正整数，不能超过执行时可退余额
currency                 必须等于订单币种
reason_code              使用下方固定退款原因枚举
reason_text              客服给出的补充说明
refund_scope             full / partial
```

退款原因枚举固定为：

```text
customer_cancellation    客户在允许阶段主动取消订单
changed_mind_return      符合无理由退货条件
quality_issue            商品存在功能、性能或制造质量问题
damaged_item             商品到货时破损
wrong_item               实际收到的商品与订单不符
missing_item             订单内部分或全部商品缺失
not_as_described         商品与页面描述存在实质差异
out_of_stock             商家缺货，无法履约
delivery_delay           配送超时，客户选择退款
lost_in_transit          物流确认丢件
other                    其他未覆盖原因，必须填写详细说明
```

不使用含义过宽的 `customer_request`：退款本身通常已经由客户提出，该值不能提供有效的业务分类。`reason_code` 只用于分类、审计和统计，不直接证明退款资格；`reason_text` 始终保留，`other` 必须提供非空且有意义的详细说明。枚举值一旦进入历史记录不得改名或改变含义，后续只能新增或停用。

`refund_scope` 是可由“修改后批准”调整的退款专属参数：

- `full` 表示审批时意图退还全部可退余额，审批版本的 `amount_cents` 必须等于审批时可退余额。
- `partial` 表示审批一个明确金额，`amount_cents` 必须大于 0 且不超过审批时可退余额；本阶段不做商品行级自动计算。
- 管理员可以将 `full` 改为 `partial`，或将 `partial` 改为 `full`，但修改后的 `refund_scope` 与金额必须一起通过完整校验。
- 执行前重新计算可退余额，不因余额变化自动修改已批准金额。批准金额超过最新余额时执行失败；执行成功后是否把订单标记为 `refunded`，以剩余可退余额是否为零判断，而不只看 `refund_scope`。

### 8.3 补偿参数

```text
amount_cents             正整数
currency                 必须等于订单币种
reason_code              delayed_shipment / transit_delay / customer_dispute / other
reason_text              客服给出的补充说明
coupon_valid_days        本阶段固定为 30，审批不能修改为其他值
```

执行时必须再次验证：同一订单同一 `reason_code` 没有成功补偿记录，且成功补偿累计值加本次金额不超过订单总金额的 50%。金额取整使用整数分，50% 上限使用向下取整。

### 8.4 状态模型

#### Action Run

```text
queued
  -> running
  -> awaiting_approval
  -> running
  -> succeeded | failed | cancelled
```

- 审批拒绝使 Run 进入 `cancelled`，表示流程按人工决定终止，不表示系统错误。
- 校验或执行失败进入 `failed`，并记录稳定错误码及是否可重试。
- 已进入终态的 Run 不允许恢复。

#### Proposal

```text
awaiting_approval
  -> approved -> executing -> succeeded
  -> rejected
  -> failed
  -> cancelled
```

- Proposal 根记录表示一次业务申请。
- 每次修改产生新的 Proposal Version；历史版本不可覆盖。
- “修改后批准”在一个事务内创建新版本并使决定引用新版本。

#### Approval

```text
pending -> approved
pending -> approved_with_changes
pending -> rejected
```

- 每个审批请求最多有一个决定。
- 对相同审批重复提交完全相同的决定时返回首次结果。
- 已决定后提交不同决定返回 `409 APPROVAL_ALREADY_DECIDED`。

#### Tool Execution

```text
claimed -> running -> succeeded
                   -> failed_retryable
                   -> failed_terminal
```

- `succeeded` 和 `failed_terminal` 是终态。
- `failed_retryable` 可以使用原幂等键再次认领，尝试次数递增。
- 同一时刻只能有一个调用者持有执行权。

## 9. 数据模型

所有业务表都必须具有 `organization_id`，跨表引用优先使用包含 `organization_id` 的复合外键。所有时间使用 UTC 文本格式，并由数据库或统一时钟产生。

### 9.1 action_runs

```text
id                       TEXT PK
organization_id          TEXT NOT NULL
conversation_id          TEXT NOT NULL
turn_id                  TEXT NOT NULL
created_by_user_id       TEXT NOT NULL
workflow_type            TEXT NOT NULL CHECK IN ('refund', 'compensation')
status                   TEXT NOT NULL
thread_id                TEXT NOT NULL
proposal_id              TEXT NULL
last_error_code          TEXT NULL
last_error_retryable     INTEGER NOT NULL DEFAULT 0
created_at               TEXT NOT NULL
updated_at               TEXT NOT NULL
completed_at             TEXT NULL
UNIQUE (organization_id, id)
UNIQUE (thread_id)
UNIQUE (organization_id, conversation_id, turn_id, workflow_type)
```

`conversation_id` 和 `turn_id` 由 `ChatService` 已验证的会话及本次服务端聊天回合产生，再通过 `AgentInvocationContext` 绑定到提案工具；它们都不是模型工具参数。`thread_id` 由服务端生成，默认等于 Run ID。API 请求不能指定或覆盖这些字段。

### 9.2 action_proposals

```text
id                       TEXT PK
organization_id          TEXT NOT NULL
run_id                   TEXT NOT NULL
order_id                 TEXT NOT NULL
action_type              TEXT NOT NULL CHECK IN ('refund', 'compensation')
status                   TEXT NOT NULL
current_version_id       TEXT NULL
created_by_user_id       TEXT NOT NULL
created_at               TEXT NOT NULL
updated_at               TEXT NOT NULL
UNIQUE (organization_id, id)
UNIQUE (organization_id, run_id)
```

同一订单和动作类型最多存在一个非终态提案。该约束用于抑制聊天重试造成的重复待审批项，但不能替代执行时的金额与重复补偿校验。

SQLite 通过部分唯一索引落实该约束，终态提案不阻止用户以后创建新的合法申请：

```text
UNIQUE (organization_id, order_id, action_type)
WHERE status IN ('awaiting_approval', 'approved', 'executing')
```

### 9.3 action_proposal_versions

```text
id                       TEXT PK
organization_id          TEXT NOT NULL
proposal_id              TEXT NOT NULL
version_no               INTEGER NOT NULL
amount_cents             INTEGER NOT NULL CHECK > 0
currency                 TEXT NOT NULL
reason_code              TEXT NOT NULL
reason_text              TEXT NOT NULL
parameters_json          TEXT NOT NULL
created_by_user_id       TEXT NOT NULL
created_at               TEXT NOT NULL
UNIQUE (organization_id, proposal_id, version_no)
UNIQUE (organization_id, id)
```

`parameters_json` 只保存经过 Pydantic 严格校验的 JSON，不保存模型 reasoning、政策正文或未经筛选的用户输入。

### 9.4 approvals

```text
id                       TEXT PK
organization_id          TEXT NOT NULL
proposal_id              TEXT NOT NULL
requested_version_id     TEXT NOT NULL
requested_by_user_id     TEXT NOT NULL
status                   TEXT NOT NULL
created_at               TEXT NOT NULL
decided_at               TEXT NULL
UNIQUE (organization_id, id)
UNIQUE (organization_id, proposal_id)
```

### 9.5 approval_decisions

```text
id                       TEXT PK
organization_id          TEXT NOT NULL
approval_id              TEXT NOT NULL
decision                 TEXT NOT NULL CHECK IN ('approved', 'approved_with_changes', 'rejected')
decided_version_id       TEXT NOT NULL
decided_by_user_id       TEXT NOT NULL
comment                  TEXT NULL
created_at               TEXT NOT NULL
UNIQUE (organization_id, id)
UNIQUE (organization_id, approval_id)
```

拒绝决定的 `decided_version_id` 指向被拒绝的请求版本。所有决定不可更新或删除。

### 9.6 tool_executions

```text
id                       TEXT PK
organization_id          TEXT NOT NULL
proposal_id              TEXT NOT NULL
proposal_version_id      TEXT NOT NULL
action_type              TEXT NOT NULL
idempotency_key          TEXT NOT NULL
status                   TEXT NOT NULL
attempt_count            INTEGER NOT NULL
result_json              TEXT NULL
error_code               TEXT NULL
error_retryable          INTEGER NOT NULL DEFAULT 0
claimed_at               TEXT NOT NULL
updated_at               TEXT NOT NULL
completed_at             TEXT NULL
UNIQUE (organization_id, id)
UNIQUE (organization_id, idempotency_key)
UNIQUE (organization_id, proposal_version_id)
```

幂等键格式由服务端生成并版本化，例如：

```text
action-execution:v1:{organization_id}:{proposal_id}:{proposal_version_id}:{action_type}
```

调用者不能提供幂等键。数据库唯一约束是最终防线。

### 9.7 refund_records

```text
id                       TEXT PK
organization_id          TEXT NOT NULL
order_id                 TEXT NOT NULL
proposal_id              TEXT NOT NULL
proposal_version_id      TEXT NOT NULL
tool_execution_id        TEXT NOT NULL
amount_cents             INTEGER NOT NULL
currency                 TEXT NOT NULL
reason_code              TEXT NOT NULL
status                   TEXT NOT NULL CHECK IN ('simulated_succeeded')
created_at               TEXT NOT NULL
UNIQUE (organization_id, tool_execution_id)
UNIQUE (organization_id, proposal_version_id)
```

### 9.8 compensation_records

```text
id                       TEXT PK
organization_id          TEXT NOT NULL
order_id                 TEXT NOT NULL
proposal_id              TEXT NOT NULL
proposal_version_id      TEXT NOT NULL
tool_execution_id        TEXT NOT NULL
amount_cents             INTEGER NOT NULL
currency                 TEXT NOT NULL
reason_code              TEXT NOT NULL
coupon_valid_days        INTEGER NOT NULL CHECK = 30
status                   TEXT NOT NULL CHECK IN ('simulated_succeeded')
created_at               TEXT NOT NULL
UNIQUE (organization_id, tool_execution_id)
UNIQUE (organization_id, proposal_version_id)
UNIQUE (organization_id, order_id, reason_code)
```

### 9.9 audit_logs

```text
id                       TEXT PK
organization_id          TEXT NOT NULL
run_id                   TEXT NOT NULL
proposal_id              TEXT NULL
actor_type               TEXT NOT NULL CHECK IN ('user', 'system')
actor_user_id             TEXT NULL
event_type               TEXT NOT NULL
resource_type             TEXT NOT NULL
resource_id               TEXT NOT NULL
details_json              TEXT NOT NULL
created_at               TEXT NOT NULL
UNIQUE (organization_id, id)
```

审计至少记录：Run 创建、提案版本创建、进入等待、审批决定、恢复请求、执行认领、执行成功/失败和终态。审计内容不得包含访问令牌、模型 reasoning、知识正文或异常堆栈。

## 10. 事务与业务不变量

### 10.1 创建工作流

在一个 `BEGIN IMMEDIATE` 事务中：

1. 验证订单属于当前企业。
2. 验证参数与现有成功记录。
3. 检查同订单同动作类型是否已有非终态提案。
4. 创建 `action_runs`、`action_proposals`、版本 1 和 `approvals`。
5. 写审计事件。
6. 提交后才首次调用 LangGraph。

业务事务成功但首次图调用失败时，Run 保持 `queued` 或 `failed`，可以通过恢复 API 使用相同 Run 和 `thread_id` 重试，不能重新创建提案。

### 10.2 作出审批决定

在一个事务中：

1. 按 `organization_id + approval_id` 锁定并读取审批。
2. 验证调用者是当前企业 `admin`。
3. 验证审批仍为 `pending`。
4. 若为修改后批准，严格校验修改字段并创建下一版本。
5. 创建唯一 `approval_decisions`，更新审批和提案状态。
6. 写审计事件并提交。
7. 提交后调用 `Command(resume=...)`。

如果第 7 步失败，批准决定仍然有效；后续恢复必须读取该持久化决定继续，不能要求管理员再次审批。

### 10.3 执行事务

模拟执行在一个数据库事务中完成：

1. 读取 Run、Proposal、Approval Decision 和被批准版本。
2. 验证它们属于同一企业且引用链完整。
3. 验证决定为批准或修改后批准。
4. 按幂等键认领或读取已有 `tool_executions`。
5. 若已有成功结果，原样返回该结果。
6. 再次计算退款余额或补偿上限，防止审批等待期间业务事实变化。
7. 写入一条模拟业务结果。
8. 更新执行、提案和 Run 状态并写审计。

对本地模拟执行，业务结果和 `tool_executions=succeeded` 必须同事务提交。未来接入真实外部系统时，必须把相同幂等键传给提供方，并新增查询与对账流程；不能用本地事务声称跨系统 exactly-once。

## 11. LangGraph 设计

### 11.1 Graph State

Graph State 只保存可序列化的稳定标识和流程输出：

```text
run_id                   Run 标识
proposal_id              提案标识
approval_id              审批标识
organization_id          用于一致性校验，不作为授权来源
decision_id              恢复后读取的决定标识
execution_id             执行记录标识
outcome                  succeeded / rejected / failed
error_code               稳定错误码
error_retryable          是否允许显式恢复
```

不把 Store、数据库连接、`TenantContext`、模型 Client、完整聊天历史或敏感对象放入 State。每个节点通过依赖注入的服务按 Run ID 重新读取权威业务数据。

### 11.2 节点与边

```text
START
  -> load_run
  -> mark_awaiting_approval
  -> await_decision [interrupt]
  -> load_persisted_decision
       | rejected
       v
     finalize_rejected -> END

       | approved / approved_with_changes
       v
     claim_execution
       | existing success -> finalize_succeeded -> END
       | acquired
       v
     execute_action
       | success -> finalize_succeeded -> END
       | retryable failure -> finalize_failed -> END
       | terminal failure -> finalize_failed -> END
```

### 11.3 interrupt 规则

- `await_decision` 节点在调用 `interrupt()` 前不创建新业务记录、不追加审计、不产生任何非幂等副作用。
- 进入等待状态和写入审计由前一个独立节点幂等完成。
- interrupt payload 仅包含展示所需的 JSON：Run ID、Approval ID、动作类型、订单号掩码、金额、币种、原因和允许的决定。
- 不在 `try/except` 中捕获 LangGraph 的 interrupt 控制流异常。
- 不依赖 interrupt 在源代码中的序号；该节点保持单一 interrupt。
- 恢复值只携带 `decision_id`。节点必须从业务数据库加载并校验决定，不能信任恢复值中的金额、角色或批准标记。

### 11.4 Checkpointer

- 本地 MVP 使用官方 `langgraph-checkpoint-sqlite` 持久化器。
- checkpoint 使用独立 SQLite 文件，通过 `LANGGRAPH_CHECKPOINT_DB_PATH` 配置，避免 Alembic 管理的业务表与 LangGraph 内部 schema 混杂。
- `thread_id` 从 `action_runs.thread_id` 读取，不接受 HTTP Body、模型参数或查询参数覆盖。
- checkpointer schema 初始化作为应用启动步骤，测试使用临时数据库。
- 进程重启后重新装配相同图定义和 checkpointer，再用同一 `thread_id` 恢复。
- 图拓扑和 State schema 的不兼容修改必须伴随 `workflow_version` 迁移策略；本阶段固定 `refund-compensation-v1`。
- checkpoint 不是租户授权边界。所有可见状态仍由业务 Store 使用 `organization_id` 查询。

### 11.5 恢复语义

存在三种恢复入口：

1. 审批决定提交后，由审批服务自动恢复等待中的 Run。
2. 自动恢复失败后，管理员调用显式 Run 恢复 API。
3. 可重试执行失败排除故障后，管理员调用相同恢复 API；执行继续使用原幂等键。

恢复 API 不允许：

- 恢复未批准的执行分支。
- 恢复已拒绝、成功、取消或不可恢复失败的 Run。
- 为另一个企业提供 `thread_id`。
- 修改已经批准的提案版本。

### 11.6 与现有 Agent Runtime 的关系

LangGraph 放在 Action Tool Gateway 后面、业务执行器前面，不放在 `ChatService` 与现有 `AgentRunner` 之间：

```text
ChatService
  -> 现有 AgentRunner / run_one_turn()
      -> propose_refund / propose_compensation
          -> ActionToolGateway
              -> ActionWorkflowService
                  -> LangGraph 确定性工作流
                      -> IdempotentActionExecutor
```

首次创建提案时，`run_one_turn()` 像调用普通工具一样同步调用提案工具。LangGraph 运行到 `interrupt()` 后，Action Tool Gateway 把 `awaiting_approval` 作为普通工具结果返回，现有 while 循环继续生成“等待审批”的本轮最终答复，然后结束。

管理员稍后审批时，原聊天 HTTP 请求和 while 循环已经结束。Approval API 持久化决定后直接恢复 LangGraph；它不恢复、唤醒或重新创建原 Agent Runtime。审批 API 返回结构化执行状态，之后的新聊天可以通过 `get_action_status` 只读工具查询结果并生成自然语言解释。

## 12. Agent 与工具设计

### 12.1 AgentInvocationContext

现有 `TenantContext` 只表达认证用户的企业身份，不能加入 `conversation_id`，因为它还用于企业管理和知识管理等非聊天场景。本阶段新增请求级调用上下文：

```python
@dataclass(frozen=True)
class AgentInvocationContext:
    tenant: TenantContext  # 当前认证用户及企业身份，由服务端认证链产生
    conversation_id: str   # 当前会话标识，已经由 ChatService 验证归属
    turn_id: str           # 当前聊天回合标识，由服务端生成，用于追踪及抑制同回合重复提案
```

可信传递链固定为：

```text
ChatService 验证 conversation_id 并生成 turn_id
  -> CustomerSupportAgentRunner
      -> CompositeToolGateway.bind(AgentInvocationContext)
          -> ActionToolGateway 的请求级闭包
              -> ActionWorkflowService
                  -> action_runs.conversation_id / turn_id
```

现有客服和知识 Gateway 改为接收 `AgentInvocationContext`，但只把 `context.tenant` 传给原应用服务。Action Tool Gateway 同时读取可信租户、`conversation_id` 和 `turn_id`。`organization_id`、`user_id`、`role`、`conversation_id`、`turn_id` 和 `thread_id` 都不能出现在模型可见的工具参数 Schema 中。

禁止使用以下替代方案：

- 不把 `conversation_id` 添加到 `propose_refund` 或 `propose_compensation` 的模型参数。
- 不把会话 ID 写入单例 Gateway 的可变属性，避免并发串线。
- 不使用全局变量或 `ContextVar` 隐式传递。
- 不扩充 `TenantContext` 的职责。

该改动只扩展 `ChatService -> CustomerSupportAgentRunner -> Gateway.bind()` 的参数传递和请求级闭包，不改变 `run_one_turn()` 的模型—工具 while 循环。

### 12.2 propose_refund

参数：

```text
order_no
amount_cents
currency
refund_scope
reason_code
reason_text
```

调用规则：

- 用户必须明确提出退款诉求。
- Agent 必须先调用 `get_order` 获取订单事实。
- 涉及政策解释时先调用 `search_knowledge`，但政策证据不能代替审批。
- 工具成功只允许表述“已创建退款提案并等待审批”，禁止表述“退款成功”。

### 12.3 propose_compensation

参数：

```text
order_no
amount_cents
currency
reason_code
reason_text
```

调用规则与退款一致。若资格需要会员等级、不可抗力或用户责任等当前系统不存在的数据，Agent 必须在原因中明确标为“待人工核实”，不能编造结论。

### 12.4 get_action_status

`get_action_status` 是只读工具，接收用户可见的 `run_id`，并使用绑定的 `context.tenant` 查询当前企业中的 Action Run、审批和执行结果。跨租户 Run 与不存在 Run 一样返回 `RUN_NOT_FOUND`。该工具不能审批、恢复或执行动作。

### 12.5 工具返回

```json
{
  "ok": true,
  "data": {
    "run_id": "...",
    "proposal_id": "...",
    "approval_id": "...",
    "action_type": "refund",
    "status": "awaiting_approval",
    "amount_cents": 1000,
    "currency": "CNY"
  }
}
```

聊天响应在现有 `LLMResponse` 上增加结构化 `pending_approvals`，调用方不应从自然语言或通用事件中解析 Approval ID。

### 12.6 工具暴露边界

- 不向模型注册 `approve_action`、`resume_run`、`execute_refund` 或 `issue_compensation`。
- 执行器不进入 `CompositeToolGateway`。
- 审批和恢复只能通过认证 HTTP API 进入应用服务。
- `organization_id`、`user_id`、`role`、`conversation_id`、`turn_id`、Proposal ID、Approval ID、`thread_id` 和幂等键均不能由模型构造为执行授权。`get_action_status` 的 `run_id` 只是租户限定查询键，不构成执行授权。

## 13. HTTP API

所有 API 继续使用 Authorization Bearer Token 和 `X-Organization-ID`。跨租户或不存在资源统一返回 404。

### 13.1 查询审批

```text
GET /approvals/?status=pending
GET /approvals/{approval_id}/
```

- 当前企业的 `agent` 和 `admin` 均可读取。
- 列表分页，默认按创建时间倒序。
- 响应包含当前版本、历史版本摘要、Run 状态和决定摘要，不包含模型 reasoning。

### 13.2 作出决定

```text
POST /approvals/{approval_id}/decisions/
```

请求：

```json
{
  "decision": "approved | approved_with_changes | rejected",
  "changes": {
    "amount_cents": 800,
    "refund_scope": "partial",
    "reason_code": "quality_issue",
    "reason_text": "仅对存在质量问题的商品进行部分退款"
  },
  "comment": "审批备注"
}
```

约束：

- 仅 `admin` 可调用。
- `approved` 和 `rejected` 时 `changes` 必须为空。
- `approved_with_changes` 必须至少改变一个允许字段。
- 退款允许修改 `amount_cents`、`reason_code`、`reason_text` 和 `refund_scope`；补偿允许修改 `amount_cents`、`reason_code` 和 `reason_text`。
- `organization_id`、订单、`action_type`、`currency`、提案人和 `coupon_valid_days` 不允许修改。
- 修改退款 `refund_scope` 时必须连同最终金额通过 8.2 节的完整一致性校验，禁止仅改变标签以绕过金额规则。
- 修改后的参数仍需通过完整领域校验。
- 相同审批的相同决定重复提交返回原决定与当前 Run 状态。
- 不同决定或不同修改内容重复提交返回 409。
- HTTP 成功提交决定但图恢复失败时，返回已持久化的决定、`resume_required=true` 和稳定错误码，不能回滚或伪装成未审批。首次调用使用 202 表示决定已经接受但执行尚未完成；同一请求重试仍返回同一决定。

### 13.3 查询与恢复 Run

```text
GET  /action-runs/{run_id}/
POST /action-runs/{run_id}/resume/
```

- 当前企业成员可查询。
- 只有 `admin` 可以显式恢复。
- 恢复请求本身幂等；Run 已成功时返回现有成功结果。
- 非法状态返回 409，并给出稳定错误码。

### 13.4 建议的状态码

```text
200  查询、重复同决定、恢复后已有稳定结果
201  首次创建审批决定
202  审批决定已保存，但工作流需要稍后显式恢复
403  当前企业成员存在但角色无权审批或恢复
404  资源不存在或不属于当前企业
409  状态冲突、审批已被其他决定占用、存在非终态重复提案
422  请求结构或允许修改字段不合法
503  checkpoint 或工作流基础设施在写入业务决定前不可用
```

## 14. 权限与安全

- 认证依旧由现有依赖生成可信 `TenantContext`。
- Store 的所有方法显式接收 `organization_id`；禁止提供不带租户条件的按 ID 查询方法。
- 审批时重新读取当前 Membership，不能使用 checkpoint 中保存的历史角色。
- 允许管理员自审是已确认的 MVP 取舍，审计必须明确记录提案人与审批人是否相同。
- 审批列表和详情中的订单号、原因及金额属于企业业务数据，不能跨租户返回。
- 知识文档、用户消息和模型输出均是不可信输入，不能改变审批策略或工具权限。
- 金额始终使用整数分，禁止浮点金额。
- JSON Schema 使用 `extra='forbid'`，枚举拒绝未知值，字符串统一去除首尾空白并限制长度。
- 日志和错误响应只返回稳定错误码，不泄露 SQL、文件路径、堆栈、Token 或提供方原始响应。
- 没有业务数据库中的有效 Approval Decision，即使 checkpoint 被错误恢复，也必须在执行节点失败关闭。

## 15. 并发与幂等

### 15.1 并发审批

两个管理员同时审批时，`approval_decisions` 的唯一约束保证只有一个决定落库。失败方重新读取：

- 内容相同则返回首次决定。
- 内容不同则返回 409。

### 15.2 重复恢复

同一个 Run 被并发恢复时：

- LangGraph checkpoint 提供运行位置恢复。
- `tool_executions` 唯一幂等键提供业务副作用保护。
- 已成功执行的节点直接返回持久化结果，不再次写业务记录。

### 15.3 金额竞争

退款余额与补偿累计上限必须在写业务结果的同一个 `BEGIN IMMEDIATE` 事务中重新计算。提案时和审批时的校验用于尽早反馈，不能替代执行时校验。

### 15.4 进程中断窗口

- checkpoint 前中断：Run 和提案已经存在，可通过 Run ID 首次启动或恢复。
- interrupt 后中断：checkpoint 保存等待位置，审批后恢复。
- 决定提交后、恢复前中断：决定保持有效，显式恢复继续。
- 执行事务中断：SQLite 回滚，原幂等键可以重试。
- 执行事务提交后、checkpoint 前中断：重放读取成功 `tool_executions` 和业务结果，不重复执行。

## 16. 错误分类

### 16.1 输入错误

例如空订单号、非正金额、未知原因码、币种格式错误。请求返回 422，不创建提案。

### 16.2 业务拒绝

例如订单不存在、退款余额不足、重复补偿、超过补偿累计上限。工具返回稳定业务错误，或者执行时将 Run 标记为不可重试失败。

### 16.3 权限错误

非管理员审批或恢复返回 403；跨租户资源返回 404。

### 16.4 并发冲突

例如审批已被另一决定占用、同订单已有非终态提案。返回 409，不静默覆盖。

### 16.5 可重试基础设施失败

例如 checkpoint 数据库暂时锁定、模拟执行器注入的临时故障。记录 `error_retryable=true`，保留同一 Run、版本和幂等键，允许显式恢复。

### 16.6 不可恢复失败

例如引用链损坏、批准版本不存在、币种与订单不一致、状态机非法跳转。记录稳定错误码并结束 Run，必须人工排查数据一致性，不能自动重建审批事实。

建议稳定错误码至少包括：

```text
ACTION_ORDER_NOT_FOUND
ACTION_INVALID_AMOUNT
ACTION_CURRENCY_MISMATCH
ACTION_ACTIVE_PROPOSAL_EXISTS
ACTION_REFUND_BALANCE_EXCEEDED
ACTION_COMPENSATION_DUPLICATE
ACTION_COMPENSATION_CAP_EXCEEDED
APPROVAL_NOT_FOUND
APPROVAL_ADMIN_REQUIRED
APPROVAL_ALREADY_DECIDED
APPROVAL_INVALID_CHANGES
RUN_NOT_FOUND
RUN_NOT_RESUMABLE
RUN_STATE_CONFLICT
EXECUTION_NOT_APPROVED
EXECUTION_RETRYABLE_FAILURE
EXECUTION_DATA_INTEGRITY_ERROR
CHECKPOINT_UNAVAILABLE
```

## 17. 配置与依赖

新增配置：

```text
LANGGRAPH_CHECKPOINT_DB_PATH
ACTION_WORKFLOW_VERSION=refund-compensation-v1
```

依赖策略：

- 使用 `langgraph>=1.1,<2.0`，以当前 `interrupt` / `Command(resume=...)` 语义为基线。
- 增加与所选 LangGraph 版本兼容的 `langgraph-checkpoint-sqlite`。
- 实施时先在隔离环境解析兼容版本并运行最小持久化测试，再把经过验证的范围写入 `requirement.txt`。
- 单元测试可以使用内存 saver；所有暂停恢复验收必须使用真实临时 SQLite checkpointer 并重新创建运行时实例。

## 18. 测试策略

开发遵循 TDD。根据仓库规范，每个新增测试方法前必须有中文注释；每个新增或修改的实体属性必须有中文注释。

### 18.1 Store 与迁移测试

- 空数据库升级到最新版本。
- 从 0010 升级到新版本。
- 复合外键与跨租户引用失败。
- 提案版本只增不改。
- 同审批只能有一个决定。
- 同幂等键只能有一个执行。
- 同订单同补偿原因只能成功一次。
- 迁移 upgrade/downgrade 对称；存在不兼容数据时 downgrade 明确失败。

### 18.2 应用服务测试

- 合法退款和补偿创建待审批提案。
- 金额、币种、固定原因枚举和订单归属校验。
- 同订单非终态提案冲突。
- `agent` 无法审批，`admin` 可以审批和自审。
- 批准、修改后批准、拒绝及重复提交语义。
- 退款允许 `full` / `partial` 双向修改，并强制校验修改后的金额一致性。
- 修改禁止更换订单、币种、动作类型或补偿券有效期。
- 决定提交后恢复失败不会丢失决定。

### 18.3 执行器测试

- 没有批准决定时永远拒绝。
- 重复执行返回同一结果 ID。
- 全额退款更新订单状态，部分退款不更新主状态。
- 多次部分退款累计不能超过订单金额。
- 同原因补偿不能重复。
- 补偿累计不能超过订单金额 50%。
- 可重试失败沿用原幂等键和递增 attempt。

### 18.4 LangGraph 测试

- 首次运行在审批节点 interrupt。
- 拒绝后进入 cancelled 且没有业务结果。
- 批准后恢复并成功执行。
- 修改后批准执行新版本参数。
- 关闭并重新创建 checkpointer/graph 后可以恢复。
- interrupt 节点重放不重复写等待审计。
- 执行成功后、checkpoint 前模拟崩溃，再恢复不重复执行。
- 恢复值伪造批准标记或金额时被忽略或拒绝。

### 18.5 API 与安全测试

- 列表、详情、决定和恢复使用可信租户上下文。
- 跨租户访问统一 404。
- 非管理员审批和恢复返回 403。
- Pydantic 拒绝未知字段。
- 并发不同审批决定只有一个成功。
- `resume_required=true` 能准确表达“决定已保存但未恢复”。

### 18.6 Agent 集成测试

- 未明确要求退款/补偿时不创建提案。
- 创建前先查询订单。
- `ChatService` 生成的 `conversation_id` 和 `turn_id` 通过 `AgentInvocationContext` 到达提案服务。
- 模型工具参数 Schema 不包含租户、会话、回合或 `thread_id`。
- Agent 只能声称“等待审批”，不能声称退款或补偿成功。
- 工具结果结构化进入 `pending_approvals`。
- 审批完成后的新聊天可通过 `get_action_status` 查询结果，原 Agent while 循环不会被恢复。
- 攻击提示无法直接调用审批、恢复或执行器。
- 现有订单、物流、工单、知识检索黄金路径保持通过。

## 19. 验收场景

### 场景 A：退款批准

1. 客服请求为订单退款 100 元。
2. Agent 查询订单并创建提案。
3. 返回 `awaiting_approval` 和 Approval ID，没有退款记录。
4. 管理员批准。
5. Run 恢复并只产生一条 100 元模拟退款记录。

### 场景 B：修改后批准

1. 原退款提案为全额退款 100 元。
2. 管理员将 `refund_scope` 改为 `partial`、金额改为 60 元并批准。
3. 原版本仍可审计，新版本保存部分退款 60 元。
4. 执行结果严格使用新版本，订单是否进入 `refunded` 由执行后可退余额决定。

### 场景 C：拒绝

1. 管理员填写原因并拒绝。
2. Run 进入 `cancelled`，Proposal 进入 `rejected`。
3. 不创建 `tool_executions`、退款或补偿记录。
4. 查询接口可以解释拒绝原因。

### 场景 D：重复请求与节点重放

1. 同一批准 Run 被重复恢复两次。
2. 两次响应返回相同 Execution ID 和业务结果 ID。
3. 数据库中只有一条模拟业务结果。

### 场景 E：重启恢复

1. Run 在审批节点暂停。
2. 关闭应用并重新创建所有服务与图实例。
3. 管理员审批后使用原 Run 的 `thread_id` 恢复。
4. 流程正常进入终态。

### 场景 F：租户攻击

1. 企业 B 使用企业 A 的 Approval ID 或 Run ID。
2. 所有读取、审批和恢复接口返回 404。
3. 企业 A 的审批、checkpoint 和执行记录不发生变化。

## 20. 实施子任务与依赖

### Task 1：领域契约与迁移

- 新增固定退款/补偿原因枚举、实体、异常和 Store Protocol。
- 新增 0011 迁移、约束与迁移测试。
- 独立验收：业务事实可持久化，跨租户和唯一约束生效。

### Task 2：SQLite Store 与事务不变量

- 实现提案版本、审批决定、Run 状态、幂等认领、结果和审计。
- 覆盖并发决定、金额竞争和稳定结果读取。
- 依赖 Task 1。

### Task 3：提案与审批应用服务

- 实现创建、列表、详情、决定和状态查询。
- 实现 RBAC、修改后批准及错误映射。
- 依赖 Task 2。

### Task 4：幂等模拟执行器

- 实现退款和补偿模拟适配器。
- 实现执行前授权链验证、上限重验和幂等结果。
- 依赖 Task 2，可与 Task 3 后半部分并行。

### Task 5：LangGraph Runtime

- 引入依赖、checkpointer、StateGraph、interrupt 与 resume。
- 实现故障和重启恢复测试。
- 依赖 Task 3、Task 4。

### Task 6：审批与 Run API

- 新增 Schema、Router、状态码和主应用装配。
- 覆盖租户隔离、角色和重复请求。
- 依赖 Task 3、Task 5。

### Task 7：Agent 提案工具集成

- 新增 `AgentInvocationContext`、两个提案工具、一个只读状态工具、提示词规则和待审批响应。
- 让 `ChatService`、`CustomerSupportAgentRunner` 和 Gateway 绑定链传递可信会话与回合标识。
- 保持现有 `run_one_turn()` while 循环和知识检索语义不变。
- 依赖 Task 3、Task 5，可与 Task 6 并行。

### Task 8：端到端验收与运维文档

- 完成六个验收场景、全量回归和恢复操作说明。
- 更新 README 的已完成/未完成能力。
- 依赖 Task 6、Task 7。

依赖关系：

```text
Task 1 -> Task 2 -> Task 3 -----> Task 5 -> Task 6 --+
                  \-> Task 4 --/          \-> Task 7 --+-> Task 8
```

## 21. 完成定义

以下条件全部满足才视为本阶段完成：

- 未审批动作的执行成功数为 0。
- 非管理员审批成功数为 0。
- 跨租户数据泄漏和状态修改数为 0。
- 相同幂等键无论重复审批、重复恢复或节点重放，都只有一个业务结果。
- 等待审批时重启应用后可以使用原 Run 恢复。
- 决定已保存但恢复失败的状态可查询、可重试且不会要求重复审批。
- 拒绝能够形成可审计终态且无执行副作用。
- 新增实体属性和测试方法符合仓库中文注释规范。
- 相关测试、数据库迁移测试和现有全量测试通过。
- README 明确说明模拟执行边界，不将模拟结果描述为真实退款或发券。

## 22. 后续演进

以下内容在本设计之外，需单独设计：

- PostgreSQL checkpointer、Worker 和多实例租约。
- 真实支付/优惠券适配器、提供方幂等键和对账任务。
- 支付分摊、商品行、积分、赠品、发票和分期退款。
- 基于可信会员与活动数据的自动补偿资格判断。
- 金额阈值自动放行、双人复核、禁止自审等更细审批策略。
- 审批超时、撤回、重新提交、通知和前端工作台。
- 失败补偿事务、人工对账和外部结果不确定状态。
- LangGraph workflow version 的在线迁移和历史 Run 兼容策略。
