<script setup lang="ts">
/** 登录与注册共用品牌外壳，装饰插画不参与表单交互。 */
import AppIcon from '@/components/common/AppIcon.vue'
</script>

<template>
  <div class="auth-shell">
    <aside class="auth-brand">
      <div class="auth-logo"><span class="auth-brand-mark"><AppIcon name="spark" :size="25" /></span><span>SupportPilot<small>智能客服工作台</small></span></div>
      <div class="auth-brand-inner">
        <p class="auth-eyebrow">为每一份信任，用心回应</p>
        <h2>好服务，<br>从一次<span>好对话</span>开始。</h2>
        <p class="auth-brand-desc">连接知识、AI 与团队协作，<br>让复杂的售后问题，拥有清晰的解决路径。</p>
        <div class="auth-art" aria-hidden="true">
          <div class="auth-orbit" /><div class="auth-orbit second" />
          <div class="art-ticket ticket-question"><span class="ticket-avatar"><AppIcon name="chat" /></span><div><b>每一个问题，都值得认真对待</b><span class="ticket-line" /><span class="ticket-line short" /></div></div>
          <div class="art-ticket ticket-answer"><span class="ticket-avatar"><AppIcon name="spark" /></span><div><b>让知识与协作，一起找到答案</b><div class="ticket-tags"><span>知识引用</span><span>协同审批</span><span>持续跟进</span></div></div></div>
          <span class="auth-art-star">✦</span>
        </div>
        <div class="auth-features"><span><AppIcon name="knowledge" :size="16" /> 知识驱动</span><span><AppIcon name="members" :size="16" /> 人机协同</span><span><AppIcon name="shield" :size="16" /> 企业空间</span></div>
      </div>
      <p class="auth-brand-footer">SUPPORT, WITH A HUMAN TOUCH.</p>
    </aside>
    <main class="auth-panel">
      <span class="auth-panel-label">你的客户服务，从这里连接</span>
      <div class="auth-card"><div class="form-kicker">欢迎使用 SUPPORTPILOT</div><slot /></div>
      <p class="auth-panel-footer">专注每一次沟通，让服务更有温度。</p>
    </main>
  </div>
</template>

<style scoped>
.auth-shell { display: grid; grid-template-columns: 1fr 1fr; min-height: 100vh; background: #fafaf7; }
.auth-brand { position: relative; display: flex; flex-direction: column; padding: 40px 9%; overflow: hidden; color: #f6f6ed; background: #203e34; }
.auth-logo { display: flex; align-items: center; gap: 12px; font-size: 23px; letter-spacing: -.6px; font-weight: 600; }.auth-logo small { display: block; margin-top: 1px; font-size: 10px; font-weight: 400; letter-spacing: 2px; color: #aac0b2; }
.auth-brand-mark { display: grid; place-items: center; width: 43px; height: 43px; color: #e1ecb2; border: 1px solid #a7c59655; border-radius: 12px; background: #b9d6a015; }
.auth-brand-inner { width: 100%; max-width: 470px; margin: auto; padding: 60px 0; }.auth-eyebrow { font-size: 12px; letter-spacing: 3px; color: #b2c8b5; }.auth-brand h2 { margin: 22px 0; font-size: clamp(30px, 3.1vw, 49px); font-weight: 500; letter-spacing: -1.5px; line-height: 1.5; }.auth-brand h2 span { color: #d5e4a2; }.auth-brand-desc { margin: 0; color: #b6c6bc; font-size: 14px; line-height: 2; }
.auth-art { position: relative; height: 235px; margin: 28px 0 12px; }.auth-orbit { position: absolute; width: 220px; height: 220px; border: 1px solid #91b99b25; border-radius: 50%; left: 20%; top: -5px; }.auth-orbit.second { width: 320px; height: 320px; left: 9%; top: -55px; }
.art-ticket { position: absolute; display: flex; align-items: flex-start; gap: 13px; padding: 21px; width: 92%; border-radius: 12px; border: 1px solid #7d9b8760; background: #335245; box-shadow: 0 15px 35px #10261c28; }.ticket-question { top: 15px; left: 0; transform: rotate(-4deg); }.ticket-answer { bottom: 14px; right: 0; background: #eaf0dd; color: #345544; transform: rotate(3deg); }.art-ticket b { font-size: 12px; font-weight: 500; }.ticket-avatar { width: 33px; height: 33px; display: grid; place-items: center; border-radius: 9px; background: #bcd0b21f; flex-shrink: 0; }.ticket-answer .ticket-avatar { background: #d8e3c8; }.ticket-line { display: block; height: 5px; margin-top: 12px; width: 200px; max-width: 100%; border-radius: 4px; background: #728d7d66; }.ticket-line.short { width: 120px; margin-top: 8px; }.ticket-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 13px; }.ticket-tags span { padding: 3px 7px; border-radius: 4px; border: 1px solid #c7d3b9; font-size: 10px; }.auth-art-star { position: absolute; top: -15px; right: 0; color: #d9e7a9; font-size: 37px; }
.auth-features { display: flex; justify-content: space-between; gap: 12px; color: #bed0be; font-size: 11px; }.auth-features span { display: flex; align-items: center; gap: 7px; }.auth-brand-footer { margin: 0; color: #a2b8aa; font-size: 9px; letter-spacing: 2.5px; }
.auth-panel { position: relative; display: flex; flex-direction: column; justify-content: center; align-items: center; min-width: 0; padding: 90px 40px; }.auth-panel-label { position: absolute; top: 46px; right: 40px; font-size: 11px; letter-spacing: 1px; color: #727e70; }.auth-card { width: 100%; max-width: 370px; }.form-kicker { margin-bottom: 22px; color: var(--sp-color-primary); font-size: 10px; letter-spacing: 2px; }.auth-panel-footer { position: absolute; bottom: 25px; font-size: 11px; color: #717a6b; }
.auth-card :deep(.page-title) { font-size: 30px; letter-spacing: -1px; }.auth-card :deep(.page-subtitle) { margin-top: 10px; font-size: 13px; }.auth-card :deep(.page-heading) { margin-bottom: 34px; }.auth-card :deep(.field) { margin-bottom: 22px; }.auth-card :deep(.field-label) { margin-bottom: 9px; }.auth-card :deep(.n-input) { --n-height: 44px !important; background: #fff; }.auth-card :deep(.submit-button) { margin-top: 8px; height: 46px; }.auth-card :deep(.page-footer) { margin-top: 25px; }
@media (max-width: 860px) { .auth-shell { grid-template-columns: 1fr; }.auth-brand { padding: 22px 25px; }.auth-brand-inner, .auth-brand-footer { display: none; }.auth-logo { font-size: 20px; }.auth-panel { min-height: calc(100vh - 89px); padding: 70px 25px; }.auth-panel-label { top: 20px; right: auto; }.auth-panel-footer { bottom: 14px; } }
@media (prefers-reduced-motion: no-preference) { .auth-card { animation: sp-enter .5s ease both; } }
</style>
