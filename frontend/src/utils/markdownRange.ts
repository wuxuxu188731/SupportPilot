/*
 * 正文偏移定位与高亮（纯 DOM 工具，不依赖 Vue，可单独单测）。
 *
 * 背景：知识库引用（Citation）自带 `start_offset`/`end_offset`，它们是**入库时那份
 * 归一化 Markdown 正文（raw_text）上的字符偏移**；而屏幕上显示的是这份 Markdown
 * 经 markdown-it 渲染后的 DOM。要把「源码字符偏移」变成「DOM 里的高亮区间」，
 * 必须先建立两者之间的逐字符对齐关系。
 *
 * 为什么不能用「纯文本搜索」定位（如 indexOf / find）：
 *  - 同一句话在正文里可能出现多次，文本搜索无法保证命中正确的那一次；
 *  - 引用正文里的片段是源码文本，渲染后 `**`、`` ` ``、`#` 等标记已被剥离，
 *    直接用源码片段去搜渲染文本会搜不到。
 *
 * 对齐算法（严格从前向后、单调推进，不做模糊搜索）：
 *  1. 从容器收集渲染后的可见文本（DOM 的 textContent 已解码 `&amp;` 等实体）；
 *  2. 让源码指针与渲染指针同步前进：字符相同则各自前进一格；不同则说明源码这里是
 *     Markdown 标记（`**`、`` ` ``、`](url)`、行首 `#`/`-`/`>` 等），跳过标记重试；
 *     `\r\n`/`\r` 与 `\n` 视为同一字符；连续空白视为一个字符（markdown-it 会折叠）；
 *  3. 源码指针走到 `start` 时记录对应的渲染位置，走到 `end` 时记录结束位置，
 *     于是得到渲染文本上的 `[renderedStart, renderedEnd)`；
 *  4. 在渲染文本的这两个位置上各插入一个零宽标记元素（文本节点就地切开，切分点
 *     不会落进标记元素内部），再用一个 `Range` 把两个标记之间的内容取出并包进
 *     `<span class="sp-range-highlight">`，最后移除两个标记。
 *
 * 定位失败（`ok:false`）的常见原因：越界（`out-of-range`）、入参非法
 * （`invalid-input`）、正文为空（`empty-container`）、以及对齐后与源码区间不符
 * （`unaligned`，例如偏移正好落在被剥离的 `**` 标记内部）。这些情况**不抛异常**，
 * 由调用方降级为「高亮所属块元素」或「只打开文档」。
 *
 * 可逆性：标记切分与内容搬移全程留痕（原始文本节点、原文、切出的后继节点、区间涉及
 * 的父节点）；`clearHighlight` 据此原地还原，可把容器恢复成高亮前的结构
 * （用 outerHTML 逐字比对可通过）。因此可以安全地反复「清空 → 重新高亮」，
 * 也能处理相邻块重叠（`CHUNK_OVERLAP_TOKENS = 80`）导致的区间重叠。
 */

/** 高亮元素的类名（查看器与测试都用它定位高亮）。 */
export const RANGE_HIGHLIGHT_CLASS = 'sp-range-highlight'

/** 用于切分文本节点的零宽标记元素名（TAG 名必须小写）。 */
const RANGE_MARKER_TAG = 'sp-range-marker'

/** 定位失败的原因码：供调用方决定降级策略与诊断输出。 */
export type LocateFailureReason =
  /** start/end 不是合法有限数，或 start < 0 / end < start */
  | 'invalid-input'
  /** end 超出正文长度 */
  | 'out-of-range'
  /** 容器没有可定位的文本（空正文或渲染结果为空） */
  | 'empty-container'
  /** 源码与渲染文本无法对齐，或对齐后与引用正文不符 */
  | 'unaligned'

/** 定位结果：ok=false 时只带失败原因，调用方走降级路径。 */
export type LocateRangeResult =
  | {
      /** 定位成功：区间已包裹并可见 */
      ok: true
      /** 命中的 DOM 区间（已包含在高亮元素内），供滚动定位 */
      range: Range
      /** 高亮元素的可见文本（与引用正文一致，仅空白差异） */
      text: string
      /** 渲染文本中的起点/终点偏移（不含结尾），仅供诊断 */
      renderedStart: number
      renderedEnd: number
    }
  | {
      /** 定位失败：调用方降级，**不要**当作异常处理 */
      ok: false
      /** 失败原因码 */
      reason: LocateFailureReason
    }

