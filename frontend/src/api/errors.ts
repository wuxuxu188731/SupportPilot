import type { ApiErrorShape } from './types'

/**
 * 前端统一错误类：后端三种错误形态（字符串 detail / 结构化 detail /
 * FastAPI 422 detail 数组）都会在响应拦截器中转换成该类型再抛出。
 * 页面通过 instanceOf ApiError 判断并消费 status/code/message/fieldErrors。
 */
export class ApiError extends Error implements ApiErrorShape {
  /** HTTP 状态码：请求未发出或网络失败时为 0 */
  readonly status: number
  /** 稳定错误码（知识库/审批等模块才有），其它为 null */
  readonly code: string | null
  /** 用户可读消息 */
  readonly message: string
  /** 字段级错误：字段名 -> 错误描述（FastAPI 422 校验错误解析而来） */
  readonly fieldErrors: Record<string, string>
  /** 原始错误（仅调试用） */
  readonly raw: unknown

  constructor(shape: ApiErrorShape) {
    super(shape.message)
    this.name = 'ApiError'
    this.status = shape.status
    this.code = shape.code
    this.message = shape.message
    this.fieldErrors = shape.fieldErrors
    this.raw = shape.raw
  }
}

/** 网络层失败（请求未发出 / 无响应）的兜底错误对象。 */
function networkError(raw: unknown): ApiErrorShape {
  return {
    status: 0,
    code: null,
    message: '无法连接到服务器，请检查网络或稍后重试',
    fieldErrors: {},
    raw,
  }
}

/** 无 detail 或解析失败时的兜底文案。 */
function fallbackMessage(status: number): string {
  if (status === 500) return '服务器内部错误，请稍后重试'
  if (status === 503) return '服务暂时不可用，请稍后重试'
  if (status === 401) return '登录已过期，请重新登录'
  if (status === 403) return '没有权限执行此操作'
  if (status === 404) return '请求的资源不存在'
  return `请求失败（HTTP ${status}）`
}

/** 解析 FastAPI 422 detail 数组，返回字段错误表与可读消息。
 *  loc 形如 ['body','username'] 时视为字段级错误（key 取最后一节）；
 *  loc 只有 ['body']/['query'] 时属于整体错误，拼入消息列表。 */
function parseValidationDetail(
  detail: unknown,
): { fieldErrors: Record<string, string>; message: string } {
  const fieldErrors: Record<string, string> = {}
  const messages: string[] = []
  const scopedLocations = new Set(['body', 'query', 'header', 'path', 'cookie'])
  if (Array.isArray(detail)) {
    for (const item of detail) {
      if (item && typeof item === 'object') {
        const entry = item as { loc?: unknown; msg?: unknown }
        const loc = Array.isArray(entry.loc) ? entry.loc.map(String) : []
        const msg = typeof entry.msg === 'string' ? entry.msg : '参数不合法'
        if (loc.length >= 2 && scopedLocations.has(loc[0])) {
          // 字段级错误：取 loc 最后一节作为字段名（忽略外层 body 等前缀）
          fieldErrors[loc[loc.length - 1]] = msg
        } else {
          messages.push(msg)
        }
      }
    }
  }
  const message =
    messages.length > 0 ? messages.join('；') : Object.keys(fieldErrors).length > 0 ? '提交内容不合法，请检查表单' : ''
  return { fieldErrors, message }
}

/**
 * 把后端任意响应体归一化为统一错误对象。
 * 支持三种形态：
 *  1) {"detail": "message"}             —— 字符串 detail
 *  2) {"detail": {"code","message"}}    —— 结构化 detail（知识库/审批）
 *  3) {"detail": [{loc,msg,...}]}       —— FastAPI 422 校验数组
 */
