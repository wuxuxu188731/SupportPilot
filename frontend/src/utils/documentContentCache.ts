/*
 * 正文查看的内存缓存（"内存 + 会话内"，不落 localStorage）。
 *
 * 背景：关闭正文面板会卸载查看器组件，如果缓存放在组件里就会随之消失，
 * 再次打开同一条引用又要重新请求正文；而设计稿要求「同一
 * (document_id, version_id) 只请求一次」覆盖"关闭再打开"的场景。
 * 因此缓存放在模块级，并挂到 tenantReset 机制上：企业切换 / 退出登录即清空，
 * 旧企业的正文绝不会留给新企业继续展示。
 *
 * 键的构成：`documentId/versionId`。**必须带 version_id**——知识块偏移相对某一个
 * 版本的正文，版本一换偏移即失效，缓存键若不含版本会串版本。
 */

import type { DocumentContentResponse } from '@/api/knowledgeTypes'
import { registerTenantResetHandler } from '@/stores/tenantReset'

/** 正文缓存：key 为 `documentId/versionId`。 */
const contentCache = new Map<string, DocumentContentResponse>()

/** 当前有效版本号缓存：key 为 `documentId`（历史版本提示需要它，避免反复请求详情）。 */
const activeVersionNumbers = new Map<string, number>()

/** 组装缓存键：文档 + 版本必须成对，避免跨版本串用。 */
export function contentCacheKey(documentId: string, versionId: string): string {
  return `${documentId}/${versionId}`
}

/** 读取已缓存的正文；未缓存返回 undefined。 */
export function readCachedContent(
  documentId: string,
  versionId: string,
): DocumentContentResponse | undefined {
  return contentCache.get(contentCacheKey(documentId, versionId))
}

/** 写入正文缓存。 */
export function writeCachedContent(response: DocumentContentResponse): void {
  contentCache.set(contentCacheKey(response.document_id, response.version_id), response)
}

/** 失效某个版本的正文缓存（重试时使用）。 */
export function invalidateCachedContent(documentId: string, versionId: string): void {
  contentCache.delete(contentCacheKey(documentId, versionId))
}

/** 读取缓存的当前有效版本号。 */
export function readCachedActiveVersionNo(documentId: string): number | undefined {
  return activeVersionNumbers.get(documentId)
}

/** 写入当前有效版本号。 */
export function writeCachedActiveVersionNo(documentId: string, versionNo: number): void {
  activeVersionNumbers.set(documentId, versionNo)
}

/** 清空全部正文缓存（企业切换 / 退出登录时调用）。 */
export function clearDocumentContentCache(): void {
  contentCache.clear()
  activeVersionNumbers.clear()
}

// 企业切换 / 退出登录：租户相关缓存一律清空
registerTenantResetHandler(clearDocumentContentCache)
