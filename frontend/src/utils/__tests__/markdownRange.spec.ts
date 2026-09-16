/*
 * 偏移定位与高亮工具测试（jsdom 单测，直接操作 DOM，不挂载 Vue 组件）。
 *
 * 覆盖设计稿 B2 的六条验收标准：
 *  1. 单段落内偏移 → 高亮文本内容与位置正确；
 *  2. 跨段落 / 跨列表项 → 分段高亮且 DOM 结构未被破坏；
 *  3. 偏移落在 `**加粗**`、`` `代码` `` 等标记内部 → 不抛异常、走降级；
 *  4. 越界与 null 输入 → 返回"需降级"信号，不抛异常；
 *  5. 重复文本场景 → 命中的是正确那一次（不得用文本搜索定位）；
 *  6. 高亮后 clearHighlight 能把 DOM 还原（outerHTML 比对）。
 */

import { describe, expect, it } from 'vitest'

import {
  RANGE_HIGHLIGHT_CLASS,
  charOffsetToLine,
  clearHighlight,
  locateRangeByOffsets,
} from '@/utils/markdownRange'
import { renderMarkdown } from '@/utils/markdown'

/** 新建一个已渲染正文的容器：模拟查看器里"Markdown 渲染 + 偏移定位"的真实场景。 */
function mountSource(source: string): HTMLElement {
  const container = document.createElement('div')
  container.innerHTML = renderMarkdown(source)
  document.body.appendChild(container)
  return container
}

/** 取容器内的高亮元素（没有则返回 null）。 */
function highlightOf(container: Element): HTMLElement | null {
  return container.querySelector<HTMLElement>(`.${RANGE_HIGHLIGHT_CLASS}`)
}

/** 取容器内全部高亮元素。 */
function highlightsOf(container: Element): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(`.${RANGE_HIGHLIGHT_CLASS}`))
}

/** 断言定位成功并返回高亮元素。 */
function expectLocated(container: Element, source: string, start: number, end: number): HTMLElement {
  const result = locateRangeByOffsets(container, source, start, end)
  expect(result.ok).toBe(true)
  const highlight = highlightOf(container)
  expect(highlight).not.toBeNull()
  return highlight as HTMLElement
}

