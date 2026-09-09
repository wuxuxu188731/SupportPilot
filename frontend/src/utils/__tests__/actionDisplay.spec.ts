/*
 * 审批显示映射测试：枚举中文文案、未知值安全降级、恢复按钮派生条件。
 */

import { describe, expect, it } from 'vitest'

import type { RunStatusResponse } from '@/api/actionTypes'
import {
  APPROVAL_STATUS_TEXT,
  canResumeRun,
  displayText,
  isRunTerminalStatus,
  reasonCodeOptions,
  RUN_STATUS_TEXT,
  statusTagType,
} from '@/utils/actionDisplay'

describe('显示映射', () => {
  it('审批状态与 Run 状态都有中文文案', () => {
    // 保护行为：颜色不能作为唯一表达方式，必须展示文字
    expect(APPROVAL_STATUS_TEXT.pending).toBe('待审批')
    expect(APPROVAL_STATUS_TEXT.approved_with_changes).toBe('修改后批准')
    expect(RUN_STATUS_TEXT.awaiting_approval).toBe('等待审批')
    expect(RUN_STATUS_TEXT.failed).toBe('已失败')
  })

  it('未知枚举值安全降级为原始值（不崩溃）', () => {
    // 边界情况：后端枚举只增不改，前端未命中时显示原始值
    expect(displayText(APPROVAL_STATUS_TEXT, 'future_status')).toBe('future_status')
    expect(displayText(APPROVAL_STATUS_TEXT, null)).toBe('—')
    expect(statusTagType({}, 'unknown')).toBe('default')
  })

  it('原因码选项按动作类型返回中英文对照', () => {
    // 保护行为：提交值必须是英文枚举，只有展示层翻译成中文
    const refundOptions = reasonCodeOptions('refund')
    expect(refundOptions).toHaveLength(11)
    expect(refundOptions.find((item) => item.value === 'quality_issue')?.label).toBe('商品质量问题')

    const compensationOptions = reasonCodeOptions('compensation')
    expect(compensationOptions).toHaveLength(4)
    expect(compensationOptions.find((item) => item.value === 'delayed_shipment')?.label).toBe('发货延迟')
  })

  it('Run 终端状态判定正确', () => {
    // 保护行为：succeeded/failed/cancelled 停止轮询，其余状态继续
    expect(isRunTerminalStatus('succeeded')).toBe(true)
    expect(isRunTerminalStatus('failed')).toBe(true)
    expect(isRunTerminalStatus('cancelled')).toBe(true)
    expect(isRunTerminalStatus('queued')).toBe(false)
    expect(isRunTerminalStatus('running')).toBe(false)
    expect(isRunTerminalStatus('awaiting_approval')).toBe(false)
  })
})

/** 构造 Run 状态响应的最小对象。 */
function runStatus(partial: {
  status?: RunStatusResponse['run']['status']
  lastErrorRetryable?: boolean
  executionErrorRetryable?: boolean
}): RunStatusResponse {
  return {
    run: {
      run_id: 'r-1',
      workflow_type: 'refund',
      status: partial.status ?? 'awaiting_approval',
      created_by_user_id: 'u-1',
      last_error_code: null,
      last_error_retryable: partial.lastErrorRetryable ?? false,
      created_at: '2026-09-01T00:00:00Z',
      updated_at: '2026-09-01T00:00:00Z',
      completed_at: null,
    },
    proposal: null,
    approval: null,
    decision: null,
    current_version: null,
    execution:
      partial.executionErrorRetryable === undefined
        ? null
        : {
            execution_id: 'e-1',
            proposal_id: 'p-1',
            proposal_version_id: 'v-1',
            action_type: 'refund',
            status: 'failed_retryable',
            attempt_count: 2,
            error_code: 'EXECUTION_RETRYABLE_FAILURE',
            error_retryable: partial.executionErrorRetryable,
            claimed_at: '2026-09-01T00:00:00Z',
            updated_at: '2026-09-01T00:00:00Z',
            completed_at: null,
          },
    result: null,
  }
}

describe('canResumeRun 恢复按钮派生条件', () => {
  it('非 admin 一律不显示恢复按钮', () => {
    // 保护行为：只有管理员能恢复 Run（后端仍会 403 兜底）
    expect(canResumeRun(runStatus({ lastErrorRetryable: true }), false, true)).toBe(false)
  })

  it('无 run 数据时不显示', () => {
    // 边界情况：Run 尚未加载时不能给出恢复入口
    expect(canResumeRun(null, true, false)).toBe(false)
  })

  it('决定返回 resume_required=true 时显示（等待审批状态）', () => {
    // 保护行为：202 场景（决定已保存但自动恢复失败）必须保留恢复入口
    expect(canResumeRun(runStatus({ status: 'awaiting_approval' }), true, true)).toBe(true)
  })

  it('Run 可重试错误时显示', () => {
    // 保护行为：run.last_error_retryable 或 execution.error_retryable 均可触发
    expect(canResumeRun(runStatus({ status: 'failed', lastErrorRetryable: true }), true, false)).toBe(true)
    expect(canResumeRun(runStatus({ status: 'failed', executionErrorRetryable: true }), true, false)).toBe(true)
  })

  it('排队中与执行中不显示（无可重试/未决定）', () => {
    // 边界情况：queued/running 且无恢复依据时不给恢复入口
    expect(canResumeRun(runStatus({ status: 'queued' }), true, false)).toBe(false)
    expect(canResumeRun(runStatus({ status: 'running' }), true, false)).toBe(false)
  })

  it('明显不可恢复终态一律隐藏（succeeded/cancelled/不可重试 failed）', () => {
    // 边界情况：终态或不可重试失败即使有 resume_required 标记也隐藏
    expect(canResumeRun(runStatus({ status: 'succeeded' }), true, true)).toBe(false)
    expect(canResumeRun(runStatus({ status: 'cancelled' }), true, true)).toBe(false)
    expect(canResumeRun(runStatus({ status: 'failed' }), true, true)).toBe(false)
    expect(canResumeRun(runStatus({ status: 'failed', lastErrorRetryable: false }), true, true)).toBe(false)
  })

  it('等待审批但无决定时隐藏', () => {
    // 边界情况：awaiting_approval 且既无恢复标记也无重试错误 → 不可恢复
    expect(canResumeRun(runStatus({ status: 'awaiting_approval' }), true, false)).toBe(false)
  })
})
