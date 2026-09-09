/// <reference types="vite/client" />

// 环境变量类型声明：为 src 中读取的 import.meta.env 提供类型提示。
// 变量说明：
// VITE_API_BASE_URL    Axios 基础地址，开发环境默认 /api（Vite 代理前缀）
// VITE_PROXY_TARGET    Vite 开发代理的目标后端地址（仅 vite.config 使用）
// VITE_CHAT_TIMEOUT_MS 聊天请求独立长超时（毫秒），未设置时默认 180000
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_PROXY_TARGET?: string
  readonly VITE_CHAT_TIMEOUT_MS?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
