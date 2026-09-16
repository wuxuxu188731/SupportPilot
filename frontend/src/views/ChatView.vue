<script setup lang="ts">
/*
 * 客服对话页面（/app/chat 与 /app/chat/:conversationId）。
 *
 * 行为说明：
 *  - /app/chat：会话列表 + 空状态/引导（新建或从列表选择）；
 *  - /app/chat/:conversationId：加载服务端历史并进入对话；
 *  - 会话 id 必须与当前企业绑定：收到 404 时清理失效选择、刷新列表、
 *    回到 /app/chat 并显示「会话不存在或已不可访问」提示；
 *  - 切换企业/退出登录：tenantReset 清理聊天状态，本页监听企业变化后
 *    回到会话列表重新加载，防止旧企业数据污染新企业页面；
 *  - 桌面端左侧会话列表常驻；窄屏使用抽屉切换会话列表；
 *  - 引用「查看原文位置」：桌面（>=1280px）在右侧内嵌正文面板同屏并排，
 *    窄屏降级为整页跳转到知识库文档详情页；面板状态只存在本页局部状态，
 *    不写 localStorage，切换企业时随 tenantReset 关闭。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NButton, NDrawer, NDrawerContent, useMessage } from 'naive-ui'

import MainLayout from '@/layouts/MainLayout.vue'
import AppIcon from '@/components/common/AppIcon.vue'
import ConversationList from '@/components/chat/ConversationList.vue'
import MessageList from '@/components/chat/MessageList.vue'
import MessageComposer from '@/components/chat/MessageComposer.vue'
import NewConversationDialog from '@/components/chat/NewConversationDialog.vue'
import ConversationSettingsDialog from '@/components/chat/ConversationSettingsDialog.vue'
import DocumentContentPanel from '@/components/chat/DocumentContentPanel.vue'
import type { CitationTarget } from '@/api/types'
import { useChatStore } from '@/stores/chat'
import { useOrganizationStore } from '@/stores/organization'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const chatStore = useChatStore()
const organizationStore = useOrganizationStore()

/** 发送框草稿：发送成功才清空，失败保留以便修改重发。 */
const draft = ref('')
/** 新建会话对话框显隐。 */
const showCreateDialog = ref(false)
/** 会话设置对话框显隐。 */
const showSettingsDialog = ref(false)
/** 窄屏会话列表抽屉显隐。 */
const showMobileList = ref(false)

/** 同屏并排正文面板的最小宽度（低于该宽度走整页降级，避免把对话区压坏）。 */
const DOC_PANEL_MIN_WIDTH = 1280
const DOC_PANEL_MEDIA_QUERY = `(min-width: ${DOC_PANEL_MIN_WIDTH}px)`

/** 是否允许同屏并排（桌面 >= 1280px）。 */
const canShowDocPanel = ref(false)

/** 当前正文面板的定位目标；null 表示面板关闭。 */
const docTarget = ref<CitationTarget | null>(null)

/** 媒体查询：只监听宽度变化，不在滚动/尺寸变化时做任何重排。 */
let docPanelMedia: MediaQueryList | null = null

/** 按媒体查询同步"是否允许并排"。 */
function syncDocPanelMedia(event?: MediaQueryListEvent): void {
  canShowDocPanel.value = event === undefined ? (docPanelMedia?.matches ?? false) : event.matches
}

/** 面板是否真正显示：窄屏即便有目标也不并排（走整页降级）。 */
const showDocPanel = computed(() => canShowDocPanel.value && docTarget.value !== null)

/** 路由参数中的会话 id（无参路由为 null）。 */
const routeConversationId = computed(() => {
  const value = route.params.conversationId
  return typeof value === 'string' && value.length > 0 ? value : null
})

/** 历史加载失败时提供重试（重新打开当前会话）。 */
function retryHistory(): void {
  if (chatStore.currentConversationId) {
    void chatStore.openConversation(chatStore.currentConversationId)
  }
}

/** 处理进入会话的返回值：404 时回列表页并展示提示。 */
async function handleOpenResult(result: Awaited<ReturnType<typeof chatStore.openConversation>>): Promise<void> {
  if (result === 'not-found') {
    // store 已清理失效选择并刷新列表；这里回到空状态路由展示提示
    await router.replace({ name: 'chat' })
  }
}

/** 打开（或切换）路由指定会话。 */
async function openConversationById(conversationId: string): Promise<void> {
  await chatStore.ensureConversationsLoaded()
  const result = await chatStore.openConversation(conversationId)
  await handleOpenResult(result)
}

/** 会话列表选择：导航到该会话（空态/抽屉共用）。 */
function onSelectConversation(conversationId: string): void {
  showMobileList.value = false
  void router.push({ name: 'chat-detail', params: { conversationId } })
}