/** 一次定位的全部改动记录：清空时回放即可精确还原 DOM。 */
interface RangeRecording {
  /** 高亮元素（还原时把它删掉） */
  span: HTMLElement
  /** 装在被搬内容的 DocumentFragment：还原时摘掉它，"仍在高亮里"的节点随之消失 */
  payload: DocumentFragment
  /** 被搬进高亮元素的节点（按文档顺序，含元素与其后代） */
  order: Node[]
  /** 被搬节点 → 它原来的父节点 */
  owners: Map<Node, Node>
  /** 被搬节点 → 它高亮前在父节点中的下标（还原时按下标插回原位） */
  indexes: Map<Node, number>
  /** 被切分过的文本节点：原始节点、原文、以及切出的后继节点 */
  splits: Array<{ node: Text; original: string; created: Text[] }>
}

const recordings = new WeakMap<Element, RangeRecording[]>()

/**
 * 计算某个源码偏移所在的行号（1 起，便于与「标题在第几行」这类人工核对对齐）。
 *
 * 换行只按 `\n` 计数：入库时已把 `\r\n`/`\r` 统一成 `\n`，偏移口径也是同一份文本。
 * 偏移超界或非有限数时收敛到首行/末行，不抛异常。
 */
export function charOffsetToLine(text: string, offset: number): number {
  if (!Number.isFinite(offset) || offset <= 0) return 1
  const limit = Math.min(Math.floor(offset), text.length)
  let line = 1
  for (let index = 0; index < limit; index += 1) {
    if (text[index] === '\n') line += 1
  }
  return line
}

/**
 * 把源码偏移定位到渲染后的元素（用于目录跳转：滚动到某个标题）。
 *
 * 与 :func:`locateRangeByOffsets` 共用同一套对齐逻辑，但**不**修改 DOM：
 * 只返回该字符位置所在的元素，调用方自行 `scrollIntoView`。
 * 偏移越界、对不齐或容器为空时返回 null。
 */
export function locateElementByOffset(
  container: Element,
  source: string,
  offset: number,
): Element | null {
  if (!Number.isFinite(offset) || offset < 0 || offset > source.length) return null
  const target = Math.floor(offset)
  const { nodes, text: renderedText } = collectRenderedText(container)
  if (nodes.length === 0 || renderedText.length === 0) return null

  const map = buildAlignment(source, renderedText)
  const rendered = map[target]
  if (rendered === undefined || rendered < 0) return null
  const point = locatePoint(nodes, Math.min(rendered, renderedText.length - 1))
  if (point === null) return null

  // 文本节点本身不可滚动，取它所在的元素
  const parent = point.node.parentElement
  if (parent !== null) return parent
  return point.node.parentNode instanceof Element ? point.node.parentNode : container
}

/** 收集容器内全部文本节点（文档顺序），并拼出渲染后的可见文本。 */
function collectRenderedText(container: Element): { nodes: Text[]; text: string } {
  const nodes: Text[] = []
  const parts: string[] = []
  for (const child of Array.from(container.childNodes)) {
    collectFromNode(child, nodes, parts)
  }
  return { nodes, text: parts.join('') }
}

/** 递归收集子树中的文本节点。 */
function collectFromNode(node: Node, nodes: Text[], parts: string[]): void {
  if (node.nodeType === Node.TEXT_NODE) {
    nodes.push(node as Text)
    parts.push(node.nodeValue ?? '')
    return
  }
  for (const child of Array.from(node.childNodes)) {
    collectFromNode(child, nodes, parts)
  }
}

/** 跳过源码中的一个 Markdown 链接目标 `](url)`；不匹配时返回 0。 */
function skipLinkDestination(source: string, index: number): number {
  if (source[index] !== ']' || source[index + 1] !== '(') return 0
  let cursor = index + 2
  while (cursor < source.length && source[cursor] !== ')') cursor += 1
  if (cursor >= source.length) return 0
  return cursor + 1 - index
}

/**
 * 跳过源码在 `index` 处开始的一段 Markdown 标记（返回跳过的字符数，0 表示这里不是标记）。
 *
 * 覆盖：行首块级标记（`#` 标题、`-`/`*`/`+` 列表、`>` 引用、`1.` 有序列表）、
 * 行内标记串（`**`、`` ` ``、`~~`、`_`）、以及 `\` 转义的反斜杠。
 * 这些字符在渲染文本里都不存在，必须整段跳过再继续对齐。
 *
 * 注意：**只跳过标记字符本身，不吞掉后面的空格**。块级元素之间在渲染文本里会多出
 * 换行（如 `</li>\n<li>`），若这里把空格也吃掉，源码与渲染文本就会在列表/标题
 * 首行整体错位；空格留给空白规则处理，且那里的前瞻会保留这类块分隔符。
 */
