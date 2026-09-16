/*
 * 知识库 API 请求/响应类型（DTO）。
 *
 * 与后端 `app/schemas/knowledge.py`、`docs/frontend/api-inventory.md`
 * 4.4 节及附录 A.9/A.10 一一对应；所有枚举值均以后端
 * `app/knowledge/base.py` 为准，不得猜测。
 * 所有属性均带中文注释；字段可空时显式标注 null（与后端 JSON 一致）。
 *
 * 事实说明：
 *  - 文档**详情与版本响应不包含正文/原始文件/下载地址**，前端不得从这两个
 *    响应伪造正文预览或下载入口；
 *  - 正文只允许从正文接口（`DocumentContentResponse`，按版本读取）获取，
 *    见 docs/frontend/api-inventory.md 4.4.8；
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

/** 上传结果回执（后端 IngestionReceiptResponse）。 */
export interface IngestionReceipt {
  /** 文档唯一标识（新文档上传成功后用于跳转详情） */
  document_id: string
  /** 本次上传对应的版本标识；deduplicated=true 时为已存在版本的 id */
  version_id: string
  /** 本次入库任务唯一标识（用于查询任务状态/轮询） */
  job_id: string
  /**
   * 入库任务状态。后端已改为异步入库：上传接口登记入队后立刻返回 queued，
   * 解析与向量写入由后台 worker 推进，前端按 queued/running 轮询到终态。
   * 只有「内容重复、复用既有成功版本」时才可能直接看到 succeeded。
   */
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
  /**
   * 规范化文本的 SHA-256 内容哈希（"sha256:<hex>"）：用于识别重复内容。
   * 异步入库下版本行会先于解析结果落库，因此尚未解析完成时为 null。
   */
  content_hash: string | null
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

/**
 * 正文目录项（后端 DocumentOutlineItemResponse）：正文中的一个标题及其字符偏移。
 *
 * 事实：目录由服务端按与切块同一套标题规则提取，前端**不得**再自行解析正文标题，
 * 否则目录偏移与知识块的 heading_path 会不一致。
 */
export interface DocumentContentOutlineItem {
  /** 标题层级：1-6，对应 Markdown 中 '#' 的个数 */
  level: number
  /** 标题文本（已去掉行首 '#' 与首尾空白） */
  title: string
  /** 完整标题路径（各级标题以 '/' 连接且含本标题自身），与引用 heading_path 同一口径 */
  heading_path: string
  /** 该标题行首在正文中的绝对字符偏移（Python 字符计数，非字节、非行号） */
  char_offset: number
}

/**
 * 指定版本的归一化 Markdown 正文（后端 DocumentContentResponse）。
 *
 * 这是知识库接口中**唯一**携带正文的响应（详情/版本接口永不含 raw_text）。
 * `text` 就是入库时解析/转换后的那份 Markdown：PDF/Word 也已在入库阶段转换完成，
 * 因此前端渲染的是它，而不是 PDF/Word 原文。
 *
 * 偏移口径：知识块的 start_offset/end_offset 与目录项的 char_offset 都是相对
 * **这个版本** `text` 的字符下标；版本一换偏移即失效，所以读取必须显式带 version_id。
 */
export interface DocumentContentResponse {
  /** 文档唯一标识 */
  document_id: string
  /** 版本标识（本次返回正文的确切版本） */
  version_id: string
  /** 版本序号：用于「该引用来自历史版本 vN，当前有效版本为 vM」提示 */
  version_no: number
  /** 文档标题（服务端可信来源，取自文档表，不采用引用里的标题） */
  title: string
  /** 来源类型：word/pdf 表示正文是入库时自动转换的 Markdown（查看器不再为此单独上屏提示） */
  source_type: DocumentSourceTypeValue
  /** 文档当前状态：disabled 表示已停用（正文仍可读，但不再参与检索） */
  status: DocumentStatusValue
  /** 文档当前有效版本；与 version_id 不同即说明本次读的是历史版本 */
  active_version_id: string | null
  /** 入库时使用的解析器版本标识（可追溯性） */
  loader_version: string
  /** 入库时使用的分块器版本标识（偏移语义所属版本，可追溯性） */
  chunker_version: string
  /** 该版本归一化正文的 sha256（"sha256:<hex>"）；未解析的版本不会走到本响应 */
  content_hash: string | null
  /** 归一化 Markdown 正文（UTF-8 解码后、行尾统一为 \n） */
  text: string
  /** 正文字符数（Python 字符计数，不是 utf-8 字节数）；前端据此做偏移越界自检 */
  text_length: number
  /** 标题目录；没有标题的文档为空数组 */
  outline: DocumentContentOutlineItem[]
}
