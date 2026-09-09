/*
 * 审批与 Action Run API 封装：/approvals/ 与 /action-runs/。
 *
 * 关键约定（与后端契约一致）：
 *  - listApprovals 的 status 参数可省略（全部）或取四种审批状态之一；
 *  - decideApproval 返回「真实 HTTP 状态码 + 响应体」而不是只返回 body：
 *    200/201/202 语义不同（幂等重放 / 首次决定且恢复成功 / 决定已保存但
 *    恢复失败），页面必须分流处理；
 *  - 审批决定与显式恢复都**不做自动重试**（决定/恢复是业务事实，重复提交
 *    由服务端幂等与 409 处理，客户端不重放）。
 */

import { httpClient } from './http'
import type {
  ApprovalDetailResponse,
  ApprovalListItem,
  ApprovalListQuery,
  DecisionRequestPayload,
  DecisionResponse,
  DecisionSubmitResult,
  ResumeResponse,
  RunStatusResponse,
} from './actionTypes'

/** 审批列表默认每页条数（后端默认一致）。 */
export const DEFAULT_APPROVAL_LIST_LIMIT = 20

/** 审批列表下限：返回条数达到该值即认为可能还有下一页（无总数接口）。 */
export const APPROVAL_LIST_PAGE_SIZE = 20

/** 拉取当前企业审批列表（可按状态筛选、分页；不返回总数）。 */
export async function listApprovals(
  query: ApprovalListQuery = {},
): Promise<ApprovalListItem[]> {
  const params: Record<string, string | number> = {
    limit: query.limit ?? DEFAULT_APPROVAL_LIST_LIMIT,
    offset: query.offset ?? 0,
  }
  if (query.status) {
    params.status = query.status
  }
  const response = await httpClient.get<ApprovalListItem[]>('/approvals/', { params })
  return response.data
}

/** 读取单个审批详情（成员均可读；跨企业/不存在统一 404）。 */
export async function getApprovalDetail(
  approvalId: string,
): Promise<ApprovalDetailResponse> {
  const response = await httpClient.get<ApprovalDetailResponse>(
    `/approvals/${encodeURIComponent(approvalId)}/`,
  )
  return response.data
}

/**
 * 作出审批决定（仅 admin）。
 *
 * 返回真实 HTTP 状态码与响应体，调用方按 200/201/202 分流：
 *  - 201：首次决定且自动恢复成功；
 *  - 202：决定已保存但工作流恢复失败（resume_required=true）；
 *  - 200：相同决定的幂等重复提交。
 * 非 2xx（403/404/409/422）由 httpClient 抛 ApiError，不在这里吞掉。
 */
export async function decideApproval(
  approvalId: string,
  payload: DecisionRequestPayload,
): Promise<DecisionSubmitResult> {
  const response = await httpClient.post<DecisionResponse>(
    `/approvals/${encodeURIComponent(approvalId)}/decisions/`,
    payload,
  )
  return { status: response.status, data: response.data }
}

/** 查询 Action Run 完整状态（成员均可读；跨企业/不存在统一 404）。 */
export async function getRunStatus(
  runId: string,
): Promise<RunStatusResponse> {
  const response = await httpClient.get<RunStatusResponse>(
    `/action-runs/${encodeURIComponent(runId)}/`,
  )
  return response.data
}

/**
 * 显式恢复 Run（仅 admin）。
 *
 * 成功返回 ResumeResponse（200）；503 表示恢复失败但业务事实（含审批决定）
 * 仍然有效，可稍后重试，该错误由 httpClient 以 ApiError 抛出，调用方按
 * status=503 / code 提示「决定仍然有效，可稍后再试」。
 */
export async function resumeRun(
  runId: string,
): Promise<ResumeResponse> {
  const response = await httpClient.post<ResumeResponse>(
    `/action-runs/${encodeURIComponent(runId)}/resume/`,
  )
  return response.data
}