function skipMarkerRun(source: string, index: number): number {
  const char = source[index]
  // `[` 只可能是链接文字起点（渲染后方括号内容保留），交给普通字符比较
  if (char === '[') return 0

  if (char === '\\') return 1
  if (char === '`' || char === '*' || char === '_' || char === '~') {
    let cursor = index
    while (cursor < source.length && source[cursor] === char) cursor += 1
    return cursor - index
  }

  const isLineStart = index === 0 || source[index - 1] === '\n'
  if (!isLineStart) return 0

  if (char === '#') {
    let cursor = index
    while (source[cursor] === '#') cursor += 1
    return cursor - index
  }
  if (char === '>' || char === '-' || char === '+') {
    const after = source[index + 1]
    if (after !== ' ' && after !== '\t') return 0
    return 1
  }
  // 有序列表标记 `1. ` / `12. `
  if (char >= '0' && char <= '9') {
    let cursor = index
    while (source[cursor] >= '0' && source[cursor] <= '9') cursor += 1
    if (source[cursor] !== '.') return 0
    cursor += 1
    if (source[cursor] !== ' ' && source[cursor] !== '\t') return 0
    return cursor - index
  }

  return 0
}

/**
 * 源码偏移 → 渲染文本偏移的对齐表。
 *
 * 返回数组长度为 `source.length + 1`，第 `i` 项表示「源码前 i 个字符」对应的渲染
 * 位置；未能对齐到的位置为 -1（调用方据此降级）。
 */
export function buildAlignment(source: string, rendered: string): number[] {
  const map = new Array<number>(source.length + 1).fill(-1)
  map[0] = 0
  let renderedIndex = 0
  let sourceIndex = 0

  while (sourceIndex < source.length && renderedIndex < rendered.length) {
    const sourceChar = source[sourceIndex] === '\r' ? '\n' : source[sourceIndex]
    const renderedChar = rendered[renderedIndex]

    // 1) 字符直接相等：各自前进一格
    if (sourceChar === renderedChar) {
      sourceIndex += 1
      renderedIndex += 1
      map[sourceIndex] = renderedIndex
      continue
    }

    // 2) Markdown 标记必须先处理：`- 条目` 的 `- ` 在渲染文本里整体消失，
    //    若先按空白跳过就会把源码与渲染文本错开（列表场景必现）
    const linkSkip = skipLinkDestination(source, sourceIndex)
    if (linkSkip > 0) {
      sourceIndex += linkSkip
      map[sourceIndex] = renderedIndex
      continue
    }
    const markerSkip = skipMarkerRun(source, sourceIndex)
    if (markerSkip > 0) {
      sourceIndex += markerSkip
      map[sourceIndex] = renderedIndex
      continue
    }

    // 3) 源码空白：渲染文本里可能整体消失（行首缩进、标记后的空格），
    //    也可能是块级元素之间多出来的换行。只有在"渲染侧的空白紧跟的内容与源码一致"
    //    时才吞掉它；否则保留为渲染文本里的真实字符（块分隔符），避免整体错位
    if (/\s/.test(sourceChar)) {
      while (sourceIndex < source.length && /\s/.test(source[sourceIndex])) sourceIndex += 1
      map[sourceIndex] = renderedIndex
      let probe = renderedIndex
      while (probe < rendered.length && /\s/.test(rendered[probe])) probe += 1
      if (probe === renderedIndex || source[sourceIndex] === rendered[probe]) {
        renderedIndex = probe
        map[sourceIndex] = renderedIndex
      }
      continue
    }

    // 4) 未知差异：保守地跳过源码一个字符，避免整体对齐卡死。
    //    即便这里判断错了，末尾的「高亮文本 vs 源码区间」自检也会兜住并降级。
    sourceIndex += 1
    map[sourceIndex] = renderedIndex
  }

  return map
}

/** 把渲染文本的字符偏移换算成「文本节点 + 节点内偏移」。 */
function locatePoint(nodes: Text[], offset: number): { node: Text; offset: number } | null {
  let remaining = offset
  for (const node of nodes) {
    const length = node.nodeValue?.length ?? 0
    if (remaining <= length) return { node, offset: remaining }
    remaining -= length
  }
  return null
}

