<script setup lang="ts">
/*
 * 成员管理页（/app/members）。
 *
 * 事实与范围说明（依据 docs/frontend/api-inventory.md 4.2.3 与后端实现）：
 *  - 成员列表企业内所有角色可读；添加成员、修改角色、移除成员仅 admin，
 *    agent 进入页面看到只读列表与说明，后端仍会在每个写操作上实时校验；
 *  - 后端禁止 admin 修改或移除自己的成员关系（409），因此界面不给自己
 *    提供改角色/移除入口，并在页面上说明「企业会始终保留至少一名管理员」；
 *  - 移除成员只收回企业访问权，不删除该成员已产生的会话/审批等业务数据；
 *  - 搜索与角色筛选均为**前端本地处理**（后端成员接口无分页与查询参数）；
 *  - 所有写操作先确认再发请求；列表不写入 localStorage，切换企业由
 *    tenantReset 机制清理。
 */
import { computed, onMounted, ref } from 'vue'
import { NAlert, NButton, NEmpty, NInput, NModal, NSelect, NSpin, useMessage } from 'naive-ui'

import MainLayout from '@/layouts/MainLayout.vue'
import RoleTag from '@/components/common/RoleTag.vue'
import AddMemberDialog from '@/components/organization/AddMemberDialog.vue'
import type { MembershipRole } from '@/api/types'
import { useMembersStore } from '@/stores/members'
import { useOrganizationStore } from '@/stores/organization'
import { formatDateTime } from '@/utils/time'

const message = useMessage()
const membersStore = useMembersStore()
const organizationStore = useOrganizationStore()

/** 角色筛选值：all 表示不过滤（本地筛选，不发送给后端）。 */
type RoleFilter = MembershipRole | 'all'

/** 角色筛选选项（本地）。 */
const ROLE_OPTIONS: { label: string; value: RoleFilter }[] = [
  { label: '全部角色', value: 'all' },
  { label: '管理员', value: 'admin' },
  { label: '客服', value: 'agent' },
]

/** 本地搜索词（匹配用户名）。 */
const searchText = ref('')
/** 本地角色筛选。 */
const roleFilter = ref<RoleFilter>('all')
/** 是否显示「添加成员」对话框。 */
const showAddDialog = ref(false)

/** 当前企业名称。 */
const organizationName = computed(
  () => organizationStore.currentOrganization?.name ?? '当前企业',
)

/** 本地搜索 + 角色筛选后的成员列表（纯前端处理）。 */
const filteredMembers = computed(() => {
  const query = searchText.value.trim().toLowerCase()
  return membersStore.members.filter((item) => {
    if (roleFilter.value !== 'all' && item.role !== roleFilter.value) return false
    if (query && !item.username.toLowerCase().includes(query)) return false
    return true
  })
})

/** 成员总数（全部，不随筛选变化）。 */
const totalCount = computed(() => membersStore.members.length)

/** 改角色的二次确认状态：确认前不发送请求。 */
const pendingRoleChange = ref<{ userId: string; username: string; role: MembershipRole } | null>(null)

/** 移除成员的二次确认状态。 */
const pendingRemoval = ref<{ userId: string; username: string } | null>(null)

/** 是否为「我」：后端禁止自我管理，界面同样不提供入口。 */
function isSelf(userId: string): boolean {
  return membersStore.isSelf(userId)
}

/** 成员是否可被管理：admin 且不是自己。 */
function isManageable(userId: string): boolean {
  return membersStore.isAdmin && !isSelf(userId)
}

/** 打开改角色确认框（选择动作本身不直接发请求）。 */
function requestRoleChange(userId: string, username: string, role: MembershipRole): void {
  pendingRoleChange.value = { userId, username, role }
}

/** 打开移除确认框。 */
function requestRemoval(userId: string, username: string): void {
  pendingRemoval.value = { userId, username }
}

/** 执行改角色：按结果给出提示；403/404/409 的语义由 store 分类。 */
async function confirmRoleChange(): Promise<void> {
  const pending = pendingRoleChange.value
  if (!pending) return
  const roleLabel = pending.role === 'admin' ? '管理员' : '客服'
  try {
    const outcome = await membersStore.changeRole(pending.userId, pending.role)
    switch (outcome.result) {
      case 'ok':
        message.success(`已将 ${pending.username} 的角色改为${roleLabel}`)
        break
      case 'forbidden':
        message.error('只有企业管理员可以修改成员角色')
        break
      case 'not-found':
        message.warning('该成员已不在本企业，列表已刷新')
        break
      case 'self-conflict':
        message.warning('不能修改自己的角色，请联系其他管理员')
        break
      default:
        message.error(outcome.message)
    }
  } finally {
    pendingRoleChange.value = null
  }
}

