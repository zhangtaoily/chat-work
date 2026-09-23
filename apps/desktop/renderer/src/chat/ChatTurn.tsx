// 会话流渲染：用户气泡 / 助手回合（阶段时间线 + 草稿卡 + 确认卡 + 终态卡片）
// 事件类型来自 @chat-work/protocol（packages/protocol，唯一契约源）
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Input,
  Space,
  Spin,
  Tag,
  Timeline,
  Typography
} from 'antd'
import { CheckOutlined, CloseOutlined, ExpandOutlined, ReloadOutlined } from '@ant-design/icons'
import type React from 'react'
import { useEffect, useState } from 'react'
import type { AssistantTurn, Turn } from './chatModel'
import { DocWorkbenchModal } from './DocWorkbenchModal'
import {
  fieldLabel,
  fieldValueText,
  HIDDEN_FIELDS,
  MEMORY_KIND_LABELS,
  MEMORY_LAYER_LABELS,
  SOURCE_LABELS
} from './labels'

const BUBBLE_MAX_WIDTH = 640

function UserTurnView({ turn }: { turn: Extract<Turn, { role: 'user' }> }): React.JSX.Element {
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
      <div
        style={{
          maxWidth: BUBBLE_MAX_WIDTH,
          padding: '10px 14px',
          borderRadius: 12,
          borderBottomRightRadius: 4,
          background: '#1677ff',
          color: '#fff',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word'
        }}
      >
        {turn.text}
      </div>
    </div>
  )
}

function StageTimeline({ steps }: { steps: AssistantTurn['stages'] }): React.JSX.Element {
  return (
    <Timeline
      style={{ margin: '4px 0 12px' }}
      items={steps.map((step) => ({
        color: 'gray',
        children: (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {step.message}
          </Typography.Text>
        )
      }))}
    />
  )
}

/** 草稿字段归一：{ value, source? } 或平铺值 */
function asField(raw: unknown): { value: unknown; source?: string } {
  if (raw && typeof raw === 'object' && 'value' in (raw as Record<string, unknown>)) {
    const rec = raw as { value: unknown; source?: unknown }
    return typeof rec.source === 'string'
      ? { value: rec.value, source: rec.source }
      : { value: rec.value }
  }
  return { value: raw }
}

function DraftCardView({ event }: { event: NonNullable<AssistantTurn['draft']> }): React.JSX.Element | null {
  const entries = Object.entries(event.draft)
  if (entries.length === 0) return null
  return (
    <Card size="small" title="参数草稿" style={{ marginBottom: 12, background: '#fafafa' }}>
      <Descriptions column={1} size="small" bordered>
        {entries.map(([key, raw]) => {
          const field = asField(raw)
          return (
            <Descriptions.Item key={key} label={fieldLabel(key)}>
              <Space size={6}>
                <span>{fieldValueText(key, field.value)}</span>
                {field.source && (
                  <Tag style={{ marginInlineEnd: 0 }}>
                    {SOURCE_LABELS[field.source] ?? field.source}
                  </Tag>
                )}
              </Space>
            </Descriptions.Item>
          )
        })}
      </Descriptions>
      {event.missing_fields.length > 0 && (
        <Typography.Text type="warning" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
          缺少必填信息：{event.missing_fields.map(fieldLabel).join('、')}
        </Typography.Text>
      )}
    </Card>
  )
}

