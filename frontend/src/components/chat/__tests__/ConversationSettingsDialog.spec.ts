/*
 * ConversationSettingsDialog 测试：空白输入的拦截必须「可见」——
 * 既不保存、也不静默关闭对话框，而是在对话框内给出错误文案；
 * 有效输入则去空白后提交并允许关闭；更新中禁止关闭。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ConversationSettingsDialog from '@/components/chat/ConversationSettingsDialog.vue'

/** 挂载对话框（teleport 打桩，让弹层内容可被查询）。 */
function mountDialog(props: Partial<{ show: boolean; systemPrompt: string | null; updating: boolean }> = {}) {
  return mount(ConversationSettingsDialog, {
    props: { show: true, systemPrompt: null, updating: false, ...props },
    global: { stubs: { teleport: true } },
  })
}

/** 找到对话框的「保存」按钮（Naive UI 的 positive 按钮）。 */
function findSaveButton(wrapper: ReturnType<typeof mountDialog>) {
  return wrapper.findAll('button').find((button) => button.text().includes('保存'))
}

/** 找到对话框的「取消」按钮（Naive UI 的 negative 按钮）。 */
function findCancelButton(wrapper: ReturnType<typeof mountDialog>) {
  return wrapper.findAll('button').find((button) => button.text().includes('取消'))
}

describe('ConversationSettingsDialog 空白输入拦截', () => {
  it('纯空格输入：不保存、不关闭对话框，并给出「系统提示词不能为空」', async () => {
    // 保护行为：空白输入不能保存成功，且用户必须能感知「为什么没保存」——
    // Naive UI 只在 positive-click 返回 false 时才不关闭对话框，
    // 若直接关闭，赋值的错误文案会随对话框一起卸载而永远不可见（静默失败）
    const wrapper = mountDialog()
    await wrapper.find('textarea').setValue('   ')

    await findSaveButton(wrapper)?.trigger('click')
    await flushPromises()

    // 未保存
    expect(wrapper.emitted('save')).toBeUndefined()
    // 未关闭（未向父组件请求收起）
    expect(wrapper.emitted('update:show')).toBeUndefined()
    // 错误提示可见
    expect(wrapper.text()).toContain('系统提示词不能为空')
  })

  it('有效输入：去空白后提交，并允许对话框关闭', async () => {
    // 保护行为：合法输入正常保存（覆盖 E-10 有效路径），行为不因拦截修复而回归
    const wrapper = mountDialog()
    await wrapper.find('textarea').setValue('  回复请用简体中文  ')

    await findSaveButton(wrapper)?.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('save')).toEqual([['回复请用简体中文']])
    expect(wrapper.emitted('update:show')).toEqual([[false]])
    expect(wrapper.text()).not.toContain('系统提示词不能为空')
  })

  it('拒绝后再输入有效值：错误提示消失且可以正常保存', async () => {
    // 边界情况：拦截一次后不能把用户困在错误态里
    const wrapper = mountDialog()
    await wrapper.find('textarea').setValue('  ')
    await findSaveButton(wrapper)?.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('系统提示词不能为空')

    await wrapper.find('textarea').setValue('仅回答订单相关问题')
    await findSaveButton(wrapper)?.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('save')).toEqual([['仅回答订单相关问题']])
  })
})

describe('ConversationSettingsDialog 关闭与更新中锁定', () => {
  it('更新中：保存与取消都不触发关闭请求', async () => {
    // 保护行为：请求进行中禁止关闭，防止半途丢状态
    const wrapper = mountDialog({ updating: true })

    await findSaveButton(wrapper)?.trigger('click')
    await flushPromises()
    await findCancelButton(wrapper)?.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('update:show')).toBeUndefined()
  })

  it('打开时同步当前已保存值', async () => {
    // 保护行为：重新打开对话框必须回显已保存的系统提示词，而不是空白
    const wrapper = mountDialog({ show: false, systemPrompt: '已保存的偏好' })

    await wrapper.setProps({ show: true })
    await flushPromises()

    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('已保存的偏好')
  })
})
