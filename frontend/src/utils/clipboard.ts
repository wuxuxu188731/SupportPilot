/*
 * 剪贴板工具：复制长 ID / 哈希到剪贴板。
 *
 * 事实说明：
 *  - 优先使用浏览器 Clipboard API（需安全上下文）；不可用时降级为
 *    execCommand 方案；两者都不可用时静默返回 false，绝不让复制失败
 *    打断页面（调用方自行决定是否提示）。
 */

/** 复制文本到剪贴板；成功返回 true，失败返回 false（不抛错）。 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false
  // 优先：Clipboard API（页面必须是安全上下文或显式授权）
  if (typeof navigator !== 'undefined' && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 授权失败/不可用：走降级方案
    }
  }
  // 降级：临时 textarea + execCommand（旧浏览器/非安全上下文）
  if (typeof document !== 'undefined') {
    try {
      const textarea = document.createElement('textarea')
      textarea.value = text
      textarea.setAttribute('readonly', '')
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(textarea)
      return ok
    } catch {
      return false
    }
  }
  return false
}