/**
 * 用两个 `Range` 在区间两端插入零宽标记元素。
 *
 * 为什么必须用 Range：直接对文本节点 `splitText` 会让另一个端点的「节点 + 偏移」
 * 记录立刻失效（同一个文本节点被切成两段后，第二个端点的偏移基准已经变了）。
 * Range 的位置会随 DOM 变更自动调整，因此先给两端各建一个折叠 Range，
 * 再插入标记就不会互相破坏。
 */
function insertRangeMarkers(
  startPoint: { node: Text; offset: number },
  endPoint: { node: Text; offset: number },
): { startMarker: Element; endMarker: Element } | null {
  const document = startPoint.node.ownerDocument
  const startRange = document.createRange()
  startRange.setStart(startPoint.node, startPoint.offset)
  startRange.collapse(true)
  const endRange = document.createRange()
  endRange.setStart(endPoint.node, endPoint.offset)
  endRange.collapse(true)

  const startMarker = document.createElement(RANGE_MARKER_TAG)
  startRange.insertNode(startMarker)
  // 起点标记插入后，终点 Range 会自动跟随位移，仍然指向正确的字符位置
  const endMarker = document.createElement(RANGE_MARKER_TAG)
  endRange.insertNode(endMarker)
  if (startMarker === endMarker) return null
  return { startMarker, endMarker }
}

/**
 * 把两个标记之间的内容搬进高亮元素。
 *
 * 用 `Range.extractContents` 而不是手动搬兄弟节点：前者天然支持区间跨越
 * `<strong>` / `<li>` / `<p>` 等结构边界的情形（切块可能横跨这些结构），
 * 且当区间端点正好落在标记元素边界时不会额外切分文本节点。
 */
function wrapBetweenMarkers(
  startMarker: Element,
  endMarker: Element,
  recording: RangeRecording,
): HTMLElement {
  const document = startMarker.ownerDocument
  const span = document.createElement('span')
  span.className = RANGE_HIGHLIGHT_CLASS
  // 供调用方与测试定位高亮
  span.setAttribute('data-test', 'range-highlight')

  const range = document.createRange()
  range.setStartAfter(startMarker)
  range.setEndBefore(endMarker)

  // 1) 先记录区间覆盖到的**真实**节点（含元素：跨段落的区间会搬走整个 `<p>`），
  //    一旦搬移就无法再追溯"它原来挂在谁下面、后面是谁"，必须提前取样
  for (const node of descendantsBetween(startMarker, endMarker)) {
    const owner = node.parentNode
    if (owner === null) continue
    recording.order.push(node)
    recording.owners.set(node, owner)
    recording.indexes.set(node, Array.prototype.indexOf.call(owner.childNodes, node))
    if (node.nodeType !== Node.TEXT_NODE) continue
    const next = node.nextSibling
    recording.splits.push({
      node: node as Text,
      original: node.nodeValue ?? '',
      created: next !== null && next.nodeType === Node.TEXT_NODE ? [next as Text] : [],
    })
  }

  // 2) 把区间内容搬进高亮元素，并移除两个零宽标记。
  //    插入位置必须在移除标记之前取好：移除后 nextSibling 就指向了别处。
  const insertParent = endMarker.parentNode
  const insertAnchor = endMarker.nextSibling
  const fragment = range.extractContents()
  span.appendChild(fragment)
  insertParent?.insertBefore(span, insertAnchor)
  startMarker.remove()
  endMarker.remove()
  recording.span = span

  // 3) 把搬进高亮元素的内容整体塞进一个 DocumentFragment：它是"还在高亮里"的
  //    唯一标志（跨段落的区间会搬走整块 `<p>`，无法靠节点的父节点判断归属），
  //    还原时直接摘掉它即可，文本节点仍留在它内部，不会打断 splitText 的兄弟关系
  const payload = document.createDocumentFragment()
  while (span.firstChild) payload.appendChild(span.firstChild)
  span.appendChild(payload)
  recording.payload = payload
  return span
}

/**
 * 收集两个标记之间（不含标记本身）按文档顺序排列的**全部节点**。
 *
 * `Range.extractContents()` 会把区间内的每个节点搬进高亮元素，这些节点可能只是
 * 被搬动的一小段文本，也可能是整个 `<p>` / `<li>`（其文本节点是它的后代），
 * 因此必须连元素带后代一起记录，还原时才不会改变结构层级。
 */
