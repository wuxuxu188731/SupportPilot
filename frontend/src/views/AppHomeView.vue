<script setup lang="ts">
/** 工作台首页：业务快捷入口与服务引导，企业和角色来自当前登录上下文。 */
import { NButton } from 'naive-ui'
import { useRouter } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'
import AppIcon from '@/components/common/AppIcon.vue'
import { useAuthStore } from '@/stores/auth'
import { useOrganizationStore } from '@/stores/organization'

const router = useRouter()
const authStore = useAuthStore()
const organizationStore = useOrganizationStore()

/** 功能入口使用已有路由，不额外请求或推测业务统计。 */
const modules = [
  { name: 'chat', title: '客服对话', caption: '与 AI 一起，找到更好的回答', detail: '多轮沟通、知识引用与处理记录，让每一次回复都有依据。', label: '开始对话', number: '01' },
  { name: 'approvals', title: '审批中心', caption: '关键决定，由你掌握', detail: '集中查看待审批提案，审核处理方案，跟进执行状态。', label: '查看审批', number: '02' },
  { name: 'knowledge', title: '知识库', caption: '让团队经验成为服务底气', detail: '沉淀售后政策与常见问题，为客服提供可靠的知识来源。', label: '浏览知识库', number: '03' },
  { name: 'members', title: '成员管理', caption: '好的服务，来自默契协作', detail: '查看团队成员与角色，让每位同事各司其职、有序协作。', label: '查看成员', number: '04' },
]
</script>

<template>
  <MainLayout>
    <section class="home-page">
      <header class="home-heading">
        <div><p class="eyebrow">你的服务，从这里开始</p><h1>工作台概览</h1></div>
        <span class="workspace-pill"><AppIcon name="building" :size="16" /> {{ organizationStore.currentOrganization?.name }}</span>
      </header>

      <section class="welcome-card">
        <div class="welcome-copy">
          <span class="hero-label"><span /> 专注沟通，从容服务</span>
          <h2>你好，{{ authStore.currentUser?.username }}<br>让每一次服务，<em>更进一步。</em></h2>
          <p>把繁杂留给工具，把用心留给客户。<br>你的 AI 客服伙伴，已在这里等你。</p>
          <n-button type="primary" size="large" data-test="go-chat" @click="router.push({ name: 'chat' })">进入客服对话 <AppIcon name="arrow" :size="18" /></n-button>
        </div>
        <div class="hero-art" aria-hidden="true">
          <div class="orbit orbit-one" /><div class="orbit orbit-two" /><div class="orbit orbit-three" />
          <div class="art-star star-one">✦</div><div class="art-star star-two">✦</div>
          <div class="art-note note-top"><span class="note-icon"><AppIcon name="knowledge" /></span><div>知识随时可用<small>让回答有据可循</small></div><span class="note-check">✓</span></div>
          <div class="pilot-symbol"><AppIcon name="spark" :size="66" /></div>
          <div class="art-note note-bottom"><span class="note-icon"><AppIcon name="shield" /></span><div>人机协同服务<small>让每个决定更安心</small></div><span class="note-check">✓</span></div>
          <span class="art-caption">SUPPORT, WITH A HUMAN TOUCH.</span>
        </div>
      </section>

      <div class="section-heading"><h2>开始今天的工作</h2><span>一个工作台，连接服务的每一步</span></div>
      <div class="module-grid">
        <router-link v-for="module in modules" :key="module.name" :to="{ name: module.name }" class="module-card" :class="'module-' + module.name">
          <div class="module-top"><span class="module-icon"><AppIcon :name="module.name" :size="24" /></span><span class="module-number">{{ module.number }}</span></div>
          <h3>{{ module.title }}</h3><p class="module-caption">{{ module.caption }}</p><p class="module-detail">{{ module.detail }}</p>
          <span class="module-link">{{ module.label }}<AppIcon name="arrow" :size="17" /></span>
        </router-link>
      </div>

      <section class="workflow-panel">
        <div class="workflow-intro"><span class="eyebrow">协作有序，服务有温度</span><h2>让服务形成闭环</h2><p>从问题到解决，每一步都清晰。</p></div>
        <ol class="workflow-steps"><li><span>1</span><div><h3>发起对话</h3><p>描述客户问题与诉求</p></div></li><li><span>2</span><div><h3>获取方案</h3><p>结合知识，生成处理建议</p></div></li><li><span>3</span><div><h3>审核与跟进</h3><p>确认提案，查看执行结果</p></div></li></ol>
      </section>
      <footer class="home-footer"><span>SupportPilot · 让每一次服务更有价值</span><span>当前身份：{{ organizationStore.currentRole === 'admin' ? '企业管理员' : '客服成员' }}</span></footer>
    </section>
  </MainLayout>
</template>