/** 新建会话提交：创建成功后进入新会话。 */
async function onConversationCreated(systemPrompt: string): Promise<void> {
  const conversationId = await chatStore.createConversation(systemPrompt)
  showCreateDialog.value = false
  if (conversationId) {
    showMobileList.value = false
    await router.push({ name: 'chat-detail', params: { conversationId } })
  } else if (chatStore.displayError) {
    message.error(chatStore.displayError)
  }
}

/** 发送消息：成功才清空输入；失败保留输入并展示失败气泡。 */
async function onSend(text: string): Promise<void> {
  const result = await chatStore.sendMessage(text)
  if (result === 'sent') {
    draft.value = ''
  } else if (result === 'not-found') {
    await router.replace({ name: 'chat' })
  } else if (result === 'error' && chatStore.displayError) {
    message.error(chatStore.displayError)
  }
}

/** 保存系统提示词：成功后关闭对话框并给出反馈。 */
async function onSaveSystemPrompt(prompt: string): Promise<void> {
  const ok = await chatStore.updateSystemPrompt(prompt)
  if (ok) {
    showSettingsDialog.value = false
    message.success('会话系统提示词已保存')
  } else if (chatStore.displayError) {
    message.error(chatStore.displayError)
  }
}

/** 待审批卡片「查看审批」：跳转审批详情（结构化 approval_id，不解析自然语言）。 */
function onOpenApproval(approvalId: string): void {
  void router.push({ name: 'approval-detail', params: { approvalId } })
}

/**
 * 引用卡片「查看原文位置」：打开来源文档正文并定位到引用区间。
 *
 * 窄屏（< 1280px）不做挤压式并排，改为整页跳转到知识库文档详情页并带上定位参数，
 * 由该页复用同一个查看器组件（设计稿 4.4 的窄屏降级）。
 */
function onOpenDocument(target: CitationTarget): void {
  if (!canShowDocPanel.value) {
    // 窄屏：整页跳转，保留版本与偏移，让详情页打开时自动定位。
    // 额外带上来源标记：详情页据此提供「返回对话」，用户关掉正文弹窗后
    // 能直接回到刚才这条会话，而不是被迫退出对话页再重新点进来。
    const conversationId = chatStore.currentConversationId
    void router.push({
      name: 'knowledge-detail',
      params: { documentId: target.documentId },
      query: {
        versionId: target.versionId,
        ...(target.startOffset == null ? {} : { start: String(target.startOffset) }),
        ...(target.endOffset == null ? {} : { end: String(target.endOffset) }),
        ...(target.headingPath == null ? {} : { heading: target.headingPath }),
        from: 'chat',
        ...(conversationId ? { conversationId } : {}),
      },
    })
    return
  }
  // 打开面板不滚动对话区（保持用户阅读位置），也不改写会话路由
  docTarget.value = target
}

/** 关闭正文面板：不重载会话、不改路由，只收起面板。 */
function onCloseDocument(): void {
  docTarget.value = null
}

/** 关闭一次性提示。 */
function onCloseNotice(): void {
  chatStore.clearNotice()
}

// 路由变化：会话参数出现/消失/切换都要同步聊天状态
watch(routeConversationId, (conversationId) => {
  if (conversationId) {
    void openConversationById(conversationId)
  } else {
    // 无会话参数：只展示会话列表与空状态引导
    chatStore.showConversationList()
    void chatStore.ensureConversationsLoaded()
  }
})

// 企业切换：聊天状态已由 tenantReset 清理，这里回到会话列表并重新加载，
// 防止旧企业的会话/消息残留到新企业页面；正文面板必须一并关闭
watch(
  () => organizationStore.currentOrganizationId,
  (organizationId, previous) => {
    if (organizationId && organizationId !== previous) {
      docTarget.value = null
      if (routeConversationId.value) {
        void router.replace({ name: 'chat' })
      }
      void chatStore.loadConversations(true)
    }
  },
)

// 挂载时按当前路由初始化（刷新 / 深链直达 /app/chat/:id 场景）
onMounted(() => {
  void chatStore.ensureConversationsLoaded()
  if (routeConversationId.value) {
    void openConversationById(routeConversationId.value)
  }
  // 监听宽度变化：只在跨越 1280px 时改变"能否并排"，不做布局重排
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    docPanelMedia = window.matchMedia(DOC_PANEL_MEDIA_QUERY)
    syncDocPanelMedia()
    docPanelMedia.addEventListener('change', syncDocPanelMedia)
  }
})

