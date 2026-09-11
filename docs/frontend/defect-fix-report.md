# SupportPilot 缺陷修复报告

> 缺陷来源：`docs/frontend/manual-test-defect-report.md`（DEF-01 ~ DEF-09）
> 修复日期：2026-09-11
> 修复基线：仓库 `master`，`625034e`（缺陷报告存档于 `92c281a`，修复截止 `c1fcae6`）
> 验证方式：前端 `typecheck` + `lint` + `vitest`（428 条）、后端 `pytest`（972 通过 / 10 跳过）、
> `app.openapi()` 实测核对

---

## 1. 修复了哪些缺陷

### 1.1 一览

| 缺陷 | 级别 | 类型 | 状态 |
| --- | --- | --- | --- |
| DEF-01 审批详情页时间未做本地时区换算 | 中 | 代码 | ✅ 已修复（含测试） |
| DEF-02 会话设置空白输入静默关闭 | 中 | 代码 | ✅ 已修复（含测试，并修复同类潜在问题） |
| DEF-03 本地判定登录过期时不展示提示 | 低 | 代码 | ✅ 已修复（含测试） |
| DEF-05 手册 A-01 分组标签不符 | 低 | 文档 + 代码 | ✅ 已修复（文档修订 + 补齐接口 `tags`） |
| DEF-04 手册 A-02 路径数 23 与实测 21 不符 | 低 | 文档 | ✅ 已修复 |
| DEF-07 手册 F-02 去重入口写错 | 低 | 文档 | ✅ 已修复（**附带的产品策略问题未决**，见 §3） |
| DEF-08 证据状态英文枚举与手册文案不符 | 信息 | 文档 + 注释 | ✅ 已修复 |
| DEF-06 手册 D-06 前置条件在界面上不可达 | 低 | 文档 + 产品 | ⚠️ 文档已修订；**产品侧降级入口未加**（见 §3） |
| DEF-09 「刷新」按钮 loading 态采样不到 | 信息 | 非缺陷 | — 无需修复（报告已判定为本地环境过快） |

### 1.2 提交对应关系

| 提交 | 内容 |
| --- | --- |
| `92c281a` | 存档缺陷报告（改动代码前的仓库清点，符合 `AGENTS.md` §3） |
| `7363628` | DEF-01 |
| `923dd9a` | DEF-02 |
| `581c3cb` | DEF-03 |
| `1b25503` | DEF-05（产品侧 `tags`） |
| `b0488d3` | DEF-04 / 05 / 06 / 07 / 08（文档修订） |
| `c1fcae6` | DEF-08（`types.ts` 注释一律） |

---

## 2. 如何修复的

所有代码缺陷均按「先写失败测试 → 确认失败原因与报告一致 → 再改实现」的顺序处理；
下列 5 条新增用例在修复前**确实失败**，且失败信息与报告描述的根因一致。

### 2.1 DEF-01 审批详情页时间未做本地时区换算

**根因**：审批详情相关的三个组件直接把后端 UTC 文本插值到模板，没有复用
成员管理页/知识库页已经在用的 `utils/time.ts::formatDateTime`。后端
`app/schemas/action.py` 中这些字段均标注为「UTC 文本格式」，前端却是唯一
没有换算的一处，因此同一时刻在列表页显示 `10:21`、在详情页显示
`2026-09-11T02:21:32Z`。

**改动**（3 个文件，均为「改用统一格式化函数」）：

- `frontend/src/views/ApprovalDetailView.vue`：基本信息「创建时间」、
  请求版本「创建时间」、决定摘要「决定时间」改为 `formatDateTime(...)`；
- `frontend/src/components/approval/ApprovalVersionTimeline.vue`：
  版本历史每条的版本时间改为 `formatDateTime(...)`；
- `frontend/src/components/approval/RunStatusPanel.vue`：Run 的
  「创建/更新时间/完成时间」改为 `formatDateTime(...)`。

`formatDateTime` 复用既有的 `parseUtcText`：带 `Z` / `±hh:mm` 的 ISO 文本按
ISO 解析，无时区后缀的文本按 UTC 解释，空值统一显示「—」（与 `RunStatusPanel`
原有的 `EMPTY` 占位语义一致，因此 `|| EMPTY` 可以安全去掉）。未改动后端数据
与接口契约。

**测试**：`frontend/src/views/__tests__/ApprovalDetailView.spec.ts` 新增
「时间展示（H-06）」两条用例——断言原始 UTC ISO 文本（含 `T`/`Z`）不再出现，
且基本信息、版本时间线 V1/V2、决定摘要、Run 三个时间字段分别等于按本地时区
渲染的 `YYYY-MM-DD HH:mm`；另一条断言未完成 Run 的完成时间显示「—」。
期望值由测试内 `localDateTime()` 从同一 UTC 时刻推导，**不依赖测试机时区**。

