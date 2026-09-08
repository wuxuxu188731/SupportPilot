/*
 * API 请求与响应类型（DTO）。
 *
 * 与后端 `app/schemas/*`、`docs/frontend/api-inventory.md` 一一对应：
 * 本阶段只使用认证与企业相关模型；聊天/知识库/审批模型留待后续阶段按需补充。
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