/** 执行移除：成功后提示；被移除者的业务数据保留。 */
async function confirmRemoval(): Promise<void> {
  const pending = pendingRemoval.value
  if (!pending) return
  try {
    const outcome = await membersStore.removeMember(pending.userId)
    switch (outcome.result) {
      case 'ok':
        message.success(`已移除成员 ${pending.username}`)
        break
      case 'forbidden':
        message.error('只有企业管理员可以移除成员')
        break
      case 'not-found':
        message.warning('该成员已不在本企业，列表已刷新')
        break
      case 'self-conflict':
        message.warning('不能移除自己，请联系其他管理员')
        break
      default:
        message.error(outcome.message)
    }
  } finally {
    pendingRemoval.value = null
  }
}

/** 添加成功：提示并清空筛选，保证新成员可见。 */
function onMemberAdded(username: string): void {
  searchText.value = ''
  roleFilter.value = 'all'
  message.success(`已添加成员 ${username}`)
}

/** 手动刷新列表。 */
async function refresh(): Promise<void> {
  await membersStore.loadMembers(true)
}

onMounted(() => {
  // 进入页面加载成员列表（守卫已保证企业上下文存在）
  void membersStore.loadMembers()
})
</script>

<template>
  <MainLayout>
    <div class="members-page" data-test="members-page">
      <!-- 页面标题与当前企业 -->
      <header class="page-head">
        <div>
          <h2 class="page-title">成员管理</h2>
          <p class="page-sub">
            当前企业：<strong>{{ organizationName }}</strong>
            <RoleTag :role="organizationStore.currentRole ?? 'agent'" />
            <span class="count-note">共 {{ totalCount }} 位成员</span>
          </p>
        </div>
        <div class="head-actions">
          <n-button
            data-test="refresh-members"
            :loading="membersStore.loading"
            :disabled="membersStore.loading"
            @click="refresh"
          >
            刷新
          </n-button>
          <n-button
            v-if="membersStore.isAdmin"
            type="primary"
            data-test="open-add-member"
            @click="showAddDialog = true"
          >
            添加成员
          </n-button>
        </div>
      </header>

      <!-- 角色说明：前端隐藏入口只是体验优化，后端仍强制校验 -->
      <n-alert v-if="membersStore.isAdmin" type="info" :show-icon="true" class="role-alert">
        你是本企业管理员，可添加成员、修改角色与移除成员。为保证企业始终有人可管理，
        管理员不能修改或移除自己的成员关系（当前共 {{ membersStore.adminCount }} 位管理员）。
      </n-alert>
      <n-alert v-else type="default" :show-icon="true" class="role-alert">
        你是本企业客服，可查看成员列表；添加成员、修改角色与移除成员需要管理员权限。
      </n-alert>

      <!-- 加载中 -->
      <div v-if="membersStore.loading && totalCount === 0" class="page-loading">
        <n-spin size="large" description="正在加载成员列表…" />
      </div>

      <!-- 加载失败：给出错误与重试入口 -->
      <n-alert
        v-else-if="membersStore.error"
        type="error"
        :show-icon="true"
        class="page-error"
        role="alert"
      >
        <div class="error-row">
          <span>{{ membersStore.error }}</span>
          <n-button size="small" :loading="membersStore.loading" @click="refresh">重试</n-button>
        </div>
      </n-alert>

      <template v-else>
        <!-- 本地搜索与筛选 -->
        <div class="filter-bar">
          <n-input
            v-model:value="searchText"
            class="search-input"
            clearable
            data-test="member-search"
            placeholder="搜索用户名"
            aria-label="搜索成员用户名"
          />
          <n-select
            v-model:value="roleFilter"
            class="role-select"
            :options="ROLE_OPTIONS"
            aria-label="按角色筛选"
          />
          <span class="filter-note">
            本地筛选：显示 {{ filteredMembers.length }} / {{ totalCount }} 位
          </span>
        </div>

        <!-- 空状态 -->
        <n-empty
          v-if="filteredMembers.length === 0"
          class="page-empty"
          :description="
            totalCount === 0
              ? '当前企业还没有成员'
              : '没有符合筛选条件的成员'
          "
        >
          <template v-if="membersStore.isAdmin && totalCount === 0" #extra>
            <n-button type="primary" @click="showAddDialog = true">添加成员</n-button>
          </template>
        </n-empty>

        <!-- 成员列表 -->
        <ul v-else class="member-list">
          <li
            v-for="member in filteredMembers"
            :key="member.user_id"
            class="member-row"
            :data-test="`member-${member.username}`"
          >
            <div class="member-main">
              <div class="member-name-line">
                <span class="member-name">{{ member.username }}</span>
                <span v-if="isSelf(member.user_id)" class="self-mark">我</span>
                <RoleTag :role="member.role" />
              </div>
              <p class="member-meta">
                加入时间：{{ member.created_at ? formatDateTime(member.created_at) : '—' }}
              </p>
            </div>

            <div v-if="isManageable(member.user_id)" class="member-actions">
              <n-select
                class="role-action-select"
                size="small"
                :value="member.role"
                :options="[
                  { label: '设为管理员', value: 'admin' },
                  { label: '设为客服', value: 'agent' },
                ]"
                :disabled="membersStore.isRoleUpdating(member.user_id) || member.role === 'admin'"
                :aria-label="`修改 ${member.username} 的角色`"
                @update:value="(value: MembershipRole) => requestRoleChange(member.user_id, member.username, value)"
              />
              <n-button
                size="small"
                type="error"
                ghost
                :loading="membersStore.isRemoving(member.user_id)"
                :data-test="`remove-${member.username}`"
                @click="requestRemoval(member.user_id, member.username)"
              >
                移除
              </n-button>
            </div>
            <span v-else-if="isSelf(member.user_id)" class="member-self-note">
              不能管理自己的成员关系
            </span>
          </li>
        </ul>
      </template>
    </div>

    <!-- 添加成员对话框 -->
    <AddMemberDialog v-model:show="showAddDialog" @added="onMemberAdded" />

    <!-- 改角色确认：确认前不发送请求 -->
    <n-modal
      :show="pendingRoleChange !== null"
      preset="card"
      title="修改成员角色"
      style="max-width: 420px"
      @update:show="pendingRoleChange = null"
    >
      <p class="confirm-text">
        确定把
        <strong>{{ pendingRoleChange?.username }}</strong>
        的角色改为
        <strong>{{ pendingRoleChange?.role === 'admin' ? '管理员' : '客服' }}</strong>
        吗？
      </p>
      <p class="confirm-hint">
        管理员可审批退款/补偿、管理知识库与成员；改动立即生效。
      </p>
      <div class="confirm-actions">
        <n-button @click="pendingRoleChange = null">取消</n-button>
        <n-button
          type="primary"
          data-test="confirm-role-change"
          :loading="pendingRoleChange ? membersStore.isRoleUpdating(pendingRoleChange.userId) : false"
          @click="confirmRoleChange"
        >
          确认修改
        </n-button>
      </div>
    </n-modal>

    <!-- 移除确认：确认前不发送请求 -->
    <n-modal
      :show="pendingRemoval !== null"
      preset="card"
      title="移除成员"
      style="max-width: 420px"
      @update:show="pendingRemoval = null"
    >
      <p class="confirm-text">
        确定把 <strong>{{ pendingRemoval?.username }}</strong> 移出本企业吗？
      </p>
      <p class="confirm-hint">
        移除后该成员立即失去本企业的数据访问权限；其已产生的会话与审批记录会保留。
        如需重新加入，可用「添加成员」再次添加。
      </p>
      <div class="confirm-actions">
        <n-button @click="pendingRemoval = null">取消</n-button>
        <n-button
          type="error"
          data-test="confirm-remove-member"
          :loading="pendingRemoval ? membersStore.isRemoving(pendingRemoval.userId) : false"
          @click="confirmRemoval"
        >
          确认移除
        </n-button>
      </div>
    </n-modal>
  </MainLayout>
