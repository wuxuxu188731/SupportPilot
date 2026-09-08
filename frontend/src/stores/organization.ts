/*
 * 企业上下文 Store（Pinia）：企业列表、当前企业、当前角色、切换与创建。
 *
 * 事实说明：
 *  - 角色字段只用于界面展示；权限的真正校验由后端完成，前端缓存的角色
 *    不是安全依据；
 *  - 当前企业 id 持久化在 localStorage，刷新后恢复；恢复前会先拉取最新
 *    企业列表校验，已失去权限（被移出企业）时清除选择并要求重新选择；
 *  - 切换企业会执行已注册的租户缓存清理回调（见 tenantReset.ts）。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as organizationApi from '@/api/organization'
import type { OrganizationAccess } from '@/api/types'
import {
  clearOrganizationStorage,
  readOrganizationStorage,
  writeOrganizationStorage,
} from '@/stores/persistence'
import { runTenantResetHandlers } from '@/stores/tenantReset'

export const useOrganizationStore = defineStore('organization', () => {
  // —— 状态 ——

  /** 当前用户可访问的企业列表（含我在各企业的角色） */
  const organizations = ref<OrganizationAccess[]>([])
  /** 当前选择的企业 id；null 表示尚未选择或已失效 */
  const currentOrganizationId = ref<string | null>(readOrganizationStorage())
  /** 企业列表加载中 */
  const loading = ref(false)
  /** 是否已成功加载过列表（成功后即使列表为空也保持 true，避免重复请求） */
  const loaded = ref(false)
  /** 列表加载错误消息；null 表示无错误 */
  const error = ref<string | null>(null)
  /** 创建企业请求进行中 */
  const creating = ref(false)

  /** 内部防并发：同一列表加载过程只执行一次 */
  let loadPromise: Promise<void> | null = null

  // —— 派生状态 ——

  /** 当前企业完整信息；列表未加载或选择失效时为 null */
  const currentOrganization = computed(
    () => organizations.value.find((item) => item.organization_id === currentOrganizationId.value) ?? null,
  )
  /** 当前用户在当前企业的角色（仅展示用）；无当前企业时为 null */
  const currentRole = computed(() => currentOrganization.value?.role ?? null)
  /** 是否已选择有效的当前企业 */
  const hasCurrentOrganization = computed(() => currentOrganization.value !== null)
  /** 是否一个企业都没有（引导创建） */
  const isEmpty = computed(() => organizations.value.length === 0)

  // —— 内部工具 ——

  /** 校验本地保存的企业选择：已失效（被移出企业）则清除。 */
  function validateLocalSelection(): void {
    const savedId = readOrganizationStorage()
    if (savedId && !organizations.value.some((item) => item.organization_id === savedId)) {
      // 用户已失去该企业权限（或企业已不存在）：清除旧选择，要求重新选择
      clearSelection()
    }
  }

  // —— 对外动作 ——

  /** 加载企业列表；加载成功后校验并保留/清除本地企业选择。 */
  async function load(force = false): Promise<void> {
    if (loaded.value && !force) return
    if (loading.value && loadPromise) return loadPromise
    loading.value = true
    error.value = null
    loadPromise = (async () => {
      try {
        const list = await organizationApi.listOrganizations()
        organizations.value = list
        loaded.value = true
        // 校验本地保存的企业是否仍然有效（可能已被移出该企业）
        validateLocalSelection()
      } catch (cause) {
        error.value = cause instanceof Error ? cause.message : '企业列表加载失败'
      } finally {
        loading.value = false
        loadPromise = null
      }
    })()
    return loadPromise
  }

  /** 守卫/页面使用：确保列表已加载成功过一次（失败不重试吞错，由页面展示）。 */
  async function ensureLoaded(): Promise<void> {
    if (!loaded.value) {
      await load()
    }
  }

  /** 创建企业：成功后重新拉取列表（拿到 admin 角色）并自动选择新企业。 */
  async function create(name: string): Promise<void> {
    if (creating.value) return // 防重复提交兜底
    creating.value = true
    try {
      const created = await organizationApi.createOrganization({ name })
      // 刷新列表：新企业在列表中带 admin 角色
      await load(true)
      await select(created.organization_id)
    } finally {
      creating.value = false
    }
  }

  /** 选择/切换企业：先执行租户缓存清理，再持久化与更新状态。 */
  async function select(organizationId: string): Promise<boolean> {
    const target = organizations.value.find((item) => item.organization_id === organizationId)
    if (!target) {
      // 选择不在列表中的企业属于逻辑错误，调用方应重新加载列表
      return false
    }
    if (organizationId !== currentOrganizationId.value) {
      // 切换企业：清理未来可能存在的租户业务缓存（当前无注册项，预留）
      await runTenantResetHandlers()
    }
    currentOrganizationId.value = organizationId
    writeOrganizationStorage(organizationId)
    return true
  }

  /** 清除当前企业选择（失去权限 / 退出登录时调用）。 */
  function clearSelection(): void {
    currentOrganizationId.value = null
    clearOrganizationStorage()
  }

  /** 整体重置（退出登录时由 auth store 调用）：清空列表与选择。 */
  function reset(): void {
    organizations.value = []
    loaded.value = false
    error.value = null
    clearSelection()
  }

  return {
    // 状态
    organizations,
    currentOrganizationId,
    loading,
    loaded,
    error,
    creating,
    // 派生
    currentOrganization,
    currentRole,
    hasCurrentOrganization,
    isEmpty,
    // 动作
    load,
    ensureLoaded,
    create,
    select,
    clearSelection,
    reset,
  }
})
