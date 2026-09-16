/*
 * API 请求与响应类型（DTO）。
 *
 * 与后端 `app/schemas/*`、`docs/frontend/api-inventory.md` 一一对应：
 * 包含认证、企业、会话与聊天模块所需模型；
 * 知识库/审批模型留待后续阶段按需补充。
 * 所有属性均带中文注释；字段可空时显式标注 null（与后端 JSON 一致）。
 */

// —— 认证模块 /auth ——

/** 注册请求体（后端 RegisterRequest）。 */
export interface RegisterRequest {
  /** 登录用户名：归一化后须匹配小写字母/数字/下划线，长度 3–32 */
  username: string
  /** 登录密码：8–72 个 UTF-8 字节，无复杂度要求 */
  password: string
}

/** 登录请求体（后端 LoginRequest）。 */
export interface LoginRequest {
  /** 登录用户名：服务端会 strip + casefold 归一化，可写大写字母 */
  username: string
  /** 登录密码 */
  password: string
}

/** 用户公开信息（后端 UserResponse）：不含密码等任何敏感字段。 */
export interface UserInfo {
  /** 用户唯一标识（服务端 uuid4 字符串） */
  user_id: string
  /** 归一化后的用户名 */
  username: string
  /** 注册时间：UTC 文本 "%Y-%m-%d %H:%M:%S" */
  created_at: string
}

/** 登录响应（后端 TokenResponse）。 */
export interface TokenResponse {
  /** 访问令牌（JWT），请求时放入 Authorization: Bearer 头 */
  access_token: string
  /** 令牌类型：后端固定返回 "bearer" */
  token_type: string
  /** 令牌有效秒数（取决于后端部署配置） */
  expires_in: number
}

// —— 企业模块 /organizations ——

/** 创建企业请求体（后端 CreateOrganizationRequest）。 */
export interface CreateOrganizationRequest {
  /** 企业名称：去首尾空白后长度 2–100 个字符 */
  name: string
}

/** 创建企业响应 / 企业信息（后端 OrganizationResponse）。 */
export interface OrganizationInfo {
  /** 企业唯一标识，作为 X-Organization-ID 请求头使用 */
  organization_id: string
  /** 企业名称 */
  name: string
}

/** 我加入的企业及我在该企业的角色（后端 OrganizationAccessResponse）。 */
export interface OrganizationAccess {
  /** 企业唯一标识 */
  organization_id: string
  /** 企业名称 */
  name: string
  /** 我在该企业的角色：admin 或 agent（仅用于界面展示，不是安全依据） */
  role: MembershipRole
}

/** 添加成员请求体（后端 AddMemberRequest）。 */
export interface AddMemberRequest {
  /** 目标用户名：必须是已注册用户的精确用户名（服务端 strip+小写后精确查找） */
  username: string
  /** 赋予该成员的角色 */
  role: MembershipRole
}

/** 修改成员角色请求体（后端 UpdateMemberRoleRequest）：成员由路径 user_id 指定。 */
export interface UpdateMemberRoleRequest {
  /** 修改后的角色 */
  role: MembershipRole
}

/** 企业成员（后端 OrganizationMemberResponse）：不含密码哈希等凭据字段。 */
export interface OrganizationMember {
  /** 成员用户标识，同时作为改角色/移除接口的路径参数 */
  user_id: string
  /** 成员用户名 */
  username: string
  /** 该成员在本企业的角色 */
  role: MembershipRole
  /** 加入企业的时间（UTC 文本，库内 CURRENT_TIMESTAMP 格式） */
  created_at: string
}

/** 成员关系（后端 MembershipResponse）：添加成员与修改角色的响应。 */
export interface MembershipResponse {
  /** 成员关系所属企业标识 */
  organization_id: string
  /** 成员用户标识 */
  user_id: string
  /** 该成员在本企业的角色 */
  role: MembershipRole
}

/** 成员角色枚举：admin=管理员（可审批/可管理企业），agent=客服。 */
export type MembershipRole = 'admin' | 'agent'

// —— 通用 ——

/** 后端返回的三种错误形态统一解析后的前端错误对象。 */
export interface ApiErrorShape {
  /** HTTP 状态码（无响应时为 0） */
  status: number
  /** 稳定错误码：知识库/审批模块返回 detail.code；其它模块通常无 */
  code: string | null
  /** 用户可读的错误消息（中文文案优先；后端英文消息原样透出） */
  message: string
  /** 字段级错误：字段名 -> 错误描述（来自 FastAPI 422 detail 数组） */
  fieldErrors: Record<string, string>
  /** 原始错误对象，仅用于调试，不用于界面展示 */
  raw: unknown
}