</template>

<style scoped>
.members-page {
  max-width: 980px;
  margin: 0 auto;
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-space-4);
  flex-wrap: wrap;
  margin-bottom: var(--sp-space-4);
}

.page-title {
  margin: 0 0 var(--sp-space-1);
  font-size: var(--sp-font-size-xl);
  font-weight: 600;
}

.page-sub {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  flex-wrap: wrap;
  margin: 0;
  color: var(--sp-color-text-2);
}

.count-note {
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.head-actions {
  display: flex;
  gap: var(--sp-space-2);
}

.role-alert {
  margin-bottom: var(--sp-space-4);
}

.page-loading {
  display: flex;
  justify-content: center;
  padding: var(--sp-space-10) 0;
}

.page-error {
  margin-bottom: var(--sp-space-4);
}

.error-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-3);
}

.filter-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-space-3);
  flex-wrap: wrap;
  margin-bottom: var(--sp-space-3);
}

.search-input {
  max-width: 260px;
}

.role-select {
  width: 140px;
}

.filter-note {
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.page-empty {
  margin-top: var(--sp-space-8);
}

.member-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-space-2);
}

.member-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-4);
  padding: var(--sp-space-3) var(--sp-space-4);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-md);
  background: var(--sp-color-bg-card);
}

.member-main {
  min-width: 0;
}

.member-name-line {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  flex-wrap: wrap;
}

.member-name {
  font-weight: 600;
}

.self-mark {
  padding: 0 var(--sp-space-1);
  border-radius: var(--sp-radius-sm);
  background: var(--sp-color-bg-hover);
  color: var(--sp-color-text-2);
  font-size: var(--sp-font-size-xs);
}

.member-meta {
  margin: var(--sp-space-1) 0 0;
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
}

.member-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-space-2);
  flex-shrink: 0;
}

.role-action-select {
  width: 130px;
}

.member-self-note {
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
  flex-shrink: 0;
}

.confirm-text {
  margin: 0 0 var(--sp-space-2);
}

.confirm-hint {
  margin: 0 0 var(--sp-space-4);
  color: var(--sp-color-text-3);
  font-size: var(--sp-font-size-sm);
  line-height: 1.6;
}

.confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-space-3);
}

@media (max-width: 560px) {
  .member-row {
    flex-direction: column;
    align-items: flex-start;
  }

  .search-input,
  .role-select {
    max-width: none;
    width: 100%;
  }
}
</style>
