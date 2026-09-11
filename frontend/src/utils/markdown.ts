/*
 * Markdown 渲染工具（Agent 消息正文）。
 *
 * 背景：Agent 的回答由大模型生成，通常是 Markdown 文本（标题、列表、加粗、
 * 代码块、表格等）。直接按纯文本展示会让用户看到 `**`、`###` 等语法符号，
 * 因此统一在本模块把 Markdown 文本编译为 HTML，供展示组件渲染。
 *
 * 安全约束（回答文本属于外部输入，必须按不可信数据处理）：
 *  - `html: false`：禁用源码内联 HTML，`<script>` 等标签一律转义为纯文本；
 *  - `linkify: false`：不自动把正文中的裸域名/邮箱变成链接，避免生成噪声链接；
 *  - 行内图片降级为「替代文本 + 地址」纯文本，不请求外部资源
 *    （避免追踪像素 / 隐私泄露 / 加载失败破图）；
 *  - 链接沿用 markdown-it 默认的 validateLink（拦截 javascript: 等危险协议）。
 *
 * 引用标记：回答正文中的 `[C1]` 是知识引用标记，必须保持可点击以定位到引用
 * 卡片。为避免「先渲染 HTML 再按字符串切割」破坏标签结构（可能产生 XSS 与
 * 无效 HTML），这里在 markdown-it 的**行内解析阶段**就把 `[C1]` 识别为独立
 * token，由自定义渲染规则输出专用元素，绝不参与 Markdown 链接解析。
 */

import MarkdownIt from 'markdown-it'
import type { MarkdownIt as MarkdownItInstance, RendererRule, StateInline } from 'markdown-it'

/** 引用标记 token 类型（自定义，markdown-it 内置类型中不存在）。 */
const CITATION_TOKEN = 'sp_citation_ref'

/** 与知识引用标记匹配的源码片段：`[C1]` / `[c12]`。 */
const CITATION_SOURCE_PATTERN = /^\[[Cc]\d+\]/

/** 引用标记在 token.meta 中存放实际标记文本的键名。 */
const CITATION_TEXT_KEY = 'spCitationText'

/** 是否启用正文引用标记（仅当该回答携带结构化 citations 时启用）。 */
export interface MarkdownRenderOptions {
  /** true=把正文中的 [C1] 渲染为可聚焦、可点击的引用标记元素 */
  citations?: boolean
}

/*
 * Markdown 渲染规则集合：按不同展示需求（是否需要引用标记）分别创建实例。
 * 两个实例都只在模块加载时创建一次，渲染时无额外解析器构造开销。
 */
const renderers = new Map<boolean, MarkdownItInstance>()

/**
 * `[C1]` 行内解析规则：命中时生成一个自闭合的引用标记 token。
 *
 * 该规则注册在 markdown-it 默认的 `link` 规则之前，因此 `[C1]` 不会被当成
 * “链接文本 + 未定义目标”处理；`[文本](url)` 这类真正的链接以 `](` 开头，
 * 不匹配本模式，解析行为不受影响。
 */
const citationInlineRule = (state: StateInline): boolean => {
  if (state.src.charCodeAt(state.pos) !== 0x5b /* [ */) return false
  const matched = CITATION_SOURCE_PATTERN.exec(state.src.slice(state.pos, state.posMax))
  if (matched === null) return false

  const text = matched[0]
  const token = state.push(CITATION_TOKEN, '', 0)
  token.meta = { [CITATION_TEXT_KEY]: text }
  state.pos += text.length
  return true
}

/** 渲染引用标记元素：点击/回车由展示组件的事件委托统一处理。 */
const renderCitationRef: RendererRule = (tokens, idx) => {
  const text = String(tokens[idx].meta?.[CITATION_TEXT_KEY] ?? '')
  const label = text.replace(/^\[|\]$/g, '').toUpperCase()
  return (
    `<span class="citation-ref" role="link" tabindex="0"` +
    ` data-citation-id="${label}" aria-label="引用 ${label}，点击定位到引用卡片">${text}</span>`
  )
}

/**
 * 图片渲染规则：降级为纯文本，不加载外部资源。
 *
 * 回答里的图片地址来自模型输出，属于不可信内容：既不应触发外部请求，
 * 也不应作为链接暴露；只展示替代文本与地址原文，保证信息不丢失。
 */
const renderPlainImage: RendererRule = (tokens, idx) => {
  const token = tokens[idx]
  // image 是自闭合行内 token，替代文本存放在 content 中
  const alt = token.content
  const src = String(token.attrGet('src') ?? '')
  if (src.length === 0) return escapeHtml(alt)
  if (alt.length === 0) return `（图片：${escapeHtml(src)}）`
  return `（图片：${escapeHtml(alt)}｜${escapeHtml(src)}）`
}

/** 转义 HTML 特殊字符（与 markdown-it 内置 escapeHtml 行为一致）。 */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** 创建（并按需缓存）Markdown 渲染器。 */
function getRenderer(citations: boolean): MarkdownItInstance {
  const cached = renderers.get(citations)
  if (cached) return cached

  const md = new MarkdownIt({
    // 禁用源码内联 HTML：回答中的标签一律转义为纯文本，从根上杜绝注入
    html: false,
    // 不自动识别裸链接：避免正文里的域名/编号被误渲染成链接
    linkify: false,
    // 不做排版替换（引号美化等）：保持客服条款原文精确可读
    typographer: false,
    // 单个换行按换行展示：聊天场景下用户/模型都习惯用换行分隔要点
    breaks: true,
  })

  md.renderer.rules.image = renderPlainImage
  if (citations) {
    // 注册到 link 规则之前，优先吞掉 [C1] 形式的引用标记
    md.inline.ruler.before('link', CITATION_TOKEN, citationInlineRule)
    md.renderer.rules[CITATION_TOKEN] = renderCitationRef
  }

  renderers.set(citations, md)
  return md
}

/**
 * 把 Agent 消息的 Markdown 文本渲染为 HTML 字符串。
 *
 * 输出可直接用于展示组件的 v-html：因为禁用了源码 HTML，且链接经过协议
 * 校验、图片被降级为文本，输出内容不包含来自回答文本的可执行标记。
 * 空文本返回空串，由调用方决定降级文案。
 */
export function renderMarkdown(source: string, options: MarkdownRenderOptions = {}): string {
  if (source.trim().length === 0) return ''
  return getRenderer(options.citations === true).render(source)
}