function formatRemain(ms: number): string {
  const totalSec = Math.floor(ms / 1000)
  const min = Math.floor(totalSec / 60)
  const sec = totalSec % 60
  return `${String(min).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
}

function ConfirmCardView(props: {
  turn: AssistantTurn
  confirmBusy: boolean
  onConfirm: (token: string, action: 'confirm' | 'reject') => void
  onExpire: (token: string) => void
}): React.JSX.Element {
  const { turn, confirmBusy, onConfirm, onExpire } = props
  const confirm = turn.confirm
  const [remain, setRemain] = useState(() =>
    confirm ? Math.max(0, confirm.expires_at - Date.now()) : 0
  )
  // 单据工作台弹层（复杂单据全屏预览，wb-a MVP）
  const [wbOpen, setWbOpen] = useState(false)

  useEffect(() => {
    if (!confirm) return
    const timer = setInterval(() => {
      const next = Math.max(0, confirm.expires_at - Date.now())
      setRemain(next)
      if (next <= 0) {
        clearInterval(timer)
        onExpire(confirm.confirm_token)
      }
    }, 1000)
    return () => clearInterval(timer)
  }, [confirm, onExpire])

  if (!confirm) return <></>

  const payloadFields =
    confirm.payload.fields && typeof confirm.payload.fields === 'object'
      ? (confirm.payload.fields as Record<string, unknown>)
      : {}
  const fields = Object.entries(payloadFields).filter(([key]) => !HIDDEN_FIELDS.has(key))

  return (
    <Card
      size="small"
      title={
        <Space>
          待确认提交
          {turn.confirmState === 'pending' && remain > 0 && (
            <Tag color="orange" style={{ marginInlineEnd: 0 }}>
              {formatRemain(remain)} 后过期
            </Tag>
          )}
        </Space>
      }
      style={{
        marginBottom: 12,
        borderColor: '#faad14',
        background: '#fffbe6'
      }}
    >
      <Descriptions column={1} size="small" bordered>
        {fields.map(([key, value]) => (
          <Descriptions.Item key={key} label={fieldLabel(key)}>
            {fieldValueText(key, value)}
          </Descriptions.Item>
        ))}
      </Descriptions>
      <Button
        type="link"
        size="small"
        icon={<ExpandOutlined />}
        style={{ padding: 0, marginTop: 8 }}
        onClick={() => setWbOpen(true)}
      >
        展开完整单据
      </Button>
      <Space style={{ marginTop: 12 }}>
        {turn.confirmState === 'confirmed' && (
          <Tag icon={<CheckOutlined />} color="success">
            已确认，提交完成
          </Tag>
        )}
        {turn.confirmState === 'rejected' && (
          <Tag icon={<CloseOutlined />} color="default">
            已取消
          </Tag>
        )}
        {remain <= 0 && turn.confirmState === 'pending' && (
          <Tag icon={<ReloadOutlined />} color="warning">
            确认已过期，请重新发起
          </Tag>
        )}
        {turn.confirmState === 'expired' && (
          <Tag icon={<ReloadOutlined />} color="warning">
            确认已失效，请重新发起
          </Tag>
        )}
        {turn.confirmState === 'pending' && remain > 0 && (
          <>
            <Button
              type="primary"
              loading={confirmBusy}
              onClick={() => onConfirm(confirm.confirm_token, 'confirm')}
            >
              确认提交
            </Button>
            <Button disabled={confirmBusy} onClick={() => onConfirm(confirm.confirm_token, 'reject')}>
              取消
            </Button>
          </>
        )}
      </Space>
      <DocWorkbenchModal
        open={wbOpen}
        onClose={() => setWbOpen(false)}
        draft={turn.draft}
        confirm={confirm}
        confirmState={turn.confirmState}
        confirmBusy={confirmBusy}
        remain={remain}
        onConfirm={onConfirm}
      />
    </Card>
  )
}

/** 待办列表卡（场景 2）：行内「同意/驳回」快捷入口，点击发送对话消息，仍走 HITL 二次确认 */
function TodoListCardView(props: {
  items: Array<Record<string, unknown>>
  streaming: boolean
  onQuickReply: (text: string) => void
}): React.JSX.Element {
  const { items, streaming, onQuickReply } = props
  return (
    <Card size="small" title={`待办审批（${items.length}）`} style={{ marginBottom: 8 }}>
      {items.map((item, index) => {
        const n = index + 1
        return (
          <div
            key={String(item['approval_id'] ?? n)}
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              gap: 12,
              padding: '8px 0',
              borderBottom: index < items.length - 1 ? '1px solid #f0f0f0' : undefined
            }}
          >
            <div style={{ minWidth: 0 }}>
              <Typography.Text strong style={{ fontSize: 13 }}>
                {n}. {String(item['title'] ?? '')}
              </Typography.Text>
              <Typography.Text
                type="secondary"
                style={{ fontSize: 12, display: 'block', marginTop: 2 }}
              >
                {String(item['applicant'] ?? '')} · 提交于 {String(item['submitted_at'] ?? '')}
              </Typography.Text>
            </div>
            <Space size={4} style={{ flexShrink: 0 }}>
              <Button
                size="small"
                type="primary"
                disabled={streaming}
                onClick={() => onQuickReply(`同意第 ${n} 条`)}
              >
                同意
              </Button>
              <Button
                size="small"
                danger
                disabled={streaming}
                onClick={() => onQuickReply(`驳回第 ${n} 条`)}
              >
                驳回
              </Button>
            </Space>
          </div>
        )
      })}
      <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
        点击「同意/驳回」后仍需在确认卡中二次确认（防误操作）
      </Typography.Text>
    </Card>
  )
}

/** 金额自适应格式化：亿 / 万 / 原值 */
function formatAmount(value: number): string {
  if (Math.abs(value) >= 1e8) return `${(value / 1e8).toFixed(2)} 亿`
  if (Math.abs(value) >= 1e4) return `${(value / 1e4).toFixed(1)} 万`
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

function BarList(props: {
  bars: Array<Record<string, unknown>>
  highlightLast?: boolean
}): React.JSX.Element {
  const { bars, highlightLast } = props
  const max = Math.max(...bars.map((b) => Number(b['value'] ?? 0)), 1)
  return (
    <>
      {bars.map((bar, index) => {
        const value = Number(bar['value'] ?? 0)
        return (
          <div key={String(bar['label'] ?? bar['period'] ?? index)} style={{ marginBottom: 8 }}>
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                fontSize: 12,
                marginBottom: 2
              }}
            >
              <Typography.Text type="secondary">{String(bar['label'] ?? bar['period'] ?? '')}</Typography.Text>
              <Typography.Text strong style={{ fontSize: 12 }}>
                {formatAmount(value)}
              </Typography.Text>
            </div>
            <div style={{ background: '#f0f0f0', borderRadius: 4, height: 8, overflow: 'hidden' }}>
              <div
                style={{
                  width: `${(value / max) * 100}%`,
                  height: '100%',
                  borderRadius: 4,
                  background:
                    highlightLast && index === bars.length - 1 ? '#52c41a' : '#1677ff'
                }}
              />
            </div>
          </div>
        )
      })}
    </>
  )
}

/** BI 结果卡（场景 3）：KPI 数值 + 趋势/拆分横向条形（纯 CSS，零图表依赖） */
function BiResultCardView({ card }: { card: Record<string, unknown> }): React.JSX.Element {
  const kpi = (card['kpi'] ?? {}) as Record<string, unknown>
  const trend = Array.isArray(card['trend'])
    ? (card['trend'] as Array<Record<string, unknown>>)
    : []
  const series = Array.isArray(card['series'])
    ? (card['series'] as Array<Record<string, unknown>>)
    : []
  const dimension = typeof card['dimension'] === 'string' ? card['dimension'] : null
  const mom = typeof card['mom_pct'] === 'number' ? (card['mom_pct'] as number) : null
  return (
    <Card
      size="small"
      title={
        <Space size={6}>
          数据查询结果
          {card['cached'] === true && (
            <Tag color="blue" style={{ marginInlineEnd: 0 }}>
              热点缓存
            </Tag>
          )}
        </Space>
      }
      style={{ marginBottom: 8 }}
    >
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {String(kpi['region'] ?? '')} · {String(kpi['period'] ?? '')}
      </Typography.Text>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, margin: '4px 0 12px' }}>
        <Typography.Text strong style={{ fontSize: 28, lineHeight: 1.2 }}>
          {formatAmount(Number(kpi['value'] ?? 0))}
        </Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {String(kpi['unit'] ?? '')}
        </Typography.Text>
        {mom !== null && (
          <Tag color={mom >= 0 ? 'red' : 'green'} style={{ marginInlineEnd: 0 }}>
            环比 {mom >= 0 ? '+' : ''}
            {mom}%
          </Tag>
        )}
      </div>
      {series.length > 0 && (
        <>
          <Typography.Text strong style={{ fontSize: 13, display: 'block', marginBottom: 8 }}>
            按{dimension ?? '维度'}拆分
          </Typography.Text>
          <BarList bars={series} />
        </>
      )}
      {series.length === 0 && trend.length > 0 && (
        <>
          <Typography.Text strong style={{ fontSize: 13, display: 'block', marginBottom: 8 }}>
            近三月趋势
          </Typography.Text>
          <BarList bars={trend} highlightLast />
        </>
      )}
      {typeof card['permission_note'] === 'string' && (
        <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
          {String(card['permission_note'])}
        </Typography.Text>
      )}
    </Card>
  )
}

/** 周报草稿卡（场景 4）：Markdown 文档工作台——编辑后复制/下载（IM 群发 Phase 1 降级） */
function WeeklyReportCardView({ card }: { card: Record<string, unknown> }): React.JSX.Element {
  const initial = typeof card['markdown'] === 'string' ? card['markdown'] : ''
  const [markdown, setMarkdown] = useState(initial)
  const [copied, setCopied] = useState(false)

  const copy = async (): Promise<void> => {
    await navigator.clipboard.writeText(markdown)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const download = (): void => {
    const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `周报_${new Date().toISOString().slice(0, 10)}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <Card size="small" title="周报草稿（可编辑）" style={{ marginBottom: 8 }}>
      <Input.TextArea
        value={markdown}
        onChange={(e) => setMarkdown(e.target.value)}
        autoSize={{ minRows: 10, maxRows: 24 }}
        style={{ fontFamily: 'monospace', fontSize: 12, marginBottom: 8 }}
      />
      <Space>
        <Button size="small" type="primary" onClick={() => void copy()}>
          {copied ? '已复制' : '复制全文'}
        </Button>
        <Button size="small" onClick={download}>
          下载 .md
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          编辑后可直接粘贴到群聊（IM 直发后续版本支持）
        </Typography.Text>
      </Space>
    </Card>
  )
}

