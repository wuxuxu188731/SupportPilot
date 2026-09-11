/*
 * 企业成员管理 Store（Pinia）：成员列表、添加成员、修改角色、移除成员。
 *
 * 事实说明（依据 docs/frontend/api-inventory.md 与 app/api/organization_router.py）：
 *  - 成员列表企业内所有角色均可读；写操作（添加/改角色/移除）仅 admin，
 *    前端按 currentRole 隐藏入口只是体验优化，后端仍会实时校验；
 *  - 后端禁止 admin 修改或移除自己的成员关系（409），保证企业始终留有管理员，
 *    因此界面不给自己提供改角色/移除入口；
 *  - 移除成员对已产生的业务数据（会话、审批等）不做级联删除，仅收回企业访问权；
 *  - 切换企业/退出登录时通过租户重置机制清空本模块缓存，避免旧企业成员数据泄漏。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as organizationApi from '@/api/organization'
import { ApiError } from '@/api/errors'
import type { MembershipRole, OrganizationMember } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useOrganizationStore } from '@/stores/organization'
import { registerTenantResetHandler } from '@/stores/tenantReset'

/** 添加成员结果分类：供页面给出差异化提示。 */
export type AddMemberOutcome =
  | { result: 'ok'; member: OrganizationMember }
  | { result: 'duplicate' | 'user-missing' | 'forbidden' | 'not-found'; member: null }
  | { result: 'error'; member: null; message: string }

/** 修改角色结果分类。 */
export type ChangeRoleOutcome =
  | { result: 'ok'; memberId: string; role: MembershipRole }
  | { result: 'forbidden' | 'not-found' | 'self-conflict'; memberId: string }
  | { result: 'error'; memberId: string; message: string }

/** 移除成员结果分类。 */
export type RemoveMemberOutcome =
  | { result: 'ok'; memberId: string }
  | { result: 'forbidden' | 'not-found' | 'self-conflict'; memberId: string }
  | { result: 'error'; memberId: string; message: string }

/** 判断错误是否为 ApiError（保留 status 以便区分 403/404/409）。 */
function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

/** 通用安全错误文案：优先使用 ApiError 已翻译的消息，否则用兜底文案。 */
function safeErrorMessage(error: unknown, fallback: string): string {
  if (isApiError(error)) return error.message || fallback
  return error instanceof Error ? error.message : fallback
}

