/*
 * 本地持久化读写（认证令牌与企业上下文）。
 *
 * 设计说明：
 *  - Token 保存在 localStorage。这是前端常见的便捷方案，但存在 XSS 读取
 *    风险，并非「完全安全」的存储（详见 frontend/README.md 安全边界一节）；
 *  - 拦截器与 Pinia Store 都从本模块读写，避免 Store <-> Axios 循环依赖；
 *  - 多标签页之间不做实时同步（简单场景可接受，注释说明）。
 */

/** 认证持久化键名。 */
export const AUTH_STORAGE_KEY = 'supportpilot.auth'
/** 当前企业持久化键名。 */
export const ORGANIZATION_STORAGE_KEY = 'supportpilot.organization'

/** 本地持久化的认证信息（对应后端 TokenResponse + 本地保存时间）。 */
export interface PersistedAuth {
  /** 访问令牌（JWT） */
  accessToken: string
  /** 令牌类型（后端固定 "bearer"） */
  tokenType: string
  /** 令牌有效秒数 */
  expiresIn: number
  /** 本地保存时间（epoch 毫秒），用于推导过期时刻 */
  savedAt: number
}

/** 本地持久化的企业选择。 */
export interface PersistedOrganization {
  /** 上次选择的企业 id；刷新页面后据此恢复 */
  organizationId: string
}

function readJson<T>(key: string): T | null {
  try {
    const raw = window.localStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : null
  } catch {
    // 本地数据损坏时按「不存在」处理，让用户重新登录/选择
    return null
  }
}

function writeJson(key: string, value: unknown): void {
  window.localStorage.setItem(key, JSON.stringify(value))
}

/** 读取本地认证信息；不存在或损坏时返回 null。 */
export function readAuthStorage(): PersistedAuth | null {
  return readJson<PersistedAuth>(AUTH_STORAGE_KEY)
}

/** 写入认证信息到本地存储（登录成功时调用）。 */
export function writeAuthStorage(value: PersistedAuth): void {
  writeJson(AUTH_STORAGE_KEY, value)
}

/** 清除本地认证信息（退出登录/登录过期时调用）。 */
export function clearAuthStorage(): void {
  window.localStorage.removeItem(AUTH_STORAGE_KEY)
}

/** 读取本地保存的企业 id；不存在时返回 null。 */
export function readOrganizationStorage(): string | null {
  const stored = readJson<PersistedOrganization>(ORGANIZATION_STORAGE_KEY)
  return stored?.organizationId ?? null
}

/** 保存当前企业 id 到本地存储（选择/切换企业时调用）。 */
export function writeOrganizationStorage(organizationId: string): void {
  writeJson(ORGANIZATION_STORAGE_KEY, { organizationId })
}

/** 清除本地企业选择（失去权限/退出登录时调用）。 */
export function clearOrganizationStorage(): void {
  window.localStorage.removeItem(ORGANIZATION_STORAGE_KEY)
}