// —— 会话与聊天模块 /conversations ——

/** 会话列表项（后端 ConversationListItem，GET /conversations/ 响应元素）。 */
export interface ConversationListItem {
  /** 会话唯一标识（服务端生成），路由参数 conversationId 使用该值 */
  conversation_id: string
  /** 会话标题：服务端由首条用户消息推导；空会话显示「新会话」 */
  title: string
  /** 会话创建时间：UTC 文本 "%Y-%m-%d %H:%M:%S"（无时区后缀） */
  created_at: string
  /** 会话最近活动时间：UTC 文本，列表按此倒序 */
  updated_at: string
}

/** 历史消息中的单条安全消息（后端 ConversationHistoryMessage）。 */
export interface ConversationHistoryMessage {
  /** 消息在会话中的原始序号（seq），从 1 开始递增，按升序返回 */
  sequence: number
  /** 消息角色：仅 user（用户问题）与 assistant（Agent 最终回答）两种 */
  role: 'user' | 'assistant'
  /** 消息正文（服务端已过滤内部字段；不含推理/工具内容） */
  content: string
  /** 消息写入时间：UTC 文本 "%Y-%m-%d %H:%M:%S"（无时区后缀） */
  created_at: string
}

/** 单个会话的安全历史响应（后端 ConversationHistoryResponse）。 */
export interface ConversationHistoryResponse {
  /** 会话唯一标识 */
  conversation_id: string
  /** 会话当前附加系统提示词；未设置时为 null */
  system_prompt: string | null
  /** 会话创建时间：UTC 文本 */
  created_at: string
  /** 会话最近活动时间：UTC 文本 */
  updated_at: string
  /** 安全历史消息数组：只含用户问题与 Agent 最终回答，可为空数组 */
  messages: ConversationHistoryMessage[]
}

/** 创建会话请求体（后端 CreateConversationRequest，extra=forbid）。 */
export interface CreateConversationRequest {
  /** 可选的会话附加系统提示词；服务端 strip 后为空等同不设置 */
  system_prompt?: string | null
}

/** 创建会话响应（后端 ConversationCreated）。 */
export interface ConversationCreated {
  /** 新会话唯一标识 */
  conversation_id: string
}

/** 更新会话系统提示词请求体（后端 UpdateSystemPromptRequest，extra=forbid）。 */
export interface UpdateSystemPromptRequest {
  /** 会话附加偏好（不能覆盖服务器安全规则），strip 后必须非空 */
  system_prompt: string
}

/** 更新会话系统提示词响应（后端 SystemPromptUpdated）。 */
export interface SystemPromptUpdated {
  /** 固定 true，表示更新请求已被服务端接受 */
  updated: boolean
}

/** 聊天请求体（后端 ChatRequest，extra=forbid）：只发送用户问题。 */
export interface ChatRequest {
  /** 用户问题：服务端 strip 后非空；空白问题会被 422 拒绝 */
  question: string
}

/** 单轮工具调用事件（后端 AgentEvent）：用于「处理过程」过程展示。 */
export interface AgentEvent {
  /** 事件类型：工具请求/开始/完成/失败 或 引用校验失败 */
  type:
    | 'tool_call.requested'
    | 'tool_call.started'
    | 'tool_call.completed'
    | 'tool_call.failed'
    | 'citation.invalid'
  /** 事件时间：服务器本地时间 ISO（无时区后缀，与 UTC 字段语义不同） */
  timestamp: string
  /** 工具调用标识（内部关联用，不用于用户输入） */
  tool_call_id: string
  /** 工具名（如 search_knowledge / get_order / propose_refund） */
  tool_call_name: string
  /** 工具参数：可能含业务/内部数据，默认不直接渲染原始 JSON */
  tool_call_arguments: Record<string, unknown>
  /** 工具执行结果（completed 时才有）：可能含内部数据，默认不直接渲染 */
  result: unknown | null
  /** 错误信息（failed 时才有）：仅展示服务端安全错误文本 */
  error: string | null
  /** 工具执行耗时（毫秒，浮点），用于过程展示 */
  duration_ms: number | null
}

