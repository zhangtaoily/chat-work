// 单据工作台弹层 MVP：复杂单据全屏预览（主表分组 + 子表明细 + 异常分级确认）
// 数据源：现有 draft_card（items 子表数组）+ confirm_card 快照，不改后端事件契约
// （原型参考 prototype.html 2250-2439；doc_workbench 事件真实接线为 Phase 2）
import { Button, Descriptions, Modal, Space, Table, Tag, Typography } from 'antd'
import { CheckOutlined } from '@ant-design/icons'
import type { TableColumnsType } from 'antd'
import type React from 'react'
import { useState } from 'react'
import type { ConfirmCardEvent, DraftCardEvent } from '@chat-work/protocol'
import type { ConfirmState } from './chatModel'
import {
  fieldLabel,
  fieldValueText,
  HIDDEN_FIELDS,
  SOURCE_LABELS
} from './labels'

/** 草稿字段归一：{ value, source? } 或平铺值（与 ChatTurn DraftCardView 一致） */
function asField(raw: unknown): { value: unknown; source?: string } {
  if (raw && typeof raw === 'object' && 'value' in (raw as Record<string, unknown>)) {
    const rec = raw as { value: unknown; source?: unknown }
    return typeof rec.source === 'string'
      ? { value: rec.value, source: rec.source }
      : { value: rec.value }
  }
  return { value: raw }
}

/** 主表分组（key 不在白名单的归入「其他」组） */
const MAIN_TABLE_GROUPS: Array<{ title: string; keys: string[] }> = [
  {
    title: '基本信息',
    keys: ['customer_name', 'customer_id', 'order_type', 'contact', 'leave_type', 'start_time', 'end_time', 'duration_days']
  },
  { title: '物流信息', keys: ['address', 'delivery_date'] },
  { title: '价格与付款', keys: ['payment_term'] }
]

/** 子表行（rules.py CRM 订单解析产物：{ sku, qty, price? }，price 缺失即异常行） */
interface ItemRow {
  key: string
  sku: string
  qty: number | null
  price: number | null
  anomalous: boolean
}

function extractItemRows(draft: DraftCardEvent | undefined): ItemRow[] {
  const value = draft ? asField(draft.draft['items']).value : []
  if (!Array.isArray(value)) return []
  return value
    .filter((it): it is Record<string, unknown> => !!it && typeof it === 'object')
    .map((it, index) => {
      const qty = typeof it['qty'] === 'number' ? it['qty'] : null
      const price = typeof it['price'] === 'number' ? it['price'] : null
      return {
        key: String(it['sku'] ?? index),
        sku: String(it['sku'] ?? '-'),
        qty,
        price,
        anomalous: price === null || qty === null
      }
    })
}

/** 主表渲染字段：跳过子表与技术性隐藏字段 */
function mainTableEntries(draft: DraftCardEvent | undefined): Array<[string, { value: unknown; source?: string }]> {
  if (!draft) return []
  return Object.entries(draft.draft)
    .filter(([key]) => key !== 'items' && !HIDDEN_FIELDS.has(key))
    .map(([key, raw]) => [key, asField(raw)] as const)
}

function formatMoney(value: number): string {
  return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function MainTableSection(props: { draft: DraftCardEvent | undefined; confirm: ConfirmCardEvent }): React.JSX.Element {
  const { draft, confirm } = props
  const entries = mainTableEntries(draft)

  // 无草稿回退：直接渲染确认卡快照的平铺 fields（恢复会话等场景）
  if (entries.length === 0) {
    const payloadFields =
      confirm.payload.fields && typeof confirm.payload.fields === 'object'
        ? (confirm.payload.fields as Record<string, unknown>)
        : {}
    return (
      <Descriptions column={1} size="small" bordered>
        {Object.entries(payloadFields)
          .filter(([key]) => !HIDDEN_FIELDS.has(key))
          .map(([key, value]) => (
            <Descriptions.Item key={key} label={fieldLabel(key)}>
              {fieldValueText(key, value)}
            </Descriptions.Item>
          ))}
      </Descriptions>
    )
  }

  // 分组渲染：白名单组 + 其余归「其他」
  const grouped = new Set(MAIN_TABLE_GROUPS.flatMap((g) => g.keys))
  const otherEntries = entries.filter(([key]) => !grouped.has(key))
  const groups = [...MAIN_TABLE_GROUPS.map((g) => ({
    title: g.title,
    entries: entries.filter(([key]) => g.keys.includes(key))
  })), { title: '其他', entries: otherEntries }].filter((g) => g.entries.length > 0)

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 12 }}>
      {groups.map((group) => (
        <div key={group.title} style={{ border: '1px solid #f0f0f0', borderRadius: 8, padding: '10px 12px' }}>
          <Typography.Text strong style={{ fontSize: 13, display: 'block', marginBottom: 8 }}>
            {group.title}
          </Typography.Text>
          <Descriptions column={1} size="small">
            {group.entries.map(([key, field]) => (
              <Descriptions.Item key={key} label={fieldLabel(key)}>
                <Space size={6}>
                  <span>{fieldValueText(key, field.value)}</span>
                  {field.source && (
                    <Tag style={{ marginInlineEnd: 0 }}>{SOURCE_LABELS[field.source] ?? field.source}</Tag>
                  )}
                </Space>
              </Descriptions.Item>
            ))}
          </Descriptions>
        </div>
      ))}
    </div>
  )
}