/** P3.3 记住偏好确认卡（PRD 9.3 写入触发①：可感知性——展示记住了什么，引导去记忆页管理） */
function MemorySaveCardView({ entry }: { entry: Record<string, unknown> }): React.JSX.Element {
  const content = String(entry['content'] ?? '')
  const kind = MEMORY_KIND_LABELS[String(entry['kind'] ?? '')] ?? String(entry['kind'] ?? '')
  const layer = MEMORY_LAYER_LABELS[String(entry['layer'] ?? '')] ?? String(entry['layer'] ?? '')
  return (
    <Card size="small" style={{ marginBottom: 8, borderColor: '#ffe58f', background: '#fffbe6' }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        已存入{layer} · {kind}
      </Typography.Text>
      <Typography.Paragraph style={{ margin: '4px 0 0', whiteSpace: 'pre-wrap' }}>
        {content}
      </Typography.Paragraph>
    </Card>
  )
}

function FinalCardsView(props: {
  cards: Array<Record<string, unknown>>
  streaming: boolean
  onQuickReply: (text: string) => void
}): React.JSX.Element | null {
  const { cards, streaming, onQuickReply } = props
  if (cards.length === 0) return null
  return (
    <>
      {cards.map((card, index) => {
        if (card['type'] === 'todo_list' && Array.isArray(card['items'])) {
          return (
            <TodoListCardView
              key={index}
              items={card['items'] as Array<Record<string, unknown>>}
              streaming={streaming}
              onQuickReply={onQuickReply}
            />
          )
        }
        if (card['type'] === 'bi_result') {
          return <BiResultCardView key={index} card={card} />
        }
        if (card['type'] === 'weekly_report') {
          return <WeeklyReportCardView key={index} card={card} />
        }
        if (card['type'] === 'memory_save' && card['entry'] instanceof Object) {
          return <MemorySaveCardView key={index} entry={card['entry'] as Record<string, unknown>} />
        }
        const entries = Object.entries(card).filter(([key]) => !HIDDEN_FIELDS.has(key))
        const hasDocNo = typeof card['doc_no'] === 'string'
        return (
          <Card
            key={index}
            size="small"
            style={{
              marginBottom: 8,
              borderColor: hasDocNo ? '#b7eb8f' : undefined,
              background: hasDocNo ? '#f6ffed' : '#fafafa'
            }}
          >
            <Descriptions column={1} size="small">
              {entries.map(([key, value]) => (
                <Descriptions.Item key={key} label={fieldLabel(key)}>
                  {fieldValueText(key, value)}
                </Descriptions.Item>
              ))}
            </Descriptions>
          </Card>
        )
      })}
    </>
  )
}

function AssistantTurnView(props: {
  turn: AssistantTurn
  confirmBusy: boolean
  streaming: boolean
  onConfirm: (token: string, action: 'confirm' | 'reject') => void
  onExpire: (token: string) => void
  onQuickReply: (text: string) => void
}): React.JSX.Element {
  const { turn } = props
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 16 }}>
      <div
        style={{
          maxWidth: BUBBLE_MAX_WIDTH,
          padding: '12px 14px',
          borderRadius: 12,
          borderBottomLeftRadius: 4,
          background: '#fff',
          border: '1px solid #f0f0f0'
        }}
      >
        {turn.stages.length > 0 && <StageTimeline steps={turn.stages} />}
        {turn.draft && <DraftCardView event={turn.draft} />}
        {turn.confirm && (
          <ConfirmCardView
            turn={turn}
            confirmBusy={props.confirmBusy}
            onConfirm={props.onConfirm}
            onExpire={props.onExpire}
          />
        )}
        {turn.finalText && (
          <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 8 }}>
            {turn.finalText}
          </Typography.Paragraph>
        )}
        {turn.finalCards && (
          <FinalCardsView
            cards={turn.finalCards}
            streaming={props.streaming}
            onQuickReply={props.onQuickReply}
          />
        )}
        {turn.failed && <Alert type="error" showIcon message={turn.failed} />}
        {!turn.finalText && !turn.confirm && !turn.draft && !turn.failed && turn.stages.length > 0 && (
          <Spin size="small" />
        )}
      </div>
    </div>
  )
}

export function ChatTurnView(props: {
  turn: Turn
  confirmBusy: boolean
  streaming: boolean
  onConfirm: (token: string, action: 'confirm' | 'reject') => void
  onExpire: (token: string) => void
  onQuickReply: (text: string) => void
}): React.JSX.Element {
  if (props.turn.role === 'user') {
    return <UserTurnView turn={props.turn} />
  }
  return (
    <AssistantTurnView
      turn={props.turn}
      confirmBusy={props.confirmBusy}
      streaming={props.streaming}
      onConfirm={props.onConfirm}
      onExpire={props.onExpire}
      onQuickReply={props.onQuickReply}
    />
  )
}