describe('locateRangeByOffsets：单段落内的精确高亮', () => {
  it('偏移落在同一段落内时，高亮文本与源码区间一致且位置正确', () => {
    // 保护行为：引用片段必须能精确框出对应文字，而不是整段高亮或定位到别处
    const source = '退货政策：签收后 7 日内可申请退货，超期不再受理。'
    const container = mountSource(source)
    const start = source.indexOf('签收后')
    const end = source.indexOf('，超期')

    const highlight = expectLocated(container, source, start, end)

    expect(highlight.textContent).toBe('签收后 7 日内可申请退货')
    // 位置正确：高亮前面的文本 = 源码区间之前的文本
    expect(container.textContent).toContain(source)
    expect(highlight.previousSibling?.textContent).toBe('退货政策：')
    expect(highlight.nextSibling?.textContent).toBe('，超期不再受理。')
  })

  it('start_offset 为 0（片段在正文开头）必须被当作合法偏移', () => {
    // 边界情况：偏移 0 是合法值，实现里不得用 !start_offset 之类的取反判断
    const source = '签收后 7 日内可申请退货。'
    const container = mountSource(source)

    const highlight = expectLocated(container, source, 0, source.indexOf('。'))

    expect(highlight.textContent).toBe('签收后 7 日内可申请退货')
  })

  it('片段横跨加粗与行内代码结构时仍能正确定位', () => {
    // 边界情况：切块可能从普通文字开始、跨过 **加粗** 与 `代码` 结束；
    // 高亮出来的可见内容必须与源码区间一致（源码里的标记在渲染后已被剥离）
    const source = '退货政策 **签收后 7 日内可申请退货。** 状态码 `PAID` 表示已支付。'
    const container = mountSource(source)
    const start = source.indexOf('政策')
    const end = source.indexOf('已支付') + '已支付。'.length

    const highlight = expectLocated(container, source, start, end)

    // 去掉源码里的标记与空白后，应与高亮内容逐字一致
    expect(highlight.textContent?.replace(/\s+/g, '')).toBe(
      source.slice(start, end).replace(/[*`\s]/g, ''),
    )
    expect(highlight.textContent).toContain('状态码 PAID 表示已支付')
  })

  it('偏移落在被剥离的标记内部时降级返回，不抛异常也不高亮错位置', () => {
    // 边界情况：偏移正好指在 `**` 上（源码与渲染文本无法对齐），
    // 必须返回 ok:false 让调用方降级，绝不能高亮一段无关文字
    const source = '退货政策 **签收后 7 日内可申请退货。**'
    const container = mountSource(source)
    const start = source.indexOf('**') + 1
    const end = start + 6

    const result = locateRangeByOffsets(container, source, start, end)

    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toBe('unaligned')
    expect(highlightOf(container)).toBeNull()
    // 降级路径不得破坏正文
    expect(container.textContent).toContain('退货政策 签收后 7 日内可申请退货。')
  })
})

describe('locateRangeByOffsets：跨结构与边界输入', () => {
  it('偏移横跨两个段落时高亮完整文本，DOM 结构不被破坏', () => {
    // 保护行为：段间空白在渲染后数量与源码不同（`\n\n` 变成 `\n`），
    // 跨越段落仍须定位到正确区间，且不得改变段落结构或丢失文字
    const source = '第一段：受理范围。\n\n第二段：所需材料。'
    const container = mountSource(source)
    const start = source.indexOf('范围')
    const end = source.indexOf('所需') + 2

    const highlight = expectLocated(container, source, start, end)

    // 高亮的可见内容与源码区间只差一个段间空行
    expect(highlight.textContent?.replace(/\s+/g, '')).toBe(
      source.slice(start, end).replace(/\s+/g, ''),
    )
    // 其余文本不变，且段落结构仍然存在
    expect(container.querySelectorAll('p').length).toBeGreaterThanOrEqual(2)
    expect(container.textContent?.replace(/\s+/g, '')).toBe(source.replace(/\s+/g, ''))
  })

  it('偏移横跨两个列表项时高亮完整文本，列表结构保留', () => {
    // 边界情况：列表标记（- ）在渲染后被剥离，高亮区间仍须覆盖正确的两个条目；
    // 高亮出来的可见内容不含被剥离的列表标记
    const source = '- 七天无理由\n- 需保持商品完好'
    const container = mountSource(source)
    const start = source.indexOf('无理由')
    const end = source.indexOf('完好') + 2

    const highlight = expectLocated(container, source, start, end)

    expect(highlight.textContent?.replace(/\s+/g, '')).toBe('无理由需保持商品完好')
    // 高亮后列表结构保留（第二个条目可能被高亮元素包住，但仍是 li）
    expect(container.querySelectorAll('li').length).toBeGreaterThanOrEqual(2)
    expect(container.textContent?.replace(/\s+/g, '')).toBe('七天无理由需保持商品完好')
  })

  it('end 超出正文长度时返回 out-of-range 而不抛异常', () => {
    // 边界情况：偏移越界说明正文与偏移不同源，必须降级而不是猜位置
    const source = '签收后 7 日内可申请退货。'
    const container = mountSource(source)

    const result = locateRangeByOffsets(container, source, 0, source.length + 10)

    expect(result).toEqual({ ok: false, reason: 'out-of-range' })
    expect(highlightOf(container)).toBeNull()
  })

  it('start 大于 end 或为负数时返回 invalid-input', () => {
    // 边界情况：上下界颠倒、负数偏移均为非法输入
    const source = '签收后 7 日内可申请退货。'
    const container = mountSource(source)

    expect(locateRangeByOffsets(container, source, 8, 3)).toEqual({
      ok: false,
      reason: 'invalid-input',
    })
    expect(locateRangeByOffsets(container, source, -1, 5)).toEqual({
      ok: false,
      reason: 'invalid-input',
    })
  })

  it('偏移为 null（或 undefined）时按非法输入降级，不抛异常', () => {
    // 边界情况：后端允许偏移为空（决策 2），组件把 null 传进来也必须安全
    const source = '签收后 7 日内可申请退货。'
    const container = mountSource(source)

    const withNull = locateRangeByOffsets(
      container,
      source,
      null as unknown as number,
      null as unknown as number,
    )
    const withUndefined = locateRangeByOffsets(
      container,
      source,
      undefined as unknown as number,
      undefined as unknown as number,
    )

    expect(withNull.ok).toBe(false)
    expect(withUndefined.ok).toBe(false)
  })

  it('容器为空（正文渲染结果为空）时返回 empty-container', () => {
    // 边界情况：未渲染正文时不能凭空定位，返回降级信号
    const container = document.createElement('div')
    document.body.appendChild(container)

    expect(locateRangeByOffsets(container, '签收后 7 日内可申请退货。', 0, 5)).toEqual({
      ok: false,
      reason: 'empty-container',
    })
  })
})

describe('locateRangeByOffsets：重复文本命中正确的那一次', () => {
  it('同一句话出现两次时，命中的是偏移所指的那一次', () => {
    // 保护行为：不得用 find/indexOf 之类的文本搜索定位——
    // 重复文本会命中错误位置，偏移必须单调推进到正确的那一段
    const source = '重复句子。\n\n其它内容。\n\n重复句子。'
    const secondStart = source.lastIndexOf('重复句子')
    const container = mountSource(source)

    const result = locateRangeByOffsets(
      container,
      source,
      secondStart,
      secondStart + '重复句子'.length,
    )
    expect(result.ok).toBe(true)

    const highlights = highlightsOf(container)
    expect(highlights).toHaveLength(1)
    // 命中的必须是最后一次出现：其前面应包含"其它内容"
    const highlight = highlights[0]
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT)
    let before = ''
    let cursor = walker.nextNode()
    while (cursor !== null && !highlight.contains(cursor)) {
      before += cursor.nodeValue ?? ''
      cursor = walker.nextNode()
    }
    expect(before).toContain('其它内容')
  })

  it('连续定位两条不同引用时只保留最后一次高亮', () => {
    // 保护行为：相邻块正文有 80 token 重叠，重复定位必须自动清掉上一次高亮，
    // 否则两条引用的高亮会互相叠加
    const source = '第一句内容。第二句内容。第三句内容。'
    const container = mountSource(source)

    expectLocated(container, source, 0, 5)
    expect(highlightsOf(container)).toHaveLength(1)

    const second = source.indexOf('第二句')
    const highlight = expectLocated(container, source, second, second + 5)

    expect(highlightsOf(container)).toHaveLength(1)
    expect(highlight.textContent).toBe('第二句内容')
  })
})

describe('clearHighlight：DOM 还原', () => {
  it('高亮后 clearHighlight 能把 DOM 还原到高亮前（outerHTML 比对）', () => {
    // 保护行为：查看器会在切换引用时反复"清空 → 重新高亮"，
    // 因此清理必须无损：结构、文本与高亮前逐字一致
    const source = '# 退货总则\n\n- **七天无理由**\n- 需保持商品完好\n\n> 以签收时间为准'
    const container = document.createElement('div')
    container.innerHTML = renderMarkdown(source)
    document.body.appendChild(container)
    const before = container.outerHTML
    const start = source.indexOf('七天')
    const end = source.indexOf('无理由') + 3

    expectLocated(container, source, start, end)
    expect(highlightOf(container)).not.toBeNull()

    clearHighlight(container)

    expect(highlightOf(container)).toBeNull()
    expect(container.outerHTML).toBe(before)
  })

  it('clearHighlight 幂等：没有高亮时重复调用不改变 DOM', () => {
    // 边界情况：关闭面板/切换引用可能重复触发清理，重复调用必须无副作用
    const source = '签收后 7 日内可申请退货。'
    const container = mountSource(source)
    const before = container.outerHTML

    clearHighlight(container)
    clearHighlight(container)

    expect(container.outerHTML).toBe(before)
  })

  it('跨段落高亮后清理同样能精确还原结构', () => {
    // 边界情况：跨越 <p> 的高亮会把节点搬到不同父节点，还原逻辑必须覆盖该分支
    const source = '第一段：受理范围。\n\n第二段：所需材料。'
    const container = mountSource(source)
    const before = container.outerHTML
    const start = source.indexOf('范围')
    const end = source.indexOf('所需') + 2

    expectLocated(container, source, start, end)
    clearHighlight(container)

    expect(container.outerHTML).toBe(before)
  })
})

describe('charOffsetToLine：偏移换算行号', () => {
  it('按换行符把字符偏移换算成 1 起的行号', () => {
    // 保护行为：诊断信息与"块级降级"需要知道偏移落在第几行
    const text = '第一行\n第二行\n第三行'

    expect(charOffsetToLine(text, 0)).toBe(1)
    expect(charOffsetToLine(text, 3)).toBe(1)
    expect(charOffsetToLine(text, 4)).toBe(2)
    expect(charOffsetToLine(text, 8)).toBe(3)
  })

  it('越界或非法偏移收敛到首行/末行，不抛异常', () => {
    // 边界情况：负数偏移按首行处理，超出长度的偏移按末行处理
    const text = '第一行\n第二行'

    expect(charOffsetToLine(text, -5)).toBe(1)
    expect(charOffsetToLine(text, Number.NaN)).toBe(1)
    expect(charOffsetToLine(text, 999)).toBe(2)
  })
})
