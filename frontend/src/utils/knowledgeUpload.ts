/*
 * 知识库上传文件的客户端验证（独立纯函数，便于单元测试）。
 *
 * 事实说明：
 *  - 前端验证只用于改善体验（提前给出可读错误），**不能替代后端校验**：
 *    service 端仍会兜底校验类型/大小/编码/标题并返回 422；
 *  - 扩展名判定不区分大小写：.MD / .Markdown / .DOCX 均视为合法；
 *  - `.md` / `.markdown` 映射为 markdown，`.txt` 映射为 text，
 *    `.docx` 映射为 word，`.pdf` 映射为 pdf；其余扩展名一律拒绝；
 *  - **word / pdf 是二进制格式，不做 UTF-8 校验**：真实 docx（zip 包）与 pdf
 *    的字节都不是合法 UTF-8，若套用文本校验会把它们全部误判为「编码非法」。
 *    后端 loader 对这两种类型同样不做本地解码，前后端语义保持一致；
 *  - Word / PDF 文档与其它类型一样支持上传新版本（后端已支持同类型覆盖）。
 */

import type { DocumentSourceTypeValue } from '@/api/knowledgeTypes'

/** 单个文档最大字节数：2MiB（与后端 MAX_DOCUMENT_BYTES 一致）。 */
export const KNOWLEDGE_MAX_FILE_BYTES = 2 * 1024 * 1024

/** 文档标题最小长度（strip 后）。 */
export const KNOWLEDGE_TITLE_MIN_LENGTH = 1

/** 文档标题最大长度（strip 后，与后端 MAX_TITLE_LENGTH 一致）。 */
export const KNOWLEDGE_TITLE_MAX_LENGTH = 200

/** 校验错误归属字段：title / file / form（整体错误）。 */
export type KnowledgeIssueField = 'title' | 'file' | 'form'

/** 稳定的校验错误码（机器可读，供展示映射与测试断言）。 */
export type KnowledgeUploadIssueCode =
  | 'multiple-files'
  | 'missing-file'
  | 'unsupported-extension'
  | 'empty-file'
  | 'file-too-large'
  | 'invalid-utf8'
  | 'title-blank'
  | 'title-too-long'
  | 'type-mismatch'

/** 上传校验问题对象。 */
export interface KnowledgeUploadIssue {
  /** 稳定错误码 */
  code: KnowledgeUploadIssueCode
  /** 错误归属字段（title / file / form） */
  field: KnowledgeIssueField
  /** 用户可读中文消息 */
  message: string
}

/** 待上传文件的最小接口：真实 File 满足；单测可用轻量实现。 */
export interface UploadFileLike {
  /** 文件名（含扩展名） */
  name: string
  /** 文件字节数（0 表示空文件） */
  size: number
  /** 读取文件内容（浏览器 File 提供 arrayBuffer；测试环境可缺失，走 FileReader 兜底） */
  arrayBuffer?: () => Promise<ArrayBuffer>
}

/** 需要本地按 UTF-8 解码的文本类型；word/pdf 是二进制，跳过编码校验。 */
const TEXT_SOURCE_TYPES: readonly DocumentSourceTypeValue[] = ['markdown', 'text']

/** 各来源类型可接受的扩展名（用于「类型不匹配」提示文案）。 */
const EXPECTED_EXTENSIONS: Record<string, string> = {
  markdown: '.md / .markdown',
  text: '.txt',
  word: '.docx',
  pdf: '.pdf',
}

/** 是否为需要 UTF-8 校验的文本类型（word/pdf 为二进制，返回 false）。 */
export function isTextSourceType(type: DocumentSourceTypeValue | null): boolean {
  return type !== null && TEXT_SOURCE_TYPES.includes(type)
}

/** 根据文件名推导来源类型：扩展名不区分大小写；不支持的类型返回 null。 */
export function detectSourceTypeByFilename(
  filename: string,
): DocumentSourceTypeValue | null {
  const lower = (filename || '').toLowerCase()
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) return 'markdown'
  if (lower.endsWith('.txt')) return 'text'
  if (lower.endsWith('.docx')) return 'word'
  if (lower.endsWith('.pdf')) return 'pdf'
  return null
}

/** 校验标题：strip 后必须为 1–200 字符（返回第一个问题或 null）。 */
export function validateKnowledgeTitle(title: string): KnowledgeUploadIssue | null {
  const clean = (title ?? '').trim()
  if (clean.length < KNOWLEDGE_TITLE_MIN_LENGTH) {
    return { code: 'title-blank', field: 'title', message: '标题不能为空' }
  }
  if (clean.length > KNOWLEDGE_TITLE_MAX_LENGTH) {
    return {
      code: 'title-too-long',
      field: 'title',
      message: `标题不能超过 ${KNOWLEDGE_TITLE_MAX_LENGTH} 个字符`,
    }
  }
  return null
}

/**
 * 校验文件基础属性（不读取内容）：
 * 未选择 / 扩展名不支持 / 文件为空 / 超过 2MiB。
 */
