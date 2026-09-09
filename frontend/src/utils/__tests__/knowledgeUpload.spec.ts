/*
 * 知识库上传文件验证测试：扩展名映射（大小写）、类型拒绝、空文件、
 * 2MiB 上限、UTF-8 校验、标题长度、新版本类型匹配、Word 拒绝新版本。
 * 全部为纯函数测试，不触达网络与真实文件系统。
 */

import { describe, expect, it } from 'vitest'

import type { UploadFileLike } from '@/utils/knowledgeUpload'
import {
  KNOWLEDGE_MAX_FILE_BYTES,
  KNOWLEDGE_TITLE_MAX_LENGTH,
  detectSourceTypeByFilename,
  isValidUtf8,
  validateKnowledgeFileBasics,
  validateKnowledgeTitle,
  validateNewDocumentUpload,
  validateNewVersionUpload,
} from '@/utils/knowledgeUpload'

/** 构造轻量文件对象（仅测试验证逻辑，不需要真实 File 读取）。 */
function fileLike(name: string, size: number, content: Uint8Array = new Uint8Array(0)): UploadFileLike {
  return {
    name,
    size,
    arrayBuffer: async () => content.buffer.slice(content.byteOffset, content.byteOffset + content.byteLength) as ArrayBuffer,
  }
}

/** 把文本编码为 UTF-8 字节。 */
function utf8Bytes(text: string): Uint8Array {
  return new TextEncoder().encode(text)
}

describe('扩展名 → 来源类型映射（不区分大小写）', () => {
  it('.md / .markdown 映射为 markdown（含大写扩展名）', () => {
    // 保护行为：markdown 类型必须接受 .md 与 .markdown 两种扩展名
    expect(detectSourceTypeByFilename('a.md')).toBe('markdown')
    expect(detectSourceTypeByFilename('a.markdown')).toBe('markdown')
    expect(detectSourceTypeByFilename('A.MD')).toBe('markdown')
    expect(detectSourceTypeByFilename('A.Markdown')).toBe('markdown')
  })

  it('.txt 映射为 text（含大写扩展名）', () => {
    // 保护行为：text 类型只接受 .txt 扩展名
    expect(detectSourceTypeByFilename('a.txt')).toBe('text')
    expect(detectSourceTypeByFilename('A.TXT')).toBe('text')
  })

  it('.docx 及其它未知扩展名拒绝（映射为 null）', () => {
    // 保护行为：Word 文档当前 HTTP 上传不可用，.docx 不得映射为 word
    expect(detectSourceTypeByFilename('a.docx')).toBeNull()
    expect(detectSourceTypeByFilename('a.pdf')).toBeNull()
    expect(detectSourceTypeByFilename('a')).toBeNull()
    expect(detectSourceTypeByFilename('')).toBeNull()
  })
})

describe('文件基础属性校验', () => {
  it('未选择文件时报 missing-file', () => {
    // 保护行为：没有文件不允许提交，错误归属 file 字段
    const issue = validateKnowledgeFileBasics(null)
    expect(issue?.code).toBe('missing-file')
    expect(issue?.field).toBe('file')
  })

  it('不支持扩展名时报 unsupported-extension', () => {
    // 保护行为：.docx 文件必须在客户端就被拒绝
    const issue = validateKnowledgeFileBasics(fileLike('a.docx', 100))
    expect(issue?.code).toBe('unsupported-extension')
  })

  it('空文件（0 字节）被拒绝', () => {
    // 边界情况：0 字节文件没有任何内容，必须提前拦截
    const issue = validateKnowledgeFileBasics(fileLike('a.md', 0))
    expect(issue?.code).toBe('empty-file')
  })

  it('超过 2MiB（2*1024*1024）被拒绝，等于上限允许', () => {
    // 边界情况：恰好 2MiB 合法，多 1 字节即拒绝（与后端一致）
    expect(validateKnowledgeFileBasics(fileLike('a.md', KNOWLEDGE_MAX_FILE_BYTES + 1))?.code).toBe(
      'file-too-large',
    )
    expect(validateKnowledgeFileBasics(fileLike('a.md', 1024))).toBeNull()
  })
})

describe('UTF-8 校验（TextDecoder 严格模式）', () => {
  it('合法 UTF-8 内容通过', () => {
    // 保护行为：正常中文/英文文本必须被识别为合法 UTF-8
    expect(isValidUtf8(utf8Bytes('支持售后文档 2026'))).toBe(true)
  })

  it('非法字节序列被拒绝（不产生替换字符）', () => {
    // 边界情况：0xFF 是非法 UTF-8 起始字节，严格模式必须判定非法
    expect(isValidUtf8(new Uint8Array([0xff, 0xfe, 0x00]))).toBe(false)
  })

  it('多字节字符被截断时判定非法', () => {
    // 边界情况：4 字节字符少 1 字节属于非法序列
    expect(isValidUtf8(new Uint8Array([0xf0, 0x9f, 0x92]))).toBe(false)
  })
})