### 2.2 DEF-02 会话设置空白输入静默关闭

**根因**：`ConversationSettingsDialog.vue` 的 `submit()` 返回类型为 `void`。
Naive UI 的 `n-dialog` 在 `positive-click` 回调返回 `false`（或 resolve 为
`false`）时才**不**关闭对话框；返回 `undefined` 会立即关闭并卸载，于是
`errorMessage.value = '系统提示词不能为空'` 刚赋值就随组件卸载，成为死代码。

已在 `frontend/node_modules/naive-ui/es/modal/src/Modal.mjs` 中确认该判定逻辑：

```js
function handlePositiveClick() {
  const { onPositiveClick } = props;
  if (onPositiveClick) Promise.resolve(onPositiveClick()).then(value => {
    if (value === false) return;
    doUpdateShow(false);
  }); else doUpdateShow(false);
}
```

**改动**（`frontend/src/components/chat/ConversationSettingsDialog.vue`）：

- `submit()` 改为返回 `boolean`：空白分支返回 **`false`**（阻止关闭，错误文案
  随之可见），有效输入返回 `true`（关闭时机与修复前完全一致——仍由父组件
  `ChatView.onSaveSystemPrompt` 在保存成功后收起）；
- **同时修复同类潜在问题**：`close()` 在 `updating` 时原本只是 `return`
  （即返回 `undefined`），**并不能**拦住 Naive UI 的自动关闭，与代码注释声明的
  「更新中禁止关闭，防止半途丢状态」相反。改为返回 `false`，使该意图真正生效。

**测试**：新增 `frontend/src/components/chat/__tests__/ConversationSettingsDialog.spec.ts`
（5 条），挂载真实 `n-modal`（仅打桩 teleport）验证：空白输入不保存、**不关闭**
且错误文案可见；有效输入去空白后提交并允许关闭；被拒后可改值重新保存；
更新中保存/取消都不触发关闭；重开回显已保存值。

### 2.3 DEF-03 本地判定登录过期时不展示提示

**根因**：`reason=expired` 原先**只**由全局 401 回调写入（`main.ts`）。而
`authStore.doRestore()` 在「本地即可判定令牌过期」时会直接 `clearSession()`，
此时 `isLoggedIn` 变为 false，路由守卫走与「从未登录」**完全相同**的分支
（`withRedirectQuery`，不带 `reason`），登录页的
`message.warning('登录已过期，请重新登录')` 因此不触发。

**改动**：

- `frontend/src/stores/auth.ts`：新增 `sessionExpired` 状态，语义为「上一次会话
  因令牌过期/失效结束（而非用户主动退出）」。置位点：本地判定过期、
  `/auth/me` 返回 401、`handleSessionExpired()`（服务端 401）。复位点：
  `persistToken()`（新会话开始）、`logout()`（主动退出，语义上不是过期）。
- `frontend/src/router/index.ts`：`withRedirectQuery` 增加 `expired` 参数；
  守卫的未登录分支改为 `withRedirectQuery(to.fullPath, authStore.sessionExpired)`，
  跳转携带 `redirect` **与** `reason=expired`，与全局 401 路径统一口径。

`main.ts` 中原有的显式 `reason: 'expired'` 保持不变（两条路径互为兜底）。

**测试**：`frontend/src/router/__tests__/guards.spec.ts` 新增 3 条——本地过期
必须带 `reason=expired` 且保留 `redirect`；从未登录**不得**误报过期；主动退出
后访问受保护页面**不得**提示过期（防止把用户自己的动作说成异常）。

### 2.4 DEF-05（产品侧）补齐 Swagger 分组标签

**根因**：`app/api/router.py` 中 `PUT /conversations/{conversation_id}/system-prompt/`
未声明 `tags`，FastAPI 将其归入 `default` 分组，与其余会话接口的中文标签不一致。

**改动**：补 `tags=["设置会话的系统提示词"]`（与 `POST /conversations/` 已有的
同名标签合并为同一分组）。**响应模型与接口契约不变**：实测 OpenAPI 仍为
**21 个路径 / 26 个操作**，`SystemPromptUpdated` 的 `$ref` 未变，`default`
分组消失。

### 2.5 DEF-04 / 05 / 06 / 07 / 08 文档修订

- **DEF-04**（`A-02` 路径数）：`23` → **`21`**。经 `git show 42b83d9:app/api/*.py`
  核对，`42b83d9` 时是 **23 个操作**（会话 5 + 认证 3 + 知识库 7 + 审批 5 +
  组织 3）、**20 个路径**——原文把**操作数**误写进了路径统计。
  `manual-test-runbook.md` 与 `api-inventory.md` 同步修正，并在盘点文档加了
  勘误段与复核命令。