function descendantsBetween(startMarker: Element, endMarker: Element): Node[] {
  const root = startMarker.ownerDocument
  const walker = root.createTreeWalker(root.body, NodeFilter.SHOW_ALL)
  const found: Node[] = []
  let inside = false
  let cursor = walker.nextNode()
  while (cursor !== null) {
    if (cursor === startMarker) {
      inside = true
    } else if (cursor === endMarker) {
      break
    } else if (inside) {
      found.push(cursor)
    }
    cursor = walker.nextNode()
  }
  return found
}

/**
 * 清除容器内的全部高亮，并把 DOM 还原到高亮前的结构。
 *
 * 幂等：没有高亮时调用无副作用。定位新引用前会自动调用本函数，
 * 避免多条引用（相邻块之间有 80 token 重叠）的高亮互相叠加。
 */
export function clearHighlight(container: Element): void {
  const list = recordings.get(container)
  if (list === undefined || list.length === 0) return

  // 逆序回放：后做的高亮先还原
  for (const recording of [...list].reverse()) {
    restoreRecording(recording)
  }
  recordings.delete(container)
}

/**
 * 回放一次定位的全部改动，把 DOM 还原成高亮前的结构。
 *
 * 三步：
 *  1. 把每个「被搬走的节点」按记录的原父节点与原下标插回原位
 *     （`insertBefore` 会把它从当前父节点摘走，所以无需先做删除）；
 *  2. 删掉空壳：高亮元素，以及仍留在高亮元素里、没有归属的节点；
 *  3. 把被切分的文本节点按「原文 + 切出的后继节点」拼回，并删掉多余节点。
 *
 * 父节点按深度从深到浅处理：先还原里层再还原外层，外层搬动就不会打断里层定位。
 * 因为严格按下标复原，"跨 `<p>` 的区间"（整块 `<p>` 被搬进高亮元素）也能精确还原。
 */
function restoreRecording(recording: RangeRecording): void {
  const grouped = new Map<Node, Array<{ node: Node; index: number }>>()
  for (const node of recording.order) {
    const owner = recording.owners.get(node)
    const index = recording.indexes.get(node)
    if (owner === undefined || index === undefined) continue
    const group = grouped.get(owner)
    if (group === undefined) grouped.set(owner, [{ node, index }])
    else group.push({ node, index })
  }

  // 1) 按原下标插回原位：下标是"高亮前父节点的第几个子节点"。插入会让后面的下标
  //    右移，因此**从小到大**处理——先插靠前的，靠后的下标恰好被前面的插入顶到正确
  //    位置；若从大到小处理，后插的节点会落到前一个节点的前面
  const entries = [...grouped.entries()].sort(
    (left, right) => depthOf(right[0]) - depthOf(left[0]),
  )
  for (const [owner, moved] of entries) {
    for (const entry of [...moved].sort((left, right) => left.index - right.index)) {
      const reference = owner.childNodes[entry.index] ?? null
      owner.insertBefore(entry.node, reference)
    }
  }

  // 2) 删掉空壳：高亮元素与仍装在里面（未被还原）的内容
  if (recording.payload.parentNode !== null) {
    recording.payload.parentNode.removeChild(recording.payload)
  }
  recording.span.remove()

  // 3) 文本节点：把本次切出的后继节点并回原节点
  for (const split of recording.splits) {
    if (split.node.parentNode === null) continue
    const extras = split.created.filter((node) => node.parentNode !== null)
    if (extras.length === 0) continue
    split.node.nodeValue = split.original + extras.map((node) => node.nodeValue ?? '').join('')
    for (const node of extras) node.remove()
  }
}

/** 计算节点深度（body 为 0），用于把父节点按"从深到浅"排序。 */
function depthOf(node: Node): number {
  let depth = 0
  let cursor: Node | null = node
  while (cursor.parentNode !== null) {
    depth += 1
    cursor = cursor.parentNode
  }
  return depth
}

/**
 * 在渲染后的正文容器里定位并高亮源码偏移 `[start, end)`。
 *
 * - 自动先清理上一次高亮（可安全重复调用）；
 * - 输入非法/越界/对不齐时返回 `{ok:false, reason}`，**不抛异常**；
 * - 成功时返回高亮元素的 `Range`，调用方可用它做 `scrollIntoView`。
 */
