/*
 * 知识库 API 请求/响应类型（DTO）。
 *
 * 与后端 `app/schemas/knowledge.py`、`docs/frontend/api-inventory.md`
 * 4.4 节及附录 A.9/A.10 一一对应；所有枚举值均以后端
 * `app/knowledge/base.py` 为准，不得猜测。
 * 所有属性均带中文注释；字段可空时显式标注 null（与后端 JSON 一致）。
 *
 * 事实说明：
 *  - 文档详情与版本响应**不包含正文/原始文件/下载地址**，前端不得伪造
 *    正文预览或下载入口；
 *  - 未知枚举值由展示层安全降级为原始字符串（见 utils/knowledgeDisplay.ts）。
 */

// —— 枚举取值（仅从后端代码取值，不新增不改名） ——

/** 文档来源类型（后端 DocumentSourceType）：markdown/text 本地解码，word/pdf 由解析服务提取。 */
export type DocumentSourceTypeValue = 'markdown' | 'text' | 'word' | 'pdf'

/** 文档生命周期状态（后端 DocumentStatus）。 */
export type DocumentStatusValue = 'processing' | 'active' | 'disabled' | 'failed'

/** 入库任务状态（后端 IngestionStatus）。 */
export type IngestionStatusValue = 'queued' | 'running' | 'succeeded' | 'failed'

// —— 响应模型 ——

/** 上传结果回执（后端 IngestionReceiptResponse）：同步入库完成后的回执。 */
export interface IngestionReceipt {
  /** 文档唯一标识（新文档上传成功后用于跳转详情） */
  document_id: string
  /** 本次上传对应的版本标识；deduplicated=true 时为已存在版本的 id */
  version_id: string
  /** 本次入库任务唯一标识（用于查询任务状态/轮询） */
  job_id: string
  /** 入库任务状态：同步管线成功时通常为 succeeded，也可能 queued/running/failed */
  status: IngestionStatusValue
  /** 是否与当前有效版本内容相同：服务端复用既有版本，未创建重复版本 */
  deduplicated: boolean
}

/** 文档版本信息（后端 DocumentVersionResponse）：不含正文与下载地址。 */
export interface DocumentVersionInfo {
  /** 版本唯一标识 */
  version_id: string
  /** 版本序号：从 1 递增；新版本成功后自动成为 active 版本 */
  version_no: number
  /** 规范化文本的 SHA-256 内容哈希（"sha256:<hex>"）：用于识别重复内容 */
  content_hash: string
  /** 入库时使用的文档解析器版本标识 */
  loader_version: string
  /** 入库时使用的分块器版本标识 */
  chunker_version: string
  /** 入库时使用的 Embedding 模型标识 */
  embedding_model: string
  /** 入库时使用的向量维度 */
  embedding_dimensions: number
  /** 版本创建时间（UTC ISO 文本，带 +00:00 后缀） */
  created_at: string
}

/** 入库任务状态详情（后端 IngestionJobResponse）。 */
export interface IngestionJobInfo {
  /** 任务唯一标识 */
  job_id: string
  /** 任务所属文档标识 */
  document_id: string
  /** 任务所属版本标识 */
  version_id: string
  /** 任务状态：queued / running / succeeded / failed */
  status: IngestionStatusValue
  /** 已尝试次数（服务端保留重试次数） */
  attempt_count: number
  /** 稳定错误码（如 INGESTION_FAILED / EMBEDDING_UNAVAILABLE）；未失败为 null */
  error_code: string | null
  /** 安全错误消息（不包含堆栈/密钥/正文内容）；未失败为 null */
  error_message: string | null
  /** 任务开始时间（UTC ISO 文本）；尚未开始为 null */
  started_at: string | null
  /** 任务结束时间（UTC ISO 文本）；未结束为 null */
  finished_at: string | null
  /** 任务创建时间（UTC ISO 文本） */
  created_at: string
}

/** 知识库文档摘要（后端 KnowledgeDocumentSummaryResponse）：列表元素。 */
export interface KnowledgeDocumentSummary {
  /** 文档唯一标识 */
  document_id: string
  /** 文档标题（上传者提供，服务端 strip 后 1–200 字符） */
  title: string
  /** 文档来源类型：markdown / text / word */
  source_type: DocumentSourceTypeValue
  /** 文档当前状态：processing / active / disabled / failed */
  status: DocumentStatusValue
  /** 当前有效版本标识；无有效版本（processing/failed 等）为 null */
  active_version_id: string | null
  /** 文档创建时间（UTC ISO 文本，带 +00:00 后缀） */
  created_at: string
  /** 文档最后更新时间（UTC ISO 文本） */
  updated_at: string
}

/** 知识库文档详情（后端 KnowledgeDocumentDetailResponse）。 */
export interface KnowledgeDocumentDetail extends KnowledgeDocumentSummary {
  /** 全部版本列表（按 version_no 升序；不含正文与下载地址） */
  versions: DocumentVersionInfo[]
  /** 有效版本最近一次入库任务；无有效版本或从未成功入库为 null */
  latest_job: IngestionJobInfo | null
}