- **DEF-05**（`A-01` 分组标签）：改为实际的 **9 个**标签
  （`knowledge`、`authentication`、`organizations`、`退款/补偿审批与Run`、
  `创建新会话`、`设置会话的系统提示词`、`查询会话列表`、`查询会话历史消息`、
  `向模型聊天`），并明确「不叫 `conversations`/`approvals`」「不应出现 `default`」。
- **DEF-06**（`D-06` 前置条件）：`D-06` 前置条件改为给出可执行的**后端 `PATCH`**
  降级命令（并建议先做 D-06 再做 D-05 以避免需要降级）；`D-05` 补注「角色下拉
  在成员已是管理员时即被禁用，界面无降级入口」并附 `MembersView.vue` 的
  `:disabled` 条件原文。
- **DEF-07**（`F-02` 去重入口）：更正为**「上传新版本」**入口，并补充实现真身
  （`ingestion.py::_resolve_duplicate` 以 `document_id` 为范围）；新增 **`F-02b`**
  记录「上传文档」入口不做跨文档去重的反例实测与检索污染风险。
- **DEF-08**（证据状态文案）：`E-07` 明确证据状态**按设计原样透出小写英文枚举**
  （实测样例 `检索：multi · 2 轮 · 证据 insufficient · 5297 ms`，与
  `MessageList.vue:92` 的模板字符串一致）。顺带修正
  `frontend/src/api/types.ts` 中与实现不符的注释（原写「SUFFICIENT /
  INSUFFICIENT（大写）」，实际后端 `app/knowledge/retrieval.py` 产生小写枚举）。
- 另在 `§11 已知边界` 补 4 条（上传文档不去重、管理员不可降级、提示词无法清空、
  证据状态英文枚举），避免后续回归误判。

---

## 3. 哪些缺陷暂未修复

以下**不是遗漏**，而是需要产品/业务决策或超出「改代码」的范围，已如实登记：

### 3.1 DEF-06 附带：是否补「管理员 → 客服」降级入口（未决）

- **已完成的**：手册部分（前置条件不可达）已修订。
- **未完成的**：UI 上**没有**新增降级入口。现状是成员一旦成为管理员，其行的
  角色下拉即被禁用（`:disabled="membersStore.isRoleUpdating(member.user_id) ||
  member.role === 'admin'"`），只能「移除」不能降级。
- **为什么不改**：这**可能是刻意的防误操作设计**（避免把最后一名管理员降级），
  也可能是实现疏漏；报告本身也只给出「建议明确这是设计意图，或补充降级入口」
  两个方向。二者产品语义不同，**需要维护者确认后**再定，不宜由我单方面选定。
  临时绕行方式已写进手册 `D-06`（用后端 `PATCH` 接口）。

### 3.2 DEF-07 附带：「上传文档」入口的跨文档内容去重（未决）

- **已完成的**：手册入口描述错误已更正。
- **未完成的**：**未**在「上传文档」入口增加内容级提示/拦截。
- **为什么不改**：报告 §DEF-07 明确写的是「建议产品侧确认是否需要在『上传文档』
  入口增加内容级提示」。该入口当前行为（201 创建新文档）与 `ingestion.py`
  实现**完全一致**，属「实现符合设计」，不是代码缺陷。是否要改变产品策略
  （影响：同一内容可被重复上传为多个独立文档并**同时参与检索**，本次实测已因此
  让检索判定证据不足）需产品决策。已在手册新增 `F-02b` 与 `§11.11` 记录，
  避免被误报为回归缺陷。

### 3.3 未修复项中不含「代码缺陷」

DEF-01 / 02 / 03 三个代码缺陷已全部修复。DEF-04 / 05 / 08 属文档与文案口径，
已修订；DEF-09 报告本身已判定为**非缺陷**（本机后端响应 < 350 ms，loading 态
采样不到），无需处理。

---

## 4. 发现的其他潜在问题

### 4.1 已一并修复：`close()` 无法真正阻止更新中关闭（同类缺陷）

**位置**：`frontend/src/components/chat/ConversationSettingsDialog.vue`

与 DEF-02 **同一根因**（`positive-click` / `negative-click` 回调返回值语义）：
`close()` 在 `props.updating` 时只 `return`（返回 `undefined`），Naive UI 仍会
关闭对话框，与代码注释「更新中禁止关闭，防止半途丢状态」相反。
已随 DEF-02 一并修复为 `return false`，并补了对应测试（`更新中：保存与取消
都不触发关闭请求`）。**此项不在原缺陷报告清单内**，是修复过程中新发现的。