onBeforeUnmount(() => {
  docPanelMedia?.removeEventListener('change', syncDocPanelMedia)
  docPanelMedia = null
})
</script>

<template>
  <MainLayout>
    <div class="chat-page" data-test="chat-page">
      <!-- 桌面端会话列表 -->
      <aside class="chat-side" aria-label="会话列表面板">
        <ConversationList
          :conversations="chatStore.conversations"
          :loading="chatStore.listLoading"
          :error="chatStore.listError"
          :active-conversation-id="chatStore.currentConversationId"
          :has-more="chatStore.listHasMore"
          :loading-more="chatStore.listLoading && chatStore.conversations.length > 0"
          :creating="chatStore.creating"
          @select="onSelectConversation"
          @create="showCreateDialog = true"
          @retry="chatStore.loadConversations(true)"
          @load-more="chatStore.loadMoreConversations"
        />
      </aside>

      <!-- 主对话区 -->
      <section class="chat-main">
        <!-- 会话头部 -->
        <header v-if="chatStore.currentConversationId" class="chat-header">
          <n-button
            quaternary
            class="mobile-list-toggle"
            aria-label="打开会话列表"
            @click="showMobileList = true"
          >
            会话列表
          </n-button>
          <h2 class="chat-title" :title="chatStore.currentConversationTitle">
            {{ chatStore.currentConversationTitle }}
          </h2>
          <n-button
            quaternary
            type="primary"
            class="settings-toggle"
            aria-label="会话设置"
            data-test="open-settings"
            @click="showSettingsDialog = true"
          >
            会话设置
          </n-button>
        </header>

        <!-- 空状态：选择/新建引导 -->
        <div v-if="!chatStore.currentConversationId" class="chat-empty" data-test="chat-empty">
          <n-alert
            v-if="chatStore.notice"
            type="warning"
            closable
            class="notice-alert"
            @close="onCloseNotice"
          >
            {{ chatStore.notice }}
          </n-alert>
          <div class="chat-welcome-mark"><AppIcon name="spark" :size="38" /></div>
          <span class="chat-eyebrow">SUPPORTPILOT · 你的 AI 客服伙伴</span>
          <h1 class="chat-welcome-title">今天，有什么可以一起解决？</h1>
          <p class="chat-welcome-desc">选择一个会话，或新建会话开始客服对话</p>
          <n-button type="primary" size="large" data-test="start-new-conversation" @click="showCreateDialog = true">
            新建会话 <AppIcon name="arrow" :size="17" class="create-arrow" />
          </n-button>
          <div class="chat-capabilities">
            <div><AppIcon name="knowledge" /><strong>有据可循</strong><span>结合知识库，查找相关依据</span></div>
            <div><AppIcon name="chat" /><strong>连贯沟通</strong><span>保留对话上下文，持续跟进</span></div>
            <div><AppIcon name="shield" /><strong>协同处理</strong><span>关键提案，交由人工审批</span></div>
          </div>
          <p v-if="chatStore.conversations.length > 0" class="empty-tip">
            也可在左侧会话列表中选择已有会话继续对话。
          </p>
        </div>

        <!-- 对话区 -->
        <template v-else>
          <MessageList
            :messages="chatStore.messages"
            :history-loading="chatStore.historyLoading"
            :empty-conversation="
              chatStore.messages.length === 0 &&
                !chatStore.historyLoading &&
                chatStore.historyLoadedConversationId === chatStore.currentConversationId
            "
            @open-approval="onOpenApproval"
            @open-document="onOpenDocument"
          />

          <!-- 历史加载失败：展示错误与重试 -->
          <div v-if="chatStore.historyError" class="history-error" role="alert">
            <n-alert type="error" :show-icon="true">
              <template #header>历史消息加载失败</template>
              <div class="history-error-body">
                <span>{{ chatStore.historyError }}</span>
                <n-button size="small" @click="retryHistory">重试</n-button>
              </div>
            </n-alert>
          </div>

          <footer class="chat-composer">
            <MessageComposer
              v-model:value="draft"
              :sending="chatStore.sending"
              :disabled="chatStore.historyLoading || chatStore.historyError !== null || chatStore.historyLoadedConversationId !== chatStore.currentConversationId"
              @send="onSend"
            />
          </footer>
        </template>
      </section>

      <!-- 桌面端：正文面板与对话区同屏并排（不是遮罩抽屉，避免盖住对话） -->
      <DocumentContentPanel
        v-if="showDocPanel && docTarget"
        :target="docTarget"
        @close="onCloseDocument"
      />

      <!-- 窄屏：会话列表抽屉 -->
      <n-drawer v-model:show="showMobileList" placement="left" :width="300">
        <n-drawer-content title="会话列表" closable>
          <ConversationList
            :conversations="chatStore.conversations"
            :loading="chatStore.listLoading"
            :error="chatStore.listError"
            :active-conversation-id="chatStore.currentConversationId"
            :has-more="chatStore.listHasMore"
            :loading-more="chatStore.listLoading && chatStore.conversations.length > 0"
            :creating="chatStore.creating"
            @select="onSelectConversation"
            @create="showCreateDialog = true"
            @retry="chatStore.loadConversations(true)"
            @load-more="chatStore.loadMoreConversations"
          />
        </n-drawer-content>
      </n-drawer>

      <!-- 新建会话对话框 -->
      <NewConversationDialog
        v-model:show="showCreateDialog"
        :creating="chatStore.creating"
        @create="onConversationCreated"
      />

      <!-- 会话设置对话框 -->
      <ConversationSettingsDialog
        v-model:show="showSettingsDialog"
        :system-prompt="chatStore.systemPrompt"
        :updating="chatStore.promptUpdating"
        @save="onSaveSystemPrompt"
      />
    </div>
  </MainLayout>
