/*
 * 知识库 API 封装：/knowledge/ 的文档列表、详情、正文读取、上传、新版本、
 * 停用/启用与入库任务查询。
 *
 * 关键约定（与后端契约一致）：
 *  - 所有上传请求使用浏览器 FormData，字段**必须恰好**为 title + file：
 *    禁止附加 organization_id / source_type / 任何其它字段（后端对额外
 *    字段直接 422）；
 *  - 不得手动设置 Content-Type: multipart/form-data：浏览器/Axios 必须
 *    自行生成带 boundary 的 Content-Type；
 *  - 上传请求使用独立、可配置的长超时（默认 180 秒，
 *    见 getKnowledgeUploadTimeoutMs），因为知识入库会同步执行文档解析、
 *    分块、Embedding、向量库写入与激活版本；
 *  - 上传**不允许自动重试**：服务端可能在请求返回前就已完成入库，
 *    客户端自动重试会造成重复文档或重复版本；超时/断网后由界面提示
 *    「结果状态可能不确定」，用户刷新文档列表后自行决定是否重新上传。
 */

import { httpClient } from './http'
import type {
  DocumentContentResponse,
  IngestionJobInfo,
  IngestionReceipt,
  KnowledgeDocumentDetail,
  KnowledgeDocumentSummary,
} from './knowledgeTypes'

/** 读取知识库上传请求超时毫秒数：优先使用 VITE_KNOWLEDGE_UPLOAD_TIMEOUT_MS。 */
export function getKnowledgeUploadTimeoutMs(): number {
  const raw = import.meta.env.VITE_KNOWLEDGE_UPLOAD_TIMEOUT_MS
  const parsed = raw ? Number.parseInt(raw, 10) : Number.NaN
  // 环境变量非法或未设置时使用默认长超时（180 秒）
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 180_000
}

/** 构造上传表单：只包含 title 与 file 两个字段（禁止附加任何其它字段）。 */
function buildUploadForm(title: string, file: File): FormData {
  const form = new FormData()
  form.append('title', title)
  form.append('file', file)
  return form
}

/** 拉取当前企业全部文档摘要（后端无分页/搜索，按创建时间升序）。 */
export async function listKnowledgeDocuments(): Promise<KnowledgeDocumentSummary[]> {
  const response = await httpClient.get<KnowledgeDocumentSummary[]>('/knowledge/documents/')
  return response.data
}

/**
 * 读取单个文档详情（含全部版本与有效版本最近入库任务）。
 * 响应不含正文/下载地址；跨企业或不存在统一 404。
 */
export async function getKnowledgeDocumentDetail(
  documentId: string,
): Promise<KnowledgeDocumentDetail> {
  const response = await httpClient.get<KnowledgeDocumentDetail>(
    `/knowledge/documents/${encodeURIComponent(documentId)}/`,
  )
  return response.data
}

/**
 * 上传新文档（仅 admin）并等待同步入库完成。
 *
 * - 使用 FormData（仅 title + file）并由浏览器生成 multipart 头；
 * - 使用独立长超时（getKnowledgeUploadTimeoutMs）；绝不自动重试；
 * - 成功返回 IngestionReceipt（status / deduplicated 由调用方分流处理）。
 */
export async function uploadKnowledgeDocument(input: {
  /** 文档标题：strip 后 1–200 字符 */
  title: string
  /** 上传的单个文件（.md/.markdown/.txt，≤2MiB，合法 UTF-8） */
  file: File
}): Promise<IngestionReceipt> {
  const form = buildUploadForm(input.title, input.file)
  const response = await httpClient.post<IngestionReceipt>('/knowledge/documents/', form, {
    timeout: getKnowledgeUploadTimeoutMs(),
  })
  return response.data
}

/**
 * 为既有文档上传新版本（仅 admin）。
 *
 * 后端要求 title 字段，但版本上传不会重命名文档：调用方必须传入当前文档
 * 标题；文件扩展名类型必须与原文档 source_type 一致；
 * 成功后新版本自动成为 active 版本。deduplicated=true 表示内容与当前有效
 * 版本相同，未创建重复版本。
 */
export async function uploadKnowledgeDocumentVersion(
  documentId: string,
  input: {
    /** 当前文档标题（版本上传不提供重命名能力，必须传原标题） */
    title: string
    /** 上传的单个文件（类型必须与原文档一致） */
    file: File
  },
): Promise<IngestionReceipt> {
  const form = buildUploadForm(input.title, input.file)
  const response = await httpClient.post<IngestionReceipt>(
    `/knowledge/documents/${encodeURIComponent(documentId)}/versions/`,
    form,
    { timeout: getKnowledgeUploadTimeoutMs() },
  )
  return response.data
}

/** 停用文档（仅 admin）：不再参与知识检索，版本与任务记录保留。 */
export async function disableKnowledgeDocument(
  documentId: string,
): Promise<KnowledgeDocumentSummary> {
  const response = await httpClient.post<KnowledgeDocumentSummary>(
    `/knowledge/documents/${encodeURIComponent(documentId)}/disable/`,
  )
  return response.data
}

/** 重新启用文档（仅 admin）：当前有效版本重新参与知识检索。 */
export async function enableKnowledgeDocument(
  documentId: string,
): Promise<KnowledgeDocumentSummary> {
  const response = await httpClient.post<KnowledgeDocumentSummary>(
    `/knowledge/documents/${encodeURIComponent(documentId)}/enable/`,
  )
  return response.data
}

/**
 * 读取指定版本的归一化 Markdown 正文（成员均可读，含历史版本）。
 *
 * 契约要点：
 *  - **必须显式带 version_id**：知识块的 start_offset/end_offset 相对某一个版本的
 *    正文，版本一换偏移即失效，因此不能用「当前有效版本」隐式替代；
 *  - 可读操作，走 httpClient 默认超时（60 秒），不需要上传用的长超时；
 *  - 404 的四种情形（文档不存在 / 跨企业 / 版本不属于该文档 / 版本尚未解析出正文）
 *    口径一致，前端统一按「来源文档不存在或已不可访问」提示；
 *  - 文档被停用（disabled）时正文仍可读：历史引用可能正指向它。
 */
export async function getDocumentVersionContent(
  documentId: string,
  versionId: string,
): Promise<DocumentContentResponse> {
  const response = await httpClient.get<DocumentContentResponse>(
    `/knowledge/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}/content/`,
  )
  return response.data
}

/** 查询单个入库任务状态（成员均可读；跨企业或不存在统一 404）。 */
export async function getIngestionJob(jobId: string): Promise<IngestionJobInfo> {
  const response = await httpClient.get<IngestionJobInfo>(
    `/knowledge/ingestion-jobs/${encodeURIComponent(jobId)}/`,
  )
  return response.data
}