export function validateKnowledgeFileBasics(file: UploadFileLike | null): KnowledgeUploadIssue | null {
  if (file === null || file === undefined) {
    return { code: 'missing-file', field: 'file', message: '请选择要上传的文件' }
  }
  if (detectSourceTypeByFilename(file.name) === null) {
    return {
      code: 'unsupported-extension',
      field: 'file',
      message: '仅支持 .md、.markdown、.txt、.docx 或 .pdf 文件',
    }
  }
  if (file.size <= 0) {
    return { code: 'empty-file', field: 'file', message: '文件内容为空，请选择非空文件' }
  }
  if (file.size > KNOWLEDGE_MAX_FILE_BYTES) {
    return {
      code: 'file-too-large',
      field: 'file',
      message: '文件不能超过 2MiB（即 2 × 1024 × 1024 字节）',
    }
  }
  return null
}

/** 校验内容是否为合法 UTF-8（TextDecoder 严格模式，与后端 decode 语义一致）。 */
export function isValidUtf8(bytes: Uint8Array): boolean {
  try {
    // 严格模式：非法字节序列直接抛错，绝不使用替换字符
    new TextDecoder('utf-8', { fatal: true }).decode(bytes)
    return true
  } catch {
    return false
  }
}

/** 通过 FileReader 读取文件字节（jsdom/旧浏览器无 arrayBuffer 时的兜底）。 */
function readWithFileReader(file: UploadFileLike): Promise<Uint8Array | null> {
  return new Promise((resolve) => {
    try {
      const reader = new FileReader()
      reader.onload = () => {
        const result = reader.result
        resolve(result instanceof ArrayBuffer ? new Uint8Array(result) : null)
      }
      reader.onerror = () => resolve(null)
      reader.readAsArrayBuffer(file as unknown as Blob)
    } catch {
      resolve(null)
    }
  })
}

/** 读取文件内容为字节数组；读取失败返回 null。 */
async function readFileBytes(file: UploadFileLike): Promise<Uint8Array | null> {
  if (typeof file.arrayBuffer === 'function') {
    try {
      return new Uint8Array(await file.arrayBuffer())
    } catch {
      return null
    }
  }
  return readWithFileReader(file)
}

/** 读取文件内容并校验 UTF-8（返回问题对象或 null）。 */
export async function validateKnowledgeFileUtf8(file: UploadFileLike): Promise<KnowledgeUploadIssue | null> {
  const bytes = await readFileBytes(file)
  if (bytes === null) {
    return { code: 'invalid-utf8', field: 'file', message: '无法读取文件内容，请重新选择' }
  }
  if (!isValidUtf8(bytes)) {
    return {
      code: 'invalid-utf8',
      field: 'file',
      message: '文件内容不是合法的 UTF-8 文本，请转换为 UTF-8 编码后重试',
    }
  }
  return null
}

/**
 * 校验「新文档」上传：标题 → 文件属性 → UTF-8（仅文本类型）。
 * 返回第一个问题或 null（全部通过）。
 */
export async function validateNewDocumentUpload(input: {
  /** 文档标题（strip 后 1–200 字符） */
  title: string
  /** 单个待上传文件 */
  file: UploadFileLike | null
}): Promise<KnowledgeUploadIssue | null> {
  const titleIssue = validateKnowledgeTitle(input.title)
  if (titleIssue) return titleIssue
  if (input.file === null || input.file === undefined) {
    return { code: 'missing-file', field: 'file', message: '请选择要上传的文件' }
  }
  const fileIssue = validateKnowledgeFileBasics(input.file)
  if (fileIssue) return fileIssue
  return validateEncodingFor(input.file)
}

/**
 * 按文件推导出的类型决定是否做 UTF-8 校验。
 *
 * word/pdf 直接放行：docx 是 zip 包、pdf 含二进制流，两者都不是合法 UTF-8，
 * 套用文本校验会把它们全部误判为「编码非法」而根本传不上去。
 */
async function validateEncodingFor(
  file: UploadFileLike,
): Promise<KnowledgeUploadIssue | null> {
  if (!isTextSourceType(detectSourceTypeByFilename(file.name))) return null
  return validateKnowledgeFileUtf8(file)
}

/**
 * 校验「新版本」上传：文件属性 → 类型匹配 → UTF-8（仅文本类型）。
 * 返回第一个问题或 null（全部通过）。
 *
 * 注意：新建版本的文件类型必须与原文档 source_type 完全一致——
 * markdown 接受 .md/.markdown，text 只接受 .txt，word 只接受 .docx，
 * pdf 只接受 .pdf。
 */
export async function validateNewVersionUpload(
  input: {
    /** 单次只允许一个文件（由输入控件约束，这里再兜底） */
    fileCount: number
    /** 单个待上传文件 */
    file: UploadFileLike | null
    /** 被上传版本的目标文档类型 */
    documentSourceType: DocumentSourceTypeValue | string
  },
): Promise<KnowledgeUploadIssue | null> {
  if (input.fileCount > 1) {
    return { code: 'multiple-files', field: 'file', message: '一次只能上传一个文件' }
  }
  if (input.file === null || input.file === undefined) {
    return { code: 'missing-file', field: 'file', message: '请选择要上传的文件' }
  }
  const fileIssue = validateKnowledgeFileBasics(input.file)
  if (fileIssue) return fileIssue
  const detected = detectSourceTypeByFilename(input.file.name)
  if (detected !== input.documentSourceType) {
    const expected = EXPECTED_EXTENSIONS[String(input.documentSourceType)] ?? '原文档类型'
    return {
      code: 'type-mismatch',
      field: 'file',
      message: `文件类型不匹配：该文档需要 ${expected} 文件`,
    }
  }
  return validateEncodingFor(input.file)
}