describe('标题校验（strip 后 1–200 字符）', () => {
  it('空白标题被拒绝', () => {
    // 保护行为：全空白标题 strip 后为空，不允许提交
    expect(validateKnowledgeTitle('   ')?.code).toBe('title-blank')
    expect(validateKnowledgeTitle('')?.code).toBe('title-blank')
  })

  it('超过 200 字符被拒绝，恰好 200 字符通过', () => {
    // 边界情况：200 字符合法，201 字符拒绝（与后端 MAX_TITLE_LENGTH 一致）
    expect(validateKnowledgeTitle('a'.repeat(201))?.code).toBe('title-too-long')
    expect(validateKnowledgeTitle('a'.repeat(KNOWLEDGE_TITLE_MAX_LENGTH))).toBeNull()
  })

  it('首尾空白在提交前不参与长度判断（strip 后校验）', () => {
    // 保护行为：两边空格不增加有效长度（服务端同样 strip 后校验）
    expect(validateKnowledgeTitle('  标题  ')).toBeNull()
  })
})

describe('新文档上传组合校验', () => {
  it('全部合法时返回 null', async () => {
    // 保护行为：合法标题 + 合法文件 + 合法 UTF-8 应通过全部校验
    const issue = await validateNewDocumentUpload({
      title: '退货指南',
      file: fileLike('文件.md', 128, utf8Bytes('# 退货指南\n正文内容')),
    })
    expect(issue).toBeNull()
  })

  it('标题错误优先于文件错误返回', async () => {
    // 保护行为：表单顺序上先暴露标题问题，避免用户先修一半
    const issue = await validateNewDocumentUpload({
      title: '  ',
      file: fileLike('a.docx', 100),
    })
    expect(issue?.code).toBe('title-blank')
  })

  it('非法 UTF-8 内容被拒绝', async () => {
    // 保护行为：二进制内容在客户端就被拦截，避免服务端 422
    const issue = await validateNewDocumentUpload({
      title: '测试',
      file: fileLike('a.md', 3, new Uint8Array([0xff, 0xfe, 0xff])),
    })
    expect(issue?.code).toBe('invalid-utf8')
  })
})

describe('新版本上传组合校验', () => {
  it('markdown 文档接受 .md 与 .markdown（大小写不敏感）', async () => {
    // 保护行为：markdown 类型的新版本文件必须是 md/markdown 扩展名
    const ok = await validateNewVersionUpload({
      fileCount: 1,
      file: fileLike('v2.MD', 64, utf8Bytes('正文')),
      documentSourceType: 'markdown',
    })
    expect(ok).toBeNull()
  })

  it('text 文档只接受 .txt，不接受 .md', async () => {
    // 保护行为：text 类型的新版本文件必须是 txt（与后端 source_type 一致）
    const wrong = await validateNewVersionUpload({
      fileCount: 1,
      file: fileLike('v2.md', 64, utf8Bytes('正文')),
      documentSourceType: 'text',
    })
    expect(wrong?.code).toBe('type-mismatch')
  })

  it('markdown 文档不接受 .txt（类型不匹配）', async () => {
    // 保护行为：类型匹配校验双向成立（md 文档传 txt 必须拒绝）
    const issue = await validateNewVersionUpload({
      fileCount: 1,
      file: fileLike('v2.txt', 64, utf8Bytes('正文')),
      documentSourceType: 'markdown',
    })
    expect(issue?.code).toBe('type-mismatch')
  })

  it('word 类型文档直接拒绝上传新版本', async () => {
    // 保护行为：word 类型不可通过 HTTP 上传新版本（入口与校验双重拦截）
    const issue = await validateNewVersionUpload({
      fileCount: 1,
      file: fileLike('v2.md', 64, utf8Bytes('正文')),
      documentSourceType: 'word',
    })
    expect(issue?.code).toBe('word-version-unsupported')
  })

  it('一次选择多个文件被拒绝', async () => {
    // 边界情况：新版本只允许单文件（multiple 选择时也需兜底拦截）
    const issue = await validateNewVersionUpload({
      fileCount: 2,
      file: fileLike('v2.md', 64, utf8Bytes('正文')),
      documentSourceType: 'markdown',
    })
    expect(issue?.code).toBe('multiple-files')
  })

  it('新版本文件同样校验空文件与 UTF-8', async () => {
    // 保护行为：新版本上传不得绕过基础校验（空文件 / 非法编码）
    expect(
      (await validateNewVersionUpload({
        fileCount: 1,
        file: fileLike('v2.txt', 0),
        documentSourceType: 'text',
      }))?.code,
    ).toBe('empty-file')
    expect(
      (await validateNewVersionUpload({
        fileCount: 1,
        file: fileLike('v2.txt', 3, new Uint8Array([0xff, 0xfe, 0xff])),
        documentSourceType: 'text',
      }))?.code,
    ).toBe('invalid-utf8')
  })
})