export function toApiError(status: number, body: unknown, raw: unknown): ApiErrorShape {
  // 网络层失败（未收到任何响应）：给出明确的连接失败提示
  if (status === 0) {
    return networkError(raw)
  }

  let code: string | null = null
  let message = ''
  let fieldErrors: Record<string, string> = {}

  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail?: unknown }).detail
    if (typeof detail === 'string') {
      // 形态 1：字符串 detail 直接作为用户可读消息（映射已知英文文案）
      message = translateBackendMessage(detail)
    } else if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
      // 形态 2：结构化 detail，取稳定 code 与 message（message 按 code 兜底翻译）
      const structured = detail as { code?: unknown; message?: unknown }
      code = typeof structured.code === 'string' ? structured.code : null
      message = typeof structured.message === 'string' ? translateBackendMessage(structured.message, code) : ''
    } else {
      // 形态 3：FastAPI 校验错误数组
      const parsed = parseValidationDetail(detail)
      fieldErrors = parsed.fieldErrors
      message = parsed.message
    }
  } else if (body && typeof body === 'object') {
    // 兼容个别直接返回 {code,message} 的情况（无 detail 包装）
    const direct = body as { code?: unknown; message?: unknown }
    if (typeof direct.code === 'string') code = direct.code
    if (typeof direct.message === 'string') message = translateBackendMessage(direct.message, code)
  }

  if (!message) {
    message = fallbackMessage(status)
  }
  return { status, code, message, fieldErrors, raw }
}

/** 后端已知英文 detail 消息 → 中文文案映射（未命中时原样透出）。 */
const BACKEND_MESSAGE_MAP: Record<string, string> = {
  'authentication required': '请先登录',
  'invalid or expired access token': '登录已过期，请重新登录',
  'invalid username or password': '用户名或密码错误',
  'username already exists': '用户名已被注册',
  'username must contain 3-32 lowercase letters, digits, or underscores':
    '用户名须为 3–32 位小写字母、数字或下划线',
  'password must contain between 8 and 72 utf-8 bytes': '密码长度须为 8–72 个字符',
  'organization context required': '缺少企业上下文，请重新选择企业',
  'organization not found': '企业不存在，或您已不在该企业中',
  'organization name must contain 2-100 characters': '企业名称长度须为 2–100 个字符',
  'admin role required': '该操作需要企业管理员权限',
  'user not found': '用户不存在，请确认对方已注册且用户名正确',
  'user is already an organization member': '该用户已是本企业成员',
  'conversation not found error': '会话不存在或已失效',
}

/** 后端稳定错误码 → 中文兜底文案（结构化错误优先按 code 提示）。 */
const CODE_MESSAGE_MAP: Record<string, string> = {
  DOCUMENT_NOT_FOUND: '文档不存在或已被移除',
  INVALID_DOCUMENT: '文档内容不合法，请检查文件类型、大小与编码',
  DOCUMENT_DISABLED: '文档已停用',
  DUPLICATE_DOCUMENT_VERSION: '相同内容的文档版本已存在',
  INGESTION_JOB_NOT_FOUND: '入库任务不存在',
  APPROVAL_NOT_FOUND: '审批不存在或不属于当前企业',
  PROPOSAL_NOT_FOUND: '提案不存在或不属于当前企业',
  RUN_NOT_FOUND: '任务不存在或不属于当前企业',
  APPROVAL_ADMIN_REQUIRED: '只有企业管理员可以执行此操作',
  APPROVAL_ALREADY_DECIDED: '该审批已被处理，请刷新列表查看最新状态',
  APPROVAL_INVALID_CHANGES: '修改内容不合法，请检查金额、原因或范围',
  RUN_NOT_RESUMABLE: '该任务当前不可恢复',
  CHECKPOINT_UNAVAILABLE: '服务暂时不可用，决定仍然有效，可稍后重试',
  EXECUTION_RETRYABLE_FAILURE: '执行遇到暂时性失败，可稍后重试',
}

/** 把后端英文消息/稳定错误码翻译为中文（未命中映射时原样返回）。 */
export function translateBackendMessage(message: string, code?: string | null): string {
  if (code && CODE_MESSAGE_MAP[code]) {
    return CODE_MESSAGE_MAP[code]
  }
  return BACKEND_MESSAGE_MAP[message] ?? message
}

/** 从任意异常生成统一错误对象：ApiError 原样返回，其余包装为通用错误。 */
export function toApiErrorFromThrowable(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error
  }
  return new ApiError(networkError(error))
}