/** 知识库结构化引用（后端 Citation）：回答中 [C1] 等标记对应这里的条目。 */
export interface Citation {
  /** 引用编号（C1..Cn）：回答正文里的 [C1] 角标与该值对应 */
  citation_id: string
  /** 来源文档标识 */
  document_id: string
  /** 来源文档版本标识 */
  version_id: string
  /** 命中的知识块标识 */
  chunk_id: string
  /** 文档标题 */
  title: string
  /** 命中知识块在文档中的标题路径（如「3.2 退货流程」）；无则为 null */
  heading_path: string | null
  /** 可信片段正文（来自知识库，服务端已校验） */
  content: string
  /**
   * 引用片段在**该版本文正**中的起始字符偏移；null 表示无法精确定位（后端降级口径）。
   *
   * 为什么标可选：旧会话中已产生的引用结构不含这两个字段，读出来就是 undefined；
   * 因此前端一律用 `== null` 判断降级（同时覆盖 null 与 undefined），
   * **禁止**用 `!start_offset`——偏移 0 是合法值（片段恰好在正文开头）。
   */
  start_offset?: number | null
  /**
   * 结束字符偏移（不含），满足 text.slice(start_offset, end_offset) === content；
   * 与 start_offset 同生同灭：两者要么都可信，要么都为 null/缺失。
   */
  end_offset?: number | null
}

/** 检索摘要（后端 RetrievalSummary）：本轮知识检索的紧凑状态信息。 */
export interface RetrievalSummary {
  /** 检索策略标识（如 baseline / adaptive / not_needed） */
  strategy: string
  /** 检索轮数 */
  round_count: number
  /** 证据状态：后端检索摘要原样透出的小写枚举，如 sufficient / insufficient / failed / not_needed */
  evidence_status: string
  /** 检索总耗时（毫秒） */
  latency_ms: number
}

/** 引用跳转载荷：从引用卡片（CitationList）上抛的**结构化**定位信息。
 *
 *  全部字段直接取自 citations 元素；禁止从回答文本或引用正文里解析，
 *  也禁止在跳转链路上重新猜测版本或偏移。 */
export interface CitationTarget {
  /** 来源文档标识 */
  documentId: string
  /** 来源版本标识（偏移相对该版本正文，必须一并携带） */
  versionId: string
  /** 引用片段起始字符偏移；null 表示无法精确定位（降级为只打开文档） */
  startOffset: number | null
  /** 引用片段结束字符偏移（不含）；null 表示无法精确定位 */
  endOffset: number | null
  /** 引用标题路径：偏移不可用时用于回退到所属章节 */
  headingPath: string | null
  /** 文档标题（仅用于面板头部占位；正文响应里的标题才是服务端可信来源） */
  title: string
}

/** 待审批提案摘要（后端 PendingApproval）。
 *  唯一可信来源是聊天响应的该结构化数组，禁止从自然语言解析 Approval ID。 */
export interface PendingApproval {
  /** 动作 Run 标识：可用于后续查询 Run 状态 */
  run_id: string
  /** 提案标识 */
  proposal_id: string
  /** 审批标识：管理员在审批中心处理时使用 */
  approval_id: string
  /** 动作类型：refund（退款）或 compensation（优惠券补偿） */
  action_type: 'refund' | 'compensation'
  /** Run 当前状态：等待审批时为 awaiting_approval */
  status: string
  /** 提案金额（整数分，非小数金额字段） */
  amount_cents: number
  /** 币种（样例数据为 CNY，不硬编码唯一币种） */
  currency: string
  /** 工作流首次启动失败时是否需要管理员显式恢复 */
  resume_required: boolean
  /** 首次启动失败的稳定错误码；正常等待审批时为 null */
  error_code: string | null
}

/** 单轮聊天成功响应（后端 LLMResponse）：Agent 工具多轮执行后的完整结果。 */
export interface LLMResponse {
  /** 最终自然语言回答；可为 null（如模型暂时无法生成完整回复） */
  llm_answer: string | null
  /** 模型推理内容（思考过程）：默认不在用户界面展示，仅保留类型 */
  llm_reasoning_content: string | null
  /** 本轮工具调用事件列表（默认折叠展示，不渲染原始参数/结果） */
  events: AgentEvent[]
  /** 知识库结构化引用列表（回答正文中的 [C1] 标记与之对应） */
  citations: Citation[]
  /** 检索摘要；本轮未触发检索时为 null */
  retrieval_summary: RetrievalSummary | null
  /** 证据不足/引用异常时服务端确定性置 true，界面需给出谨慎提示 */
  answer_incomplete: boolean
  /** 本轮提出的退款/补偿待审批提案（禁止从自然语言解析） */
  pending_approvals: PendingApproval[]
}
