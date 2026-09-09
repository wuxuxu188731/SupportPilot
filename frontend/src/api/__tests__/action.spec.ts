/*
 * 审批与 Run API 封装测试：路径、查询参数、请求体与真实状态码透传。
 * 通过 mock httpClient 进行，不触达网络。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

// mock 统一 HTTP 客户端：验证调用形态
vi.mock('@/api/http', () => ({
  httpClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import * as actionApi from '@/api/action'
import { httpClient } from '@/api/http'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('审批列表 API', () => {
  it('GET /approvals/ 携带默认 limit=20、offset=0，不传 status 表示全部', async () => {
    // 保护行为：未显式传参时应带默认分页参数；不传 status 表示不过滤
    vi.mocked(httpClient.get).mockResolvedValue({ data: [] })

    const result = await actionApi.listApprovals()

    expect(result).toEqual([])
    expect(httpClient.get).toHaveBeenCalledWith('/approvals/', {
      params: { limit: 20, offset: 0 },
    })
  })

  it('指定 status 时筛选参数出现在查询串中', async () => {
    // 保护行为：状态筛选必须原样传给后端（待审批/已批准等 Tab 复用）
    vi.mocked(httpClient.get).mockResolvedValue({ data: [] })

    await actionApi.listApprovals({ status: 'pending', limit: 20, offset: 0 })

    expect(httpClient.get).toHaveBeenCalledWith('/approvals/', {
      params: { limit: 20, offset: 0, status: 'pending' },
    })
  })

  it('分页参数原样透传（加载更多场景）', async () => {
    // 保护行为：调用方传入的 limit/offset 必须出现在查询串中
    vi.mocked(httpClient.get).mockResolvedValue({ data: [] })

    await actionApi.listApprovals({ status: 'approved', limit: 20, offset: 40 })

    expect(httpClient.get).toHaveBeenCalledWith('/approvals/', {
      params: { limit: 20, offset: 40, status: 'approved' },
    })
  })
})

describe('审批详情与决定 API', () => {
  it('GET /approvals/{id}/ 按审批 id 请求', async () => {
    // 保护行为：详情路径必须包含审批 id，供后端做归属校验
    vi.mocked(httpClient.get).mockResolvedValue({ data: { approval_id: 'a-1' } })

    const result = await actionApi.getApprovalDetail('a-1')

    expect(result).toEqual({ approval_id: 'a-1' })
    expect(httpClient.get).toHaveBeenCalledWith('/approvals/a-1/')
  })

  it('POST /approvals/{id}/decisions/ 返回真实 HTTP 状态码与响应体（201）', async () => {
    // 保护行为：页面必须能区分 200/201/202，因此 API 返回值必须带 status
    const body = { decision_id: 'd-1', decision: 'approved' }
    vi.mocked(httpClient.post).mockResolvedValue({ status: 201, data: body })

    const result = await actionApi.decideApproval('a-1', {
      decision: 'approved',
      changes: null,
      comment: null,
    })

    expect(result.status).toBe(201)
    expect(result.data).toEqual(body)
    expect(httpClient.post).toHaveBeenCalledWith('/approvals/a-1/decisions/', {
      decision: 'approved',
      changes: null,
      comment: null,
    })
  })

  it('决定接口按真实状态码透传 200 与 202', async () => {
    // 保护行为：200（幂等重放）与 202（决定已保存但恢复失败）不能被吞掉
    vi.mocked(httpClient.post).mockResolvedValueOnce({
      status: 200,
      data: { decision_id: 'd-1', resume_required: false },
    })
    vi.mocked(httpClient.post).mockResolvedValueOnce({
      status: 202,
      data: { decision_id: 'd-1', resume_required: true },
    })

    const idempotent = await actionApi.decideApproval('a-1', {
      decision: 'approved',
      changes: null,
      comment: null,
    })
    const pending = await actionApi.decideApproval('a-1', {
      decision: 'approved',
      changes: null,
      comment: null,
    })

    expect(idempotent.status).toBe(200)
    expect(pending.status).toBe(202)
    expect(pending.data.resume_required).toBe(true)
  })

  it('决定接口异常（409/422）不吞掉，交由 httpClient 抛 ApiError', async () => {
    // 保护行为：领域错误必须原样抛出，由调用方按 code/status 分流
    vi.mocked(httpClient.post).mockRejectedValueOnce(new Error('conflict'))

    await expect(
      actionApi.decideApproval('a-1', { decision: 'approved', changes: null, comment: null }),
    ).rejects.toThrow('conflict')
    expect(httpClient.post).toHaveBeenCalledTimes(1)
  })
})

describe('Run 状态与恢复 API', () => {
  it('GET /action-runs/{id}/ 按 run id 请求完整状态', async () => {
    // 保护行为：Run 状态查询路径必须包含 run id
    vi.mocked(httpClient.get).mockResolvedValue({ data: { run: { run_id: 'r-1' } } })

    const result = await actionApi.getRunStatus('r-1')

    expect(result.run.run_id).toBe('r-1')
    expect(httpClient.get).toHaveBeenCalledWith('/action-runs/r-1/')
  })

  it('POST /action-runs/{id}/resume/ 发起恢复请求', async () => {
    // 保护行为：恢复必须是 POST 请求且路径含 run id
    vi.mocked(httpClient.post).mockResolvedValue({ data: { resume_ok: true } })

    const result = await actionApi.resumeRun('r-1')

    expect(result.resume_ok).toBe(true)
    expect(httpClient.post).toHaveBeenCalledWith('/action-runs/r-1/resume/')
  })

  it('恢复失败（503）由 httpClient 抛出，不自动重试', async () => {
    // 保护行为：503 必须原样抛给调用方提示「决定仍然有效」，且只调用一次
    vi.mocked(httpClient.post).mockRejectedValueOnce(new Error('checkpoint unavailable'))

    await expect(actionApi.resumeRun('r-1')).rejects.toThrow('checkpoint unavailable')
    expect(httpClient.post).toHaveBeenCalledTimes(1)
  })
})