export const useMembersStore = defineStore('members', () => {
  // —— 状态 ——

  /**
   * 当前成员列表所属的企业 id：仅用于「同一企业已有数据则跳过重复请求」的判断，
   * 不参与请求有效性校验（校验只依赖 requestEpoch 与组织 store 的当前快照）。
   * 命名不与 `captureOrganizationId()` 等动作重名，避免 Pinia 合并 state 时
   * 把同名动作覆盖到状态字段上。
   */
  const loadedOrganizationId = ref<string | null>(null)
  /** 当前企业成员列表（后端一次返回全部，无分页） */
  const members = ref<OrganizationMember[]>([])
  /** 成员列表加载中 */
  const loading = ref(false)
  /** 成员列表加载错误消息；null 表示无错误 */
  const error = ref<string | null>(null)
  /** 添加成员请求进行中（防重复提交） */
  const adding = ref(false)
  /** 正在改角色的成员 id 集合（逐个按钮 loading） */
  const roleUpdatingIds = ref<string[]>([])
  /** 正在移除的成员 id 集合（逐个按钮 loading） */
  const removingIds = ref<string[]>([])
  /** 最近一次一次性提示（页面展示后清除） */
  const notice = ref<string | null>(null)
  /** 请求失效标记：企业切换/退出登录时递增，旧响应据此丢弃 */
  const requestEpoch = ref(0)

  // —— 派生状态 ——

  /** 当前用户 id：用于标记「我」并隐藏自我管理入口。 */
  const currentUserId = computed(() => useAuthStore().currentUser?.user_id ?? null)
  /** 我在当前企业的角色是否为 admin（仅界面展示） */
  const isAdmin = computed(() => useOrganizationStore().currentRole === 'admin')
  /** 管理员数量：用于提示企业至少保留一名管理员 */
  const adminCount = computed(() => members.value.filter((item) => item.role === 'admin').length)
  /** 成员列表是否为空（已加载完成且无成员） */
  const isEmpty = computed(() => members.value.length === 0)

  // —— 内部工具 ——

  /** 当前组织 store 快照：读取此刻的企业 id（无企业时为 null）。 */
  function captureOrganizationId(): string | null {
    return useOrganizationStore().currentOrganizationId
  }

  /**
   * 校验异步请求是否仍然有效。
   *
   * 只依赖两件事：请求发起时捕获的 epoch 未被租户切换递增，以及组织 store
   * 此刻的企业 id 仍与发起时一致。**不检查已加载数据的企业 id**——那会形成
   * 循环依赖（首次加载时该字段必然为空，导致响应被误判为过期而丢弃）。
   */
  function isRequestCurrent(epoch: number, capturedOrganizationId: string | null): boolean {
    return requestEpoch.value === epoch && captureOrganizationId() === capturedOrganizationId
  }

  /** 追加一次性提示。 */
  function setNotice(message: string): void {
    notice.value = message
  }

  /** 清除一次性提示（页面消费后调用）。 */
  function clearNotice(): void {
    notice.value = null
  }

  /** 某个成员是否正在改角色/移除（按钮 loading 与禁用依据）。 */
  function isRoleUpdating(userId: string): boolean {
    return roleUpdatingIds.value.includes(userId)
  }

  /** 某个成员是否正在被移除。 */
  function isRemoving(userId: string): boolean {
    return removingIds.value.includes(userId)
  }

  /** 是否是我自己：后端禁止自我改角色/移除，界面同样不提供入口。 */
  function isSelf(userId: string): boolean {
    return currentUserId.value !== null && currentUserId.value === userId
  }

  /** 刷新当前角色状态：写操作被 403 拒绝后角色可能已在别处被降级。 */
  async function refreshCurrentRole(): Promise<void> {
    try {
      await useOrganizationStore().load(true)
    } catch {
      // 角色刷新失败不阻断主流程，下一次请求仍会由后端再次校验
    }
  }

  // —— 租户重置 ——

  /** 切换企业/退出登录时的租户缓存清理：成员相关状态归零并失效旧请求。 */
  function resetForTenantChange(): void {
    requestEpoch.value += 1
    loadedOrganizationId.value = null
    members.value = []
    loading.value = false
    error.value = null
    adding.value = false
    roleUpdatingIds.value = []
    removingIds.value = []
    notice.value = null
  }

  // 注册到租户重置机制：企业切换或退出登录时自动清理成员状态
  registerTenantResetHandler(resetForTenantChange)

  // —— 成员列表 ——

  /**
   * 加载成员列表（force=true 强制刷新；否则同一企业已有数据时跳过）。
   * 后端一次返回该企业全部成员，无分页参数。
   */
  async function loadMembers(force = false): Promise<void> {
    if (loading.value) return
    if (!force && members.value.length > 0 && loadedOrganizationId.value === captureOrganizationId()) return
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) {
      // 无当前企业（守卫生效时不应发生）：不发起租户请求
      error.value = '请先选择企业'
      return
    }
    const epoch = requestEpoch.value
    loading.value = true
    error.value = null
    try {
      const list = await organizationApi.listMembers(capturedOrganizationId)
      // 企业已切换/已退出：丢弃过期响应，避免把旧企业成员写进新企业界面
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return
      members.value = list
      loadedOrganizationId.value = capturedOrganizationId
    } catch (cause) {
      if (!isRequestCurrent(epoch, capturedOrganizationId)) return
      error.value = cause instanceof Error ? cause.message : '成员列表加载失败'
    } finally {
      loading.value = false
    }
  }

  // —— 写操作 ——

  /** 添加成员：成功后把新成员追加进列表并刷新企业列表（角色可能变化）。 */
  async function addMember(username: string, role: MembershipRole): Promise<AddMemberOutcome> {
    if (adding.value) return { result: 'error', member: null, message: '正在提交，请稍候' }
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) {
      return { result: 'error', member: null, message: '请先选择企业' }
    }
    adding.value = true
    try {
      const membership = await organizationApi.addMember(capturedOrganizationId, {
        username: username.trim(),
        role,
      })
      if (captureOrganizationId() !== capturedOrganizationId) {
        // 提交期间切换了企业：不再写入旧企业数据
        return { result: 'error', member: null, message: '企业已切换，请重新操作' }
      }
      // 重新拉取列表以获得服务端权威的加入时间与排序
      await loadMembers(true)
      const stored = members.value.find((item) => item.user_id === membership.user_id)
      const added: OrganizationMember = stored ?? {
        user_id: membership.user_id,
        username: username.trim().toLowerCase(),
        role: membership.role,
        // 后端添加响应不含加入时间，列表刷新失败时留空，不伪造时间
        created_at: '',
      }
      return { result: 'ok', member: added }
    } catch (cause) {
      if (!isApiError(cause)) {
        return { result: 'error', member: null, message: '添加失败，请稍后重试' }
      }
      if (cause.status === 409) return { result: 'duplicate', member: null }
      if (cause.status === 404) return { result: 'user-missing', member: null }
      if (cause.status === 403) {
        // 角色可能已在别处被降级：刷新角色，界面入口随之收敛
        await refreshCurrentRole()
        return { result: 'forbidden', member: null }
      }
      return {
        result: 'error',
        member: null,
        message: safeErrorMessage(cause, '添加失败，请稍后重试'),
      }
    } finally {
      adding.value = false
    }
  }

  /** 修改成员角色：成功后以服务端响应为准更新本地列表项。 */
  async function changeRole(userId: string, role: MembershipRole): Promise<ChangeRoleOutcome> {
    if (isRoleUpdating(userId)) {
      return { result: 'error', memberId: userId, message: '正在提交，请稍候' }
    }
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) {
      return { result: 'error', memberId: userId, message: '请先选择企业' }
    }
    if (isSelf(userId)) return { result: 'self-conflict', memberId: userId }
    roleUpdatingIds.value = [...roleUpdatingIds.value, userId]
    try {
      const membership = await organizationApi.updateMemberRole(capturedOrganizationId, userId, {
        role,
      })
      if (captureOrganizationId() !== capturedOrganizationId) {
        return { result: 'error', memberId: userId, message: '企业已切换，请重新操作' }
      }
      members.value = members.value.map((item) =>
        item.user_id === userId ? { ...item, role: membership.role } : item,
      )
      return { result: 'ok', memberId: userId, role: membership.role }
    } catch (cause) {
      if (!isApiError(cause)) {
        return { result: 'error', memberId: userId, message: '修改角色失败，请稍后重试' }
      }
      if (cause.status === 403) {
        await refreshCurrentRole()
        return { result: 'forbidden', memberId: userId }
      }
      if (cause.status === 404) {
        // 成员已被移除：刷新列表让界面与服务端一致
        await loadMembers(true)
        return { result: 'not-found', memberId: userId }
      }
      if (cause.status === 409) return { result: 'self-conflict', memberId: userId }
      return {
        result: 'error',
        memberId: userId,
        message: safeErrorMessage(cause, '修改角色失败，请稍后重试'),
      }
    } finally {
      roleUpdatingIds.value = roleUpdatingIds.value.filter((item) => item !== userId)
    }
  }

  /** 移除成员：成功后从本地列表移除该项。 */
  async function removeMember(userId: string): Promise<RemoveMemberOutcome> {
    if (isRemoving(userId)) {
      return { result: 'error', memberId: userId, message: '正在提交，请稍候' }
    }
    const capturedOrganizationId = captureOrganizationId()
    if (!capturedOrganizationId) {
      return { result: 'error', memberId: userId, message: '请先选择企业' }
    }
    if (isSelf(userId)) return { result: 'self-conflict', memberId: userId }
    removingIds.value = [...removingIds.value, userId]
    try {
      await organizationApi.removeMember(capturedOrganizationId, userId)
      if (captureOrganizationId() !== capturedOrganizationId) {
        return { result: 'error', memberId: userId, message: '企业已切换，请重新操作' }
      }
      members.value = members.value.filter((item) => item.user_id !== userId)
      return { result: 'ok', memberId: userId }
    } catch (cause) {
      if (!isApiError(cause)) {
        return { result: 'error', memberId: userId, message: '移除成员失败，请稍后重试' }
      }
      if (cause.status === 403) {
        await refreshCurrentRole()
        return { result: 'forbidden', memberId: userId }
      }
      if (cause.status === 404) {
        await loadMembers(true)
        return { result: 'not-found', memberId: userId }
      }
      if (cause.status === 409) return { result: 'self-conflict', memberId: userId }
      return {
        result: 'error',
        memberId: userId,
        message: safeErrorMessage(cause, '移除成员失败，请稍后重试'),
      }
    } finally {
      removingIds.value = removingIds.value.filter((item) => item !== userId)
    }
  }

  return {
    // 状态
    loadedOrganizationId,
    members,
    loading,
    error,
    adding,
    roleUpdatingIds,
    removingIds,
    notice,
    requestEpoch,
    // 派生
    currentUserId,
    isAdmin,
    adminCount,
    isEmpty,
    // 动作
    loadMembers,
    addMember,
    changeRole,
    removeMember,
    isRoleUpdating,
    isRemoving,
    isSelf,
    setNotice,
    clearNotice,
  }
})
