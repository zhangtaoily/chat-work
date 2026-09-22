// 展示文案映射：字段名 / 参数来源徽标 / 请假类型 / 单据状态（对齐 mcp_oa mock 与流水线规则）
export const FIELD_LABELS: Record<string, string> = {
  leave_type: '假期类型',
  start_time: '开始时间',
  end_time: '结束时间',
  duration_days: '时长（天）',
  reason: '事由',
  balance_days: '可用余额（天）',
  user_id: '工号',
  doc_no: '单据号',
  status: '状态',
  submitted_at: '提交时间',
  idempotent_reuse: '幂等命中',
  approval_id: '审批单号',
  approval_title: '审批事项',
  applicant: '申请人',
  action: '审批动作',
  comment: '审批意见',
  doc_type: '单据类型',
  message: '处理结果'
}

export const LEAVE_TYPE_LABELS: Record<string, string> = {
  annual: '年假',
  comp: '调休',
  sick: '病假',
  personal: '事假',
  marriage: '婚假',
  bereavement: '丧假',
  maternity: '产假'
}

export const SOURCE_LABELS: Record<string, string> = {
  default: '默认',
  memory: '记忆',
  ask: '对话提取',
  computed: '自动计算'
}

export const DOC_STATUS_LABELS: Record<string, string> = {
  submitted: '已提交待审批',
  approved: '已通过',
  rejected: '已驳回'
}

export const ACTION_LABELS: Record<string, string> = {
  approve: '同意',
  reject: '驳回'
}

/** 技术性字段不进入卡片展示（幂等键 / 后端附带的类型中文名） */
export const HIDDEN_FIELDS = new Set(['idempotency_key', 'leave_type_label'])

export function fieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key
}

export function fieldValueText(key: string, value: unknown): string {
  if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') {
    return JSON.stringify(value)
  }
  if (key === 'leave_type' && typeof value === 'string') {
    return LEAVE_TYPE_LABELS[value] ?? value
  }
  if (key === 'action' && typeof value === 'string') {
    return ACTION_LABELS[value] ?? value
  }
  if (key === 'idempotent_reuse') return value ? '是（未重复提交）' : '否'
  if (key === 'status' && typeof value === 'string') {
    return DOC_STATUS_LABELS[value] ?? value
  }
  return String(value)
}