export function locateRangeByOffsets(
  container: Element,
  source: string,
  start: number,
  end: number,
): LocateRangeResult {
  // 1) 入参自检：偏移必须是非负有限数，且 end 不小于 start
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end < start) {
    return { ok: false, reason: 'invalid-input' }
  }
  const from = Math.floor(start)
  const to = Math.floor(end)
  // 越界同样降级：正文与偏移同源，出现越界说明两边对不上，不能猜
  if (to > source.length) {
    return { ok: false, reason: 'out-of-range' }
  }

  clearHighlight(container)

  const { nodes, text: renderedText } = collectRenderedText(container)
  if (nodes.length === 0 || renderedText.length === 0) {
    return { ok: false, reason: 'empty-container' }
  }

  const map = buildAlignment(source, renderedText)
  const renderedStart = map[from]
  const renderedEnd = map[to]
  // 对齐表未覆盖（源码尾部未匹配）或区间塌陷成空：降级
  if (renderedStart < 0 || renderedEnd < 0 || renderedEnd <= renderedStart) {
    return { ok: false, reason: 'unaligned' }
  }

  const startPoint = locatePoint(nodes, renderedStart)
  const endPoint = locatePoint(nodes, renderedEnd)
  if (startPoint === null || endPoint === null) {
    return { ok: false, reason: 'unaligned' }
  }

  const expected = source.slice(from, to)
  const recording: RangeRecording = {
    span: document.createElement('span'),
    payload: document.createDocumentFragment(),
    order: [],
    owners: new Map(),
    indexes: new Map(),
    splits: [],
  }
  const markers = insertRangeMarkers(startPoint, endPoint)
  if (markers === null) {
    return { ok: false, reason: 'unaligned' }
  }
  const span = wrapBetweenMarkers(markers.startMarker, markers.endMarker, recording)

  // 2) 自检：高亮出来的文本必须与源码区间一致（只忽略空白差异）。
  //    不一致说明对齐把偏移指到了被剥离的标记内部，回滚并降级，绝不高亮错位置。
  if (!isSameText(span.textContent ?? '', expected)) {
    const list = recordings.get(container)
    if (list === undefined) recordings.set(container, [recording])
    else list.push(recording)
    clearHighlight(container)
    return { ok: false, reason: 'unaligned' }
  }

  const list = recordings.get(container)
  if (list === undefined) recordings.set(container, [recording])
  else list.push(recording)

  return {
    ok: true,
    range: rangeOf(span),
    text: span.textContent ?? '',
    renderedStart,
    renderedEnd,
  }
}

/** 高亮元素的区间：供调用方滚动定位。 */
function rangeOf(span: HTMLElement): Range {
  const range = span.ownerDocument.createRange()
  range.selectNodeContents(span)
  return range
}

/** 比较两段文本是否等价（忽略空白与 Markdown 标记差异）。 */
function isSameText(left: string, right: string): boolean {
  if (collapse(left) === collapse(right)) return true
  // 区间跨越块边界时（如从列表项 1 跨到列表项 2），源码里的 `- `、`# ` 等标记
  // 在渲染文本里并不存在，因此再按"去掉标记 + 去掉空白"比较一次
  return normalizeContent(left) === normalizeContent(right)
}

/** 去掉全部空白：渲染会在块级元素之间插入源码里没有的换行（如 `</li>\n<li>`）。 */
function collapse(text: string): string {
  return text.replace(/\s+/g, '')
}

/**
 * 去掉 Markdown 标记与全部空白，只留下可见内容。
 *
 * 用于跨块边界的内容比对：源码片段里会带上 `- `、`# `、`**` 等标记，
 * 而渲染后的 DOM 文本里没有；去掉标记后两者应逐字一致。
 */
function normalizeContent(text: string): string {
  let result = ''
  let index = 0
  while (index < text.length) {
    const char = text[index]
    if (/\s/.test(char)) {
      index += 1
      continue
    }
    // 行首块级标记与行内标记字符都属于"源码有、渲染无"
    const isLineStart = index === 0 || text[index - 1] === '\n'
    if (char === '\\') {
      index += 1
      continue
    }
    if (char === '#') {
      while (text[index] === '#') index += 1
      continue
    }
    if (isLineStart && (char === '-' || char === '+' || char === '>' || char === '*')) {
      const after = text[index + 1]
      if (after === ' ' || after === '\t') {
        index += 1
        continue
      }
    }
    if (char === '*' || char === '_' || char === '`' || char === '~') {
      index += 1
      continue
    }
    result += char
    index += 1
  }
  return result
}
