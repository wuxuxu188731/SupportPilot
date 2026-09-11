/*
 * Markdown 渲染工具测试：常用语法渲染、安全边界（源码 HTML 转义、危险协议
 * 链接拦截、图片降级）、引用标记 token 化与空文本处理。
 */

import { describe, expect, it } from 'vitest'

import { renderMarkdown } from '@/utils/markdown'

describe('renderMarkdown：Markdown 语法渲染', () => {
  it('标题、列表、加粗渲染为对应 HTML 元素', () => {
    // 保护行为：Agent 回答中的 Markdown 语法必须渲染成富文本，
    // 而不是把 ### / ** 等语法符号原样展示给用户
    const html = renderMarkdown('### 退款政策\n\n- **7 天无理由**\n- 需保持商品完好')

    expect(html).toContain('<h3>退款政策</h3>')
    expect(html).toContain('<ul>')
    expect(html).toContain('<li><strong>7 天无理由</strong></li>')
    expect(html).not.toContain('###')
    expect(html).not.toContain('**')
  })

  it('行内代码与围栏代码块渲染为 code/pre', () => {
    // 保护行为：政策条款外的技术内容（如订单号格式）需要等宽代码样式
    const html = renderMarkdown('状态码 `PAID`：\n\n```\norder_id=1001\n```')

    expect(html).toContain('<code>PAID</code>')
    expect(html).toContain('<pre>')
    expect(html).toContain('order_id=1001')
  })

  it('表格渲染为 table，引用块渲染为 blockquote', () => {
    // 保护行为：售后政策常用表格对比，必须结构化展示而非纯文本堆叠
    const html = renderMarkdown('| 场景 | 期限 |\n| --- | --- |\n| 退货 | 7 天 |\n\n> 以签收时间为准')

    expect(html).toContain('<table>')
    expect(html).toContain('<th>场景</th>')
    expect(html).toContain('<td>7 天</td>')
    expect(html).toContain('<blockquote>')
  })

  it('单个换行保留为换行展示', () => {
    // 边界情况：聊天场景下模型常用单换行分隔要点，不能被合并成一行
    const html = renderMarkdown('第一行\n第二行')
    expect(html).toContain('<br>')
  })

  it('空文本渲染为空串，全空白文本同样为空串', () => {
    // 边界情况：空文本不应产生空标签，降级文案交由展示组件决定
    expect(renderMarkdown('')).toBe('')
    expect(renderMarkdown('   \n  ')).toBe('')
  })
})

describe('renderMarkdown：安全边界', () => {
  it('源码内联 HTML 被转义为纯文本', () => {
    // 安全边界：回答文本属于不可信输入，<script>/img 等标签只能作为
    // 转义后的纯文本展示，绝不允许生成真实标签（onerror 仅在文本中保留）
    const html = renderMarkdown('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>')

    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;script&gt;')
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;')
  })

  it('javascript: 协议链接被拦截，不生成可点击链接', () => {
    // 安全边界：危险协议必须被 markdown-it 的 validateLink 拦下
    const html = renderMarkdown('[点我](javascript:alert(1))')
    expect(html).not.toContain('<a ')
    expect(html).not.toContain('href="javascript:')
  })

  it('正文中的裸域名不自动变成链接', () => {
    // 边界情况：禁用了 linkify，政策正文里的域名/编号不应被误渲染成链接
    const html = renderMarkdown('详见 www.example.com 或 service@example.com')
    expect(html).not.toContain('<a ')
  })

  it('图片降级为替代文本与地址，不产生 img 标签', () => {
    // 安全边界：不加载外部图片（避免追踪像素与隐私泄露），信息以文本保留
    const html = renderMarkdown('![流程图](https://example.com/a.png)')

    expect(html).not.toContain('<img')
    expect(html).toContain('流程图')
    expect(html).toContain('https://example.com/a.png')
  })
})

describe('renderMarkdown：引用标记', () => {
  it('citations=false 时不生成引用标记元素', () => {
    // 边界情况：历史消息没有结构化 citations，不能从文本猜测生成引用标记
    const html = renderMarkdown('依据政策 [C1] 处理', { citations: false })
    expect(html).not.toContain('citation-ref')
    expect(html).toContain('[C1]')
  })

  it('citations=true 时 [C1] 渲染为可聚焦的引用标记', () => {
    // 保护行为：启用引用标记时 [C1] 必须变成可点击标记（含无障碍属性）
    const html = renderMarkdown('依据政策 [C1] 处理', { citations: true })

    expect(html).toContain('class="citation-ref"')
    expect(html).toContain('data-citation-id="C1"')
    expect(html).toContain('tabindex="0"')
    expect(html).toContain('aria-label="引用 C1，点击定位到引用卡片"')
  })

  it('小写 [c2] 归一化为大写引用标记', () => {
    // 边界情况：模型可能输出小写引用标记，仍需正确定位到 C2 引用卡片
    const html = renderMarkdown('见 [c2]', { citations: true })
    expect(html).toContain('data-citation-id="C2"')
  })

  it('加粗包裹的引用标记不破坏标签结构', () => {
    // 安全边界：引用标记在 markdown-it 行内阶段解析，
    // 因此 **结果 [C1]** 必须保持 strong 标签配对，而不是字符串切割产生残缺 HTML
    const html = renderMarkdown('**结果 [C1]**', { citations: true })

    expect(html).toContain('<strong>')
    expect(html).toContain('</strong>')
    expect((html.match(/<strong>/g) ?? []).length).toBe((html.match(/<\/strong>/g) ?? []).length)
    expect(html).toContain('data-citation-id="C1"')
  })

  it('普通 Markdown 链接不受引用标记规则影响', () => {
    // 边界情况：引用规则注册在 link 规则之前，不能抢走 [文本](url) 的解析
    const html = renderMarkdown('[退货政策](https://example.com/policy)', { citations: true })

    expect(html).toContain('href="https://example.com/policy"')
    expect(html).not.toContain('citation-ref')
  })
})
