<script setup lang="ts">
/*
 * 会话列表组件：展示当前企业、当前用户的会话，支持选择、新建、
 * 错误重试与「加载更多」。
 *
 * 说明：
 *  - 后端不返回会话总数：通过「最近一页是否拉满」推断是否还有更多；
 *  - 更新时间按本地时间格式化（see utils/time.ts）。
 */
import { NButton, NEmpty, NSpin } from 'naive-ui'

import type { ConversationListItem } from '@/api/types'
import { formatConversationTime } from '@/utils/time'

defineProps<{
  /** 会话列表数据（更新时间倒序） */
  conversations: ConversationListItem[]
  /** 列表加载中（首屏/加载更多共用） */
  loading: boolean
  /** 列表加载错误消息；null 表示无错误 */
  error: string | null
  /** 当前高亮的会话 id；null 表示没有选中 */
  activeConversationId: string | null
  /** 是否还存在下一页（供「加载更多」按钮显隐） */
  hasMore: boolean
  /** 是否正在加载更多（区别于首屏 loading，用于按钮转圈） */
  loadingMore: boolean
  /** 是否正在创建新会话（新建按钮 loading） */
  creating: boolean
}>()

const emit = defineEmits<{
  /** 选择某个会话 */
  select: [conversationId: string]
  /** 打开新建会话对话框 */
  create: []
  /** 首屏加载失败后的重试 */
  retry: []
  /** 加载更多会话 */
  loadMore: []
}>()

/** 会话卡片键盘可达：按 Enter/空格等效点击。 */
function onItemKeydown(event: KeyboardEvent, conversationId: string): void {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    emit('select', conversationId)
  }
}
</script>

<template>
  <div class="conversation-list">
    <div class="list-header">
      <span class="list-title">会话</span>
      <n-button
        size="small"
        type="primary"
        secondary
        :loading="creating"
        :disabled="creating"
        aria-label="新建会话"
        data-test="new-conversation"
        @click="emit('create')"
      >
        + 新建会话
      </n-button>
    </div>

    <!-- 错误状态：展示原因并提供重试 -->
    <div v-if="error" class="state-block" role="alert">
      <p class="state-error">{{ error }}</p>
      <n-button size="small" @click="emit('retry')">重试</n-button>
    </div>

    <!-- 首屏加载态 -->
    <div v-else-if="loading && conversations.length === 0" class="state-block" aria-live="polite">
      <n-spin size="small" />
      <p class="state-hint">会话加载中…</p>
    </div>

    <!-- 空列表 -->
    <div v-else-if="conversations.length === 0" class="state-block">
      <n-empty description="暂无会话" size="small">
        <template #extra>
          <n-button size="small" type="primary" @click="emit('create')">开始第一个会话</n-button>
        </template>
      </n-empty>
    </div>

    <!-- 会话列表 -->
    <ul v-else class="list-items" role="listbox" aria-label="会话列表">
      <li
        v-for="item in conversations"
        :key="item.conversation_id"
        role="option"
        :aria-selected="item.conversation_id === activeConversationId"
      >
        <button
          type="button"
          class="list-item"
          :class="{ active: item.conversation_id === activeConversationId }"
          :title="item.title"
          @click="emit('select', item.conversation_id)"
          @keydown="onItemKeydown($event, item.conversation_id)"
        >
          <span class="item-title">{{ item.title }}</span>
          <span class="item-time">{{ formatConversationTime(item.updated_at) }}</span>
        </button>
      </li>
    </ul>

    <!-- 加载更多：只显示按钮，不假装后端返回了总数 -->
    <div v-if="conversations.length > 0 && hasMore && !error" class="load-more">
      <n-button
        size="small"
        quaternary
        :loading="loadingMore"
        :disabled="loadingMore"
        @click="emit('loadMore')"
      >
        加载更多
      </n-button>
    </div>
  </div>
</template>

<style scoped>
.conversation-list {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}

.list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-2);
  padding: 0 var(--sp-space-2) var(--sp-space-3);
}

.list-title {
  font-weight: 600;
  font-size: var(--sp-font-size-lg);
  color: var(--sp-color-text-1);
}

.state-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-space-3);
  padding: var(--sp-space-8) var(--sp-space-4);
  text-align: center;
}

.state-hint {
  margin: 0;
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-3);
}

.state-error {
  margin: 0;
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-error);
  word-break: break-all;
}

.list-items {
  list-style: none;
  margin: 0;
  padding: 0 var(--sp-space-2);
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}

.list-items li {
  margin-bottom: var(--sp-space-1);
}

.list-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  padding: var(--sp-space-2) var(--sp-space-3);
  border: 1px solid transparent;
  border-radius: var(--sp-radius-md);
  background: transparent;
  color: var(--sp-color-text-1);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.list-item:hover {
  background: var(--sp-color-bg-hover);
}

.list-item:focus-visible {
  outline: 2px solid var(--sp-color-primary);
  outline-offset: -2px;
}

.list-item.active {
  background: var(--sp-color-primary-weak);
  border-color: var(--sp-color-primary);
}

.item-title {
  width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--sp-font-size-sm);
  font-weight: 500;
}

.item-time {
  font-size: var(--sp-font-size-xs);
  color: var(--sp-color-text-3);
}

.load-more {
  display: flex;
  justify-content: center;
  padding-top: var(--sp-space-2);
}
</style>
