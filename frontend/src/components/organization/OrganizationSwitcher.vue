<script setup lang="ts">
/*
 * 企业切换器（主界面顶栏）：显示当前企业，点击展开企业列表进行切换。
 *  - 选项来自企业 Store（后端实时列表），角色以文字后缀提示；
 *  - 切换成功/失败都有明确反馈；失败（企业已失效）时提示刷新列表；
 *  - 切换会触发租户缓存清理（见 organization store 的 select）。
 */
import { computed } from 'vue'
import { NDropdown, NButton, useMessage } from 'naive-ui'

import { useOrganizationStore } from '@/stores/organization'

const message = useMessage()
const organizationStore = useOrganizationStore()

/** 角色中文名（仅展示用途）。 */
function roleLabel(role: 'admin' | 'agent'): string {
  return role === 'admin' ? '管理员' : '客服'
}

/** 下拉选项：企业名（我的角色）。 */
const options = computed(() =>
  organizationStore.organizations.map((item) => ({
    label: `${item.name}（${roleLabel(item.role)}）`,
    key: item.organization_id,
  })),
)

/** 切换企业并给出反馈。 */
async function handleSelect(key: string): Promise<void> {
  const target = organizationStore.organizations.find((item) => item.organization_id === key)
  const ok = await organizationStore.select(key)
  if (!ok) {
    // 目标企业已不在列表中（可能刚被移出）：刷新列表后提示
    message.warning('企业信息已变化，请刷新后重试')
    await organizationStore.load(true)
    return
  }
  message.success(`已切换到「${target?.name ?? key}」`)
}
</script>

<template>
  <n-dropdown
    trigger="click"
    :options="options"
    :show-arrow="true"
    @select="handleSelect"
  >
    <n-button quaternary aria-label="切换当前企业" class="switcher-button">
      <span class="switcher-name">{{ organizationStore.currentOrganization?.name ?? '未选择企业' }}</span>
      <span class="switcher-caret" aria-hidden="true">▾</span>
    </n-button>
  </n-dropdown>
</template>

<style scoped>
.switcher-button {
  max-width: 260px;
}

.switcher-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.switcher-caret {
  margin-left: var(--sp-space-1);
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}
</style>
