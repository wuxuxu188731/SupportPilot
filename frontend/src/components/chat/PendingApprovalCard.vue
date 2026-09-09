<script setup lang="ts">
/*
 * 待审批提案卡片：结构化展示 pending_approvals 单条数据。
 *
 * 事实与范围说明：
 *  - approval_id / run_id 只从结构化 pending_approvals 读取，不解析自然语言；
 *  - 「查看审批」点击发出 open-detail 事件，由父级（ChatView）跳转
 *    审批详情路由 /app/approvals/{approval_id}；
 *  - 若审批已被处理，详情页会展示最新状态；不可访问则按 404 规则回列表。
 */
import { ref } from 'vue'
import { NButton, NTag, NText } from 'naive-ui'

import type { PendingApproval } from '@/api/types'
import { formatAmountCents } from '@/utils/money'

const props = defineProps<{
  /** 单条待审批提案（结构化数据） */
  approval: PendingApproval
}>()

const emit = defineEmits<{
  /** 点击「查看审批」：跳转审批详情（由 ChatView 处理路由） */
  'open-detail': [approvalId: string]
}>()

/** action_type → 中文标签与颜色。 */
const ACTION_META: Record<PendingApproval['action_type'], { label: string; color: 'info' | 'warning' }> = {
  refund: { label: '退款提案', color: 'warning' },
  compensation: { label: '补偿提案', color: 'info' },
}

/** 复制反馈文案（复制成功后短暂显示）。 */
const copiedKey = ref<'approval' | 'run' | null>(null)

/** 复制文本到剪贴板：优先 Clipboard API，失败时给出提示（浏览器限制场景）。 */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 继续走降级路径
  }
  // 降级：创建临时输入框执行复制（旧浏览器 / 非安全上下文）
  try {
    const helper = document.createElement('textarea')
    helper.value = text
    helper.setAttribute('readonly', '')
    helper.style.position = 'fixed'
    helper.style.opacity = '0'
    document.body.appendChild(helper)
    helper.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(helper)
    return ok
  } catch {
    return false
  }
}

/** 复制 approval_id，成功后短暂显示「已复制」。 */
async function copyApprovalId(): Promise<void> {
  const ok = await copyText(props.approval.approval_id)
  if (ok) {
    copiedKey.value = 'approval'
    window.setTimeout(() => {
      copiedKey.value = null
    }, 1600)
  }
}

/** 复制 run_id，成功后短暂显示「已复制」。 */
async function copyRunId(): Promise<void> {
  const ok = await copyText(props.approval.run_id)
  if (ok) {
    copiedKey.value = 'run'
    window.setTimeout(() => {
      copiedKey.value = null
    }, 1600)
  }
}

/** 跳转审批详情：点击「查看审批」时发出事件（ChatView 负责路由跳转）。 */
function openDetail(): void {
  emit('open-detail', props.approval.approval_id)
}
</script>

<template>
  <div class="approval-card" data-test="pending-approval-card">
    <div class="approval-head">
      <n-tag :type="ACTION_META[approval.action_type]?.color ?? 'default'" size="small" round>
        {{ ACTION_META[approval.action_type]?.label ?? approval.action_type }}
      </n-tag>
      <n-text depth="3" class="approval-status">状态：{{ approval.status }}</n-text>
    </div>

    <div class="approval-body">
      <p class="approval-amount">
        {{ formatAmountCents(approval.amount_cents, approval.currency) }}
        <n-text depth="3" class="approval-currency">{{ approval.currency }}</n-text>
      </p>
      <n-text v-if="approval.resume_required" type="warning" class="approval-note">
        {{ approval.error_code ? `工作流启动失败（${approval.error_code}），需要管理员恢复` : '工作流需要管理员恢复执行' }}
      </n-text>

      <dl class="approval-ids">
        <div class="id-row">
          <dt>审批 ID</dt>
          <dd>
            <code>{{ approval.approval_id }}</code>
            <n-button size="tiny" quaternary :aria-label="'复制审批 ID'" @click="copyApprovalId">
              {{ copiedKey === 'approval' ? '已复制' : '复制' }}
            </n-button>
          </dd>
        </div>
        <div class="id-row">
          <dt>Run ID</dt>
          <dd>
            <code>{{ approval.run_id }}</code>
            <n-button size="tiny" quaternary :aria-label="'复制 Run ID'" @click="copyRunId">
              {{ copiedKey === 'run' ? '已复制' : '复制' }}
            </n-button>
          </dd>
        </div>
      </dl>
    </div>

    <div class="approval-foot">
      <n-text depth="3" class="approval-note">
        进入审批中心可查看详情并处理该提案，管理员可批准、修改后批准或拒绝。
      </n-text>
      <n-button size="tiny" secondary type="primary" data-test="open-approval" @click="openDetail">
        查看审批
      </n-button>
    </div>
  </div>
</template>

<style scoped>
.approval-card {
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-md);
  padding: var(--sp-space-3);
  background: var(--sp-color-bg-card);
}

.approval-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-2);
  margin-bottom: var(--sp-space-2);
}

.approval-status {
  font-size: var(--sp-font-size-xs);
}

.approval-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
}

.approval-amount {
  margin: 0;
  font-size: var(--sp-font-size-xl);
  font-weight: 700;
  color: var(--sp-color-text-1);
}

.approval-currency {
  font-size: var(--sp-font-size-sm);
  font-weight: 400;
  margin-left: var(--sp-space-1);
}

.approval-note {
  font-size: var(--sp-font-size-xs);
}

.approval-ids {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-1);
}

.id-row {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  font-size: var(--sp-font-size-sm);
}

.id-row dt {
  color: var(--sp-color-text-3);
  flex-shrink: 0;
  min-width: 56px;
}

.id-row dd {
  margin: 0;
  display: flex;
  align-items: center;
  gap: var(--sp-space-1);
  min-width: 0;
}

.id-row code {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 220px;
  background: var(--sp-color-bg-hover);
  border-radius: var(--sp-radius-sm);
  padding: 1px var(--sp-space-1);
  font-size: var(--sp-font-size-xs);
}

.approval-foot {
  margin-top: var(--sp-space-2);
  padding-top: var(--sp-space-2);
  border-top: 1px dashed var(--sp-color-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-2);
  flex-wrap: wrap;
}
</style>