</template>

<style scoped>
.chat-welcome-mark { display: grid; place-items: center; width: 80px; height: 80px; margin-bottom: 6px; border: 1px solid #d7e3c9; border-radius: 24px; background: #edf3e5; color: #537b49; transform: rotate(-7deg); }
.chat-eyebrow { margin-top: 8px; font-size: 10px; letter-spacing: 1.5px; color: var(--sp-color-text-3); }
.chat-welcome-title { margin: 0; font-size: clamp(21px, 2vw, 28px); font-weight: 500; letter-spacing: -.6px; text-align: center; }
.chat-welcome-desc { margin: 0 0 10px; color: var(--sp-color-text-3); font-size: 13px; text-align: center; }
.create-arrow { margin-left: 22px; }
.chat-capabilities { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); width: min(100%, 620px); gap: 12px; margin-top: 36px; }
.chat-capabilities > div { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; padding: 18px; border: 1px solid var(--sp-color-border); border-radius: 10px; background: #fafbf7; color: #65825c; }
.chat-capabilities strong { font-size: 13px; font-weight: 500; color: var(--sp-color-text-1); }
.chat-capabilities span { font-size: 11px; line-height: 1.8; color: var(--sp-color-text-3); }
.chat-page {
  display: flex;
  gap: var(--sp-space-5);
  height: calc(100vh - 60px - 48px);
  min-height: 480px;
}

/* —— 会话列表（桌面端） —— */
.chat-side {
  display: none;
  width: 280px;
  flex-shrink: 0;
  background: var(--sp-color-bg-card);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-lg);
  padding: var(--sp-space-4) var(--sp-space-3);
  overflow: hidden;
}

/* —— 主对话区 —— */
.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--sp-color-bg-card);
  border: 1px solid var(--sp-color-border);
  border-radius: var(--sp-radius-lg);
  overflow: hidden;
}

.chat-header {
  display: flex;
  align-items: center;
  gap: var(--sp-space-3);
  padding: var(--sp-space-3) var(--sp-space-5);
  border-bottom: 1px solid var(--sp-color-border);
  flex-shrink: 0;
}

.chat-title {
  margin: 0;
  flex: 1;
  min-width: 0;
  font-size: var(--sp-font-size-lg);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings-toggle {
  flex-shrink: 0;
}

.chat-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-space-4);
  padding: var(--sp-space-6);
  color: var(--sp-color-text-2);
}

.notice-alert {
  max-width: 480px;
  width: 100%;
}

.empty-tip {
  margin: 0;
  font-size: var(--sp-font-size-sm);
  color: var(--sp-color-text-3);
}

.history-error {
  padding: 0 var(--sp-space-4) var(--sp-space-2);
  flex-shrink: 0;
}

.history-error-body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-space-3);
}

.chat-composer {
  flex-shrink: 0;
  padding: var(--sp-space-3) var(--sp-space-5) var(--sp-space-5);
  border-top: 1px solid var(--sp-color-border);
}

.mobile-list-toggle {
  display: inline-flex;
}

/* —— 桌面端显示左侧列表、隐藏移动端切换 —— */
@media (min-width: 960px) {
  .chat-side {
    display: block;
  }

  .mobile-list-toggle {
    display: none;
  }
}

/* —— 窄屏：整页自然滚动，输入区不遮挡消息 —— */
@media (max-width: 959px) {
  .chat-page {
    height: auto;
    min-height: calc(100vh - 160px);
    flex-direction: column;
  }

  .chat-main {
    overflow: visible;
  }

  .chat-header {
    flex-wrap: wrap;
  }
}
</style>