### 4.2 已一并修复：`RetrievalSummary.evidence_status` 类型注释与实现不符

**位置**：`frontend/src/api/types.ts:248`

原注释写「SUFFICIENT / INSUFFICIENT（大写）等」，但后端
`app/knowledge/retrieval.py` 实际产生**小写**枚举（`sufficient` / `insufficient` /
`failed` / `not_needed`）并经 `app/knowledge/results.py` 原样透出。若后续有人
按注释写 `=== 'SUFFICIENT'` 判断，会**静默失效**。已改为与实现一致的注释。

### 4.3 未修复（建议关注）：保存失败时用户输入会丢失

**位置**：`frontend/src/views/ChatView.vue::onSaveSystemPrompt` +
`ConversationSettingsDialog.vue`

Naive UI 在点击「保存」时会**立即**关闭对话框（早于 `PUT` 请求返回），
所以当请求失败（网络错误、422、404 等）时，用户只能看到一条 `message.error`
toast，**对话框已关闭、刚刚编辑的文本已丢失**，需要重新打开并重新输入。

- 有效输入路径的正常行为（报告 E-10 已确认）不受影响，因此**未改动**；
- 若要改善，方向是「改为受控 `:show`，仅在保存成功后关闭，失败时保留草稿与
  错误位置」——即报告 DEF-02 提到的第二个修复方向。属于体验优化而非本次缺陷，
  留待确认。

### 4.4 未修复（环境/数据）：测试遗留数据无法清理

缺陷报告 §4 记录了一条标题为字面量 `undefined` 的文档
（`document_id = 73179c1c-…`）留在测试库中，成因是我方测试脚本的误操作。

- **不是产品缺陷**（后端对标题 1–200 字符的校验本身正确）；
- 但**产品确实没有「删除文档」能力**（手册 §11.3 已声明），导致这类误上传
  **永久残留并持续参与检索**。这与 §3.2 的检索污染是同一个治理缺口：
  建议一并评估「文档删除/停用即退出检索」的能力。
- 该数据位于专用测试库 `manual-test.db`，**未污染** `chat_history.db`。

### 4.5 未修复（测试环境）：`guards.spec.ts` 在全量并行跑时可能超时

本次基线（改动前的全量 `npm run test`）出现 **1 例偶发失败**：
`guards.spec.ts > 未登录访问 /app：跳转登录页并记录原始目标` 报
`Test timed out in 20000ms`；**单独运行该文件时 18 条全部通过**，改动后的
两次全量运行也全部通过（428/428）。推断为本机资源竞争（该次全量运行
`collect` 耗时 598 s）导致的偶发超时，**非本次改动引入**，也**未修改**
`testTimeout` 或该用例。

### 4.6 说明：`sessionExpired` 标记的生命周期

DEF-03 新增的 `sessionExpired` 会保持为 `true` 直到下一次登录成功
（`persistToken`）或主动退出（`logout`）。这意味着：**过期的用户不登录、
直接访问任意受保护路由时，每次都会带上 `reason=expired` 并提示「登录已过期」**。
这与「他确实是被过期登出的」一致，属**有意行为**；在此记录以便后续 review
时不会被误认为状态泄漏。

---

## 5. 验证证据

| 检查项 | 命令 | 结果 |
| --- | --- | --- |
| 前端类型检查 | `npm run typecheck` | 通过（无输出即无错误） |
| 前端 Lint | `npm run lint` | 通过 |
| 前端测试 | `npm run test` | **428 passed / 40 files**（改动前 418，新增 10 条） |
| 后端测试 | `.venv/Scripts/python -m pytest -q` | **972 passed, 10 skipped** |
| 接口面核对 | `python -c "import main; print(len(main.app.openapi()['paths']))"` | **21 路径 / 26 操作**，`default` 分组消失 |
| 工作区状态 | `git status --short` | 干净（所有改动已提交） |

**验证边界（如实说明）**：本次修复的验证为**组件级自动化**——`vitest` 在 jsdom 中
挂载**真实的** Vue 组件与**真实的** naive-ui 构建，断言真实渲染出的 DOM 文本
（DEF-01）与真实的对话框关闭机制（DEF-02，已对照 `naive-ui` 源码中的
`value === false` 判定）；DEF-03 断言真实的路由守卫跳转 query。**未**重新执行
缺陷报告所用的「真实浏览器 + 真实后端 + 真实模型」端到端手工回归——
建议按修订后的 `manual-test-runbook.md` 复跑 A-01/A-02、E-07/E-10、F-02、
H-02、H-06 以做最终确认。
