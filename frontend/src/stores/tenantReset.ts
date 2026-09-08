/*
 * 租户缓存重置机制（预留）。
 *
 * 后续阶段加入聊天记录、知识库列表、审批缓存等「跟随企业变化」的业务
 * 缓存时，各模块在创建缓存处调用 registerTenantResetHandler 注册清理回调；
 * 切换企业 / 退出登录时统一执行，保证旧企业的数据不会泄漏到新企业界面。
 * 当前无业务缓存注册，本机制仅为统一入口预留。
 */

/** 租户级缓存清理回调：切换企业或退出登录时执行。 */
export type TenantResetHandler = () => void | Promise<void>

/** 已注册的清理回调集合。 */
const handlers = new Set<TenantResetHandler>()

/**
 * 注册一个租户级清理回调，返回取消注册函数。
 * 示例：进入聊天页缓存会话列表的模块，可在此注册清空缓存的回调。
 */
export function registerTenantResetHandler(handler: TenantResetHandler): () => void {
  handlers.add(handler)
  return () => {
    handlers.delete(handler)
  }
}

/** 执行全部已注册的租户清理回调（顺序执行，等待异步回调完成）。 */
export async function runTenantResetHandlers(): Promise<void> {
  for (const handler of handlers) {
    await handler()
  }
}
