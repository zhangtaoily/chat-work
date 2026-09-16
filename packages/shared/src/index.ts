// @chat-work/shared：纯工具（时间/金额格式化、枚举常量），无 UI 依赖

/** 金额格式化：千分位 + 两位小数（如 1234567.8 → "1,234,567.80"） */
export function formatMoney(value: number): string {
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })
}

/** 日期格式化：YYYY-MM-DD */
export function formatDate(date: Date): string {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

/** 工具读写类型（与 protocol annotations._meta.rw 对齐） */
export enum ToolRw {
  Read = 'read',
  Write = 'write'
}

/** HITL 人工确认级别（与 protocol annotations._meta.humanConfirmation 对齐） */
export enum HumanConfirmation {
  None = 'none',
  Required = 'required'
}
