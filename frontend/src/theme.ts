import type { GlobalThemeOverrides } from 'naive-ui'

/*
 * Naive UI 主题覆盖：把组件默认色与 styles/tokens.css 中的设计令牌对齐，
 * 保证「企业蓝」在表单、按钮、标签等组件上保持一致。
 */
export const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#2456c7',
    primaryColorHover: '#3a6ad6',
    primaryColorPressed: '#1c439d',
    primaryColorSuppl: '#2456c7',
    successColor: '#1f9d55',
    warningColor: '#b7791f',
    errorColor: '#c0392b',
    borderRadius: '10px',
    fontFamily:
      "'Inter', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Helvetica Neue', Arial, sans-serif",
  },
  Button: {
    fontWeight: '500',
  },
  Card: {
    borderRadius: '14px',
  },
}
