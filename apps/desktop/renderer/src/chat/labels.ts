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
  message: '处理结果',
  // ERP 单据（CRM 销售订单，单据工作台弹层）
  customer_name: '客户名称',
  customer_id: '客户编码',
  order_type: '单据类型',
  contact: '联系人',
  address: '收货地址',
  delivery_date: '期望交货',
  payment_term: '付款方式',
  items: '商品明细',
  sku: '物料编码',
  qty: '数量',
  price: '单价（元）',
  amount: '金额（元）',
  // MES 生产（P3.1：工单进度 / 报工明细，状态映射对齐 agent_core MES_STATUS_LABELS）
  work_order: '工单号',
  sku_name: '物料名称',
  plan_qty: '计划数量',
  completed_qty: '完成数量',
  good_qty: '良品数',
  ng_qty: '不良数',
  workstation: '工位',
  plan_start: '计划开工',
  plan_end: '计划完工',
  report_no: '报工单号',
  operator: '操作工',
  report_time: '报工时间',
  work_hours: '工时（小时）',
  // U8 财务总账（P3.1：科目余额 / 凭证明细，PRD 8.2 只读）
  period: '会计期间',
  rows: '明细行',
  subject: '科目',
  direction: '方向',
  opening: '期初余额',
  debit: '借方发生',
  credit: '贷方发生',
  closing: '期末余额',
  debit_total: '借方合计',
  credit_total: '贷方合计',
  balanced: '借贷平衡',
  voucher_no: '凭证号',
  voucher_date: '凭证日期',
  summary: '摘要',
  entries: '会计分录',
  // P3.2 科室覆盖（管理速览 / 技术备件 / 产品看板，卡片为嵌套结构走摘要展示）
  bi: 'BI 指标',
  voucher: '凭证核查',
  inventory: '库存水位',
  alerts: '预警明细',
  customer: '客户档案',
  customer_candidates: '候选客户'
}

export const ORDER_TYPE_LABELS: Record<string, string> = {
  standard: '标准销售',
  sample: '样品单'
}

export const PAYMENT_TERM_LABELS: Record<string, string> = {
  prepay: '预付',
  net30: '月结 30 天',
  net60: '月结 60 天'
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

/** P3.1 MES 工单状态（对齐 agent_core rules.MES_STATUS_LABELS） */
export const MES_STATUS_LABELS: Record<string, string> = {
  pending: '未开工',
  running: '生产中',
  done: '已完工',
  closed: '已结案'
}

/** P3.1 U8 借贷方向 */
export const DIRECTION_LABELS: Record<string, string> = {
  debit: '借',
  credit: '贷'
}

/** P3.3 我的记忆（PRD 9.1-9.3）：分层 / 类别 / 来源标签 */
export const MEMORY_LAYER_LABELS: Record<string, string> = {
  L2: '个人记忆',
  L3: '组织记忆'
}

export const MEMORY_KIND_LABELS: Record<string, string> = {
  preference: '偏好',
  params: '常用参数',
  habit: '操作习惯',
  faq: '常见问题'
}

export const MEMORY_SOURCE_LABELS: Record<string, string> = {
  ask: '手动添加',
  auto_consolidated: '自动沉淀',
  promoted: '科室提炼'
}

export const ACTION_LABELS: Record<string, string> = {
  approve: '同意',
  reject: '驳回'
}

/** 技术性字段不进入卡片展示（幂等键 / 后端附带的类型中文名 / 卡片类型标识） */
export const HIDDEN_FIELDS = new Set(['idempotency_key', 'leave_type_label', 'type'])

export function fieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key
}

export function fieldValueText(key: string, value: unknown): string {
  if (Array.isArray(value)) return `共 ${value.length} 行明细`
  if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') {
    return JSON.stringify(value)
  }
  if (key === 'leave_type' && typeof value === 'string') {
    return LEAVE_TYPE_LABELS[value] ?? value
  }
  if (key === 'order_type' && typeof value === 'string') {
    return ORDER_TYPE_LABELS[value] ?? value
  }
  if (key === 'payment_term' && typeof value === 'string') {
    return PAYMENT_TERM_LABELS[value] ?? value
  }
  if (key === 'action' && typeof value === 'string') {
    return ACTION_LABELS[value] ?? value
  }
  if (key === 'idempotent_reuse') return value ? '是（未重复提交）' : '否'
  if (key === 'status' && typeof value === 'string') {
    return MES_STATUS_LABELS[value] ?? DOC_STATUS_LABELS[value] ?? value
  }
  if (key === 'direction' && typeof value === 'string') {
    return DIRECTION_LABELS[value] ?? value
  }
  return String(value)
}