function ItemTableSection(props: {
  rows: ItemRow[]
  onlyAnomaly: boolean
  onToggleAnomaly: (checked: boolean) => void
}): React.JSX.Element {
  const { rows, onlyAnomaly, onToggleAnomaly } = props
  const shown = onlyAnomaly ? rows.filter((r) => r.anomalous) : rows

  const columns: TableColumnsType<ItemRow> = [
    { title: '#', width: 48, render: (_v, _r, index) => index + 1 },
    { title: '物料编码', dataIndex: 'sku' },
    { title: '数量', dataIndex: 'qty', render: (v) => (v === null ? '—' : v) },
    { title: '单价（元）', dataIndex: 'price', render: (v) => (v === null ? '—' : formatMoney(v)) },
    {
      title: '金额（元）',
      render: (_v, r) => (r.qty !== null && r.price !== null ? formatMoney(r.qty * r.price) : '—')
    },
    {
      title: '状态',
      width: 96,
      render: (_v, r) =>
        r.anomalous ? <Tag color="orange" style={{ marginInlineEnd: 0 }}>缺单价</Tag> : <Tag color="green" style={{ marginInlineEnd: 0 }}>正常</Tag>
    }
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', margin: '4px 0 8px' }}>
        <Typography.Text strong style={{ fontSize: 13 }}>
          商品明细（{rows.length} 行）
        </Typography.Text>
        <Button size="small" type={onlyAnomaly ? 'primary' : 'default'} onClick={() => onToggleAnomaly(!onlyAnomaly)}>
          仅看异常行
        </Button>
      </div>
      <Table<ItemRow>
        size="small"
        columns={columns}
        dataSource={shown}
        pagination={false}
        onRow={(record) => ({ style: { background: record.anomalous ? '#fffbe6' : undefined } })}
      />
    </div>
  )
}

export function DocWorkbenchModal(props: {
  open: boolean
  onClose: () => void
  draft?: DraftCardEvent | undefined
  confirm: ConfirmCardEvent
  confirmState: ConfirmState
  confirmBusy: boolean
  /** 确认剩余毫秒数（ConfirmCardView 持有倒计时，透传保持同步） */
  remain: number
  onConfirm: (token: string, action: 'confirm' | 'reject') => void
}): React.JSX.Element {
  const { open, onClose, draft, confirm, confirmState, confirmBusy, remain, onConfirm } = props
  const [onlyAnomaly, setOnlyAnomaly] = useState(false)

  const rows = extractItemRows(draft)
  const missing = draft?.missing_fields ?? []
  const anomalyCount = rows.filter((r) => r.anomalous).length + missing.length
  const hasAnomaly = anomalyCount > 0
  const total = rows.reduce((sum, r) => (r.qty !== null && r.price !== null ? sum + r.qty * r.price : sum), 0)
  const title = typeof confirm.payload.skill_title === 'string' ? confirm.payload.skill_title : '单据工作台'

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width="min(1140px, 96vw)"
      centered
      title={
        <Space size={8} wrap>
          <span>{title}</span>
          {draft && <Tag color="blue" style={{ marginInlineEnd: 0 }}>草稿 v{draft.draft_version}</Tag>}
          {hasAnomaly && (
            <Tag color="orange" style={{ marginInlineEnd: 0 }}>
              {anomalyCount} 处异常
            </Tag>
          )}
        </Space>
      }
      footer={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
          <div style={{ minWidth: 0 }}>
            {rows.length > 0 && (
              <>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  合计金额
                </Typography.Text>
                <Typography.Text strong style={{ fontSize: 22, lineHeight: 1.2, display: 'block' }}>
                  ¥ {formatMoney(total)}
                </Typography.Text>
              </>
            )}
            {missing.length > 0 && (
              <Typography.Text type="warning" style={{ fontSize: 12 }}>
                缺少必填信息：{missing.map(fieldLabel).join('、')}
              </Typography.Text>
            )}
          </div>
          <Space style={{ flexShrink: 0 }}>
            {confirmState === 'confirmed' && (
              <Tag icon={<CheckOutlined />} color="success" style={{ marginInlineEnd: 0 }}>
                已确认，提交完成
              </Tag>
            )}
            {confirmState === 'rejected' && (
              <Tag color="default" style={{ marginInlineEnd: 0 }}>
                已取消
              </Tag>
            )}
            {(confirmState === 'expired' || (confirmState === 'pending' && remain <= 0)) && (
              <Tag color="warning" style={{ marginInlineEnd: 0 }}>
                确认已失效，请重新发起
              </Tag>
            )}
            {confirmState === 'pending' && remain > 0 && (
              <>
                <Button loading={confirmBusy} onClick={() => onConfirm(confirm.confirm_token, 'reject')}>
                  取消
                </Button>
                <Button
                  type="primary"
                  loading={confirmBusy}
                  onClick={() => onConfirm(confirm.confirm_token, 'confirm')}
                >
                  {hasAnomaly ? '异常已知晓 · 确认提交' : '确认提交'}
                </Button>
              </>
            )}
          </Space>
        </div>
      }
    >
      <div style={{ maxHeight: '62vh', overflowY: 'auto', paddingRight: 4 }}>
        <MainTableSection draft={draft} confirm={confirm} />
        {rows.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <ItemTableSection rows={rows} onlyAnomaly={onlyAnomaly} onToggleAnomaly={setOnlyAnomaly} />
          </div>
        )}
      </div>
    </Modal>
  )
}