<style scoped>
.home-page { max-width: 1280px; margin: 0 auto; }
.home-heading, .section-heading, .home-footer { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.home-heading { margin-bottom: 28px; }
.eyebrow { margin: 0 0 6px; color: var(--sp-color-text-3); font-size: 12px; letter-spacing: 1.5px; }
h1 { margin: 0; font-size: 28px; font-weight: 600; letter-spacing: -1px; }
.workspace-pill { display: inline-flex; align-items: center; gap: 8px; max-width: 40%; overflow-wrap: anywhere; padding: 7px 13px; border: 1px solid var(--sp-color-border); border-radius: 8px; background: #fff; color: var(--sp-color-text-2); font-size: 12px; }
.welcome-card { position: relative; display: grid; grid-template-columns: 1.2fr 1fr; overflow: hidden; min-height: 330px; border: 1px solid #dce5da; border-radius: 18px; background: #eaf0e6; }
.welcome-copy { position: relative; z-index: 1; padding: 36px 40px; }
.hero-label { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; color: #416347; }
.hero-label > span { width: 6px; height: 6px; background: #4c7a50; border-radius: 50%; }
.welcome-copy h2 { margin: 18px 0 14px; font-size: clamp(25px, 2.3vw, 36px); font-weight: 500; line-height: 1.55; letter-spacing: -1px; overflow-wrap: anywhere; }
.welcome-copy em { font-style: normal; color: var(--sp-color-primary); }
.welcome-copy p { margin: 0 0 24px; color: #60705f; font-size: 13px; line-height: 1.9; }
.welcome-copy .app-icon { margin-left: 16px; }
.hero-art { position: relative; min-width: 0; overflow: hidden; }
.orbit { position: absolute; left: 50%; top: 50%; width: 260px; height: 260px; border: 1px solid #c9d7c3; border-radius: 50%; transform: translate(-50%, -50%); }
.orbit-two { width: 390px; height: 390px; opacity: .65; }
.orbit-three { width: 510px; height: 510px; opacity: .35; }
.pilot-symbol { position: absolute; left: 50%; top: 50%; display: grid; place-items: center; width: 112px; height: 112px; border: 8px solid #ffffff55; border-radius: 30px; color: #f2f6d9; background: #32664f; box-shadow: 0 16px 36px #1e46362b; transform: translate(-50%, -50%) rotate(-9deg); }
.art-note { position: absolute; z-index: 1; display: flex; align-items: center; gap: 12px; padding: 14px 16px; border: 1px solid #fff; background: #ffffffeb; border-radius: 12px; box-shadow: 0 8px 24px #2b503610; font-size: 13px; }
.note-top { top: 46px; left: 8%; transform: rotate(-5deg); }
.note-bottom { bottom: 47px; right: 5%; transform: rotate(4deg); }
.note-icon { display: grid; place-items: center; width: 36px; height: 36px; background: #edf3e9; border-radius: 9px; color: #42694a; }
.art-note small { display: block; margin-top: 3px; font-size: 10px; color: #657365; }
.note-check { margin-left: 15px; color: #5e8665; }
.art-star { position: absolute; font-size: 25px; color: #70916a; }
.star-one { top: 29%; right: 13%; }.star-two { bottom: 24%; left: 16%; font-size: 17px; }
.art-caption { position: absolute; bottom: 17px; width: 100%; text-align: center; font-size: 8px; letter-spacing: 2.4px; color: #65765d; }
.section-heading { margin: 32px 0 17px; }.section-heading h2, .workflow-intro h2 { margin: 0; font-size: 18px; font-weight: 600; }
.section-heading > span { color: var(--sp-color-text-3); font-size: 12px; }
.module-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.module-card { padding: 23px; border: 1px solid var(--sp-color-border); border-radius: 12px; color: inherit; text-decoration: none; background: #fff; transition: transform .2s, box-shadow .2s, border-color .2s; }
.module-card:hover { transform: translateY(-4px); border-color: #a2b7a4; box-shadow: var(--sp-shadow-card); }
.module-top { display: flex; justify-content: space-between; align-items: center; }.module-icon { display: grid; place-items: center; width: 44px; height: 44px; border-radius: 12px; background: #eaf1e8; color: #3c6c4f; }
.module-approvals .module-icon { background: #faf0df; color: #a77b38; }.module-knowledge .module-icon { background: #eaf0f6; color: #5c7c97; }.module-members .module-icon { background: #f2edf4; color: #8e7297; }
.module-number { font-family: var(--sp-font-family-mono); font-size: 12px; color: #8b9287; }
.module-card h3 { margin: 21px 0 5px; font-size: 17px; font-weight: 600; }.module-caption { margin: 0; font-size: 12px; color: var(--sp-color-text-2); }.module-detail { margin: 12px 0 22px; font-size: 12px; line-height: 1.9; color: var(--sp-color-text-3); }
.module-link { display: flex; justify-content: space-between; align-items: center; padding-top: 15px; border-top: 1px solid #f0f1ed; font-size: 12px; color: var(--sp-color-primary); }
.workflow-panel { display: flex; align-items: center; gap: 32px; margin-top: 26px; padding: 26px 30px; border: 1px solid var(--sp-color-border); border-radius: 12px; background: #f0f0e9; }.workflow-intro { flex-shrink: 0; }.workflow-intro h2 { margin-top: 7px; }.workflow-intro p { margin: 6px 0 0; font-size: 12px; color: var(--sp-color-text-3); }
.workflow-steps { display: grid; grid-template-columns: repeat(3, 1fr); flex: 1; gap: 22px; margin: 0; padding: 0; list-style: none; }.workflow-steps li { display: flex; align-items: center; gap: 12px; }.workflow-steps li > span { display: grid; place-items: center; flex-shrink: 0; width: 30px; height: 30px; background: #fff; border: 1px solid #dde1d5; border-radius: 50%; color: #69795d; font-family: Georgia, serif; font-style: italic; }.workflow-steps h3 { margin: 0; font-size: 13px; font-weight: 500; }.workflow-steps p { margin: 4px 0 0; font-size: 11px; color: var(--sp-color-text-3); }
.home-footer { margin-top: 25px; font-size: 11px; color: var(--sp-color-text-3); }
@media (max-width: 1200px) { .module-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.workflow-panel { align-items: flex-start; flex-direction: column; gap: 22px; }.workflow-steps { width: 100%; }.welcome-copy { padding: 30px; } }
</style>
