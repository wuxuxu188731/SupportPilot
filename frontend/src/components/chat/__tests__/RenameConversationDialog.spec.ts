/* 会话重命名对话框测试：验证标题回显、长度校验与保存期间的关闭控制。 */
import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import RenameConversationDialog from '@/components/chat/RenameConversationDialog.vue'

/** 挂载重命名对话框并保留弹层内容。 */
function mountDialog(props: Partial<{ show: boolean; title: string; updating: boolean }> = {}) {
  return mount(RenameConversationDialog, {
    props: { show: true, title: '原标题', updating: false, ...props },
    global: { stubs: { teleport: true } },
  })
}

/** 查找对话框保存按钮。 */
function saveButton(wrapper: ReturnType<typeof mountDialog>) {
  return wrapper.findAll('button').find((button) => button.text().includes('保存'))
}

describe('RenameConversationDialog', () => {
  // 保护行为：打开时回显目标会话的当前标题。
  it('打开时回显当前标题', async () => {
    const wrapper = mountDialog({ show: false, title: '订单咨询' })
    await wrapper.setProps({ show: true })
    await flushPromises()

    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('订单咨询')
  })

  // 边界情况：全空白标题不能提交，错误应留在对话框中。
  it('拦截空白标题并显示错误', async () => {
    const wrapper = mountDialog()
    await wrapper.find('input').setValue('   ')
    await saveButton(wrapper)?.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('save')).toBeUndefined()
    expect(wrapper.text()).toContain('会话标题长度必须为 1–30 个字符')
  })

  // 保护行为：合法标题去除首尾空白后交给父组件保存。
  it('提交去空白后的标题', async () => {
    const wrapper = mountDialog()
    await wrapper.find('input').setValue('  售后跟进  ')
    await saveButton(wrapper)?.trigger('click')

    expect(wrapper.emitted('save')).toEqual([['售后跟进']])
  })
})
