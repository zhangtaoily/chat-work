// 我的记忆页（PLAN P3.3，PRD 9.1-9.3）：PIPL 知情同意开关 / L2+L3 清单 /
// 手动添加 / 单条删除 / 一键清除 / L2→L3 升级 / MEMORY.md 导出
import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  message,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
  Typography
} from 'antd'
import {
  DeleteOutlined,
  DownloadOutlined,
  PlusOutlined,
  ReloadOutlined,
  RiseOutlined
} from '@ant-design/icons'
import {
  addMemory,
  clearMemory,
  deleteMemory,
  exportMemoryMd,
  listMemory,
  promoteMemory,
  setMemoryConsent,
  type MemoryEntry
} from '../lib/api'

const bridge = window.chatwork
// web 冒烟模式（无 preload 桥）沿用 App.tsx 的直传身份
const WEB_SMOKE_USER_ID = 'u001'

const KIND_OPTIONS = [
  { value: 'preference', label: '偏好' },
  { value: 'params', label: '常用参数' },
  { value: 'habit', label: '操作习惯' },
  { value: 'faq', label: '常见问题' }
]

const KIND_COLORS: Record<string, string> = {
  preference: 'blue',
  params: 'cyan',
  habit: 'purple',
  faq: 'geekblue'
}

function sourceTag(source: string) {
  if (source === 'auto_consolidated') return <Tag color="orange">自动沉淀</Tag>
  if (source === 'promoted') return <Tag color="gold">科室提炼</Tag>
  return <Tag>手动添加</Tag>
}

function fmtTime(value: string | null): string {
  if (!value) return '-'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleDateString()
}

export default function MemoryView() {
  const [loading, setLoading] = useState(false)
  const [items, setItems] = useState<MemoryEntry[]>([])
  const [stats, setStats] = useState({ personal: 0, dept: 0, auto_consolidated: 0, references: 0 })
  const [consentGranted, setConsentGranted] = useState(false)
  const [consentAt, setConsentAt] = useState<string | null>(null)
  const [layer, setLayer] = useState<'all' | 'L2' | 'L3'>('all')
  const [newContent, setNewContent] = useState('')
  const [newKind, setNewKind] = useState('preference')
  const [adding, setAdding] = useState(false)
  const [userId, setUserId] = useState(WEB_SMOKE_USER_ID)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!bridge) return
    bridge
      .getAuthStatus()
      .then((s: { loggedIn: boolean; userId?: string }) => {
        if (s.loggedIn && s.userId) setUserId(s.userId)
      })
      .catch(() => {})
  }, [])

  const load = useCallback(
    async (layerArg?: 'L2' | 'L3') => {
      setLoading(true)
      setError('')
      try {
        const result = await listMemory(layerArg)
        setItems(result.items)
        setStats(result.stats)
        setConsentGranted(result.consent.granted)
        setConsentAt(result.consent.at)
      } catch (err) {
        setError(err instanceof Error ? err.message : '记忆加载失败')
      } finally {
        setLoading(false)
      }
    },
    []
  )

  useEffect(() => {
    void load(layer === 'all' ? undefined : layer)
  }, [load, layer])

  const handleConsent = useCallback(async (granted: boolean) => {
    try {
      const res = await setMemoryConsent(granted)
      setConsentGranted(granted)
      if (granted) {
        message.success('已开启个人记忆（PIPL 知情同意已记录）')
      } else {
        message.info(`已撤回同意并清除个人记忆（${res.purged} 条）`)
      }
      void load(layer === 'all' ? undefined : layer)
    } catch (err) {
      message.error(err instanceof Error ? err.message : '设置失败')
    }
  }, [load, layer])

  const revokeWithConfirm = useCallback(() => {
    Modal.confirm({
      title: '撤回个人记忆同意？',
      content: '撤回后将立即删除您的全部个人记忆（L2），且不再自动记录；组织记忆（L3）不受影响。',
      okText: '撤回并清除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => handleConsent(false)
    })
  }, [handleConsent])

  const handleAdd = useCallback(async () => {
    const content = newContent.trim()
    if (!content) return
    setAdding(true)
    try {
      await addMemory(content, newKind)
      setNewContent('')
      message.success('已记入个人记忆')
      void load(layer === 'all' ? undefined : layer)
    } catch (err) {
      message.error(err instanceof Error ? err.message : '写入失败')
    } finally {
      setAdding(false)
    }
  }, [newContent, newKind, load, layer])

  const handleDelete = useCallback(
    async (entry: MemoryEntry) => {
      try {
        await deleteMemory(entry.id)
        message.success('已删除')
        void load(layer === 'all' ? undefined : layer)
      } catch (err) {
        message.error(err instanceof Error ? err.message : '删除失败')
      }
    },
    [load, layer]
  )

  const handlePromote = useCallback(
    async (entry: MemoryEntry) => {
      try {
        await promoteMemory(entry.id)
        message.success('已升级为组织记忆（本科室可见）')
        void load(layer === 'all' ? undefined : layer)
      } catch (err) {
        message.error(err instanceof Error ? err.message : '升级失败（需科室管理员）')
      }
    },
    [load, layer]
  )

  const handleClear = useCallback(async () => {
    try {
      const purged = await clearMemory()
      message.success(`已清除个人记忆（${purged} 条）`)
      void load(layer === 'all' ? undefined : layer)
    } catch (err) {
      message.error(err instanceof Error ? err.message : '清除失败')
    }
  }, [load, layer])

  const handleExport = useCallback(async () => {
    try {
      await exportMemoryMd(userId)
      message.success('已导出 MEMORY.md')
    } catch (err) {
      message.error(err instanceof Error ? err.message : '导出失败')
    }
  }, [userId])

  return (
    <div style={{ padding: 24, maxWidth: 860, margin: '0 auto', width: '100%' }}>
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
          <Space>
            <Typography.Text strong>个人记忆（PIPL 知情同意）</Typography.Text>
            <Switch
              checked={consentGranted}
              checkedChildren="已同意"
              unCheckedChildren="未开启"
              onChange={(v) => (v ? handleConsent(true) : revokeWithConfirm())}
            />
            {consentGranted && consentAt && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                同意于 {fmtTime(consentAt)}
              </Typography.Text>
            )}
          </Space>
          <Space>
            <Button icon={<DownloadOutlined />} onClick={() => void handleExport()}>
              导出 MEMORY.md
            </Button>
            <Popconfirm
              title="一键清除全部个人记忆？"
              description="组织记忆（L3）不受影响"
              okText="清除"
              okButtonProps={{ danger: true }}
              onConfirm={() => void handleClear()}
              disabled={stats.personal === 0}
            >
              <Button danger disabled={stats.personal === 0} icon={<DeleteOutlined />}>
                一键清除
              </Button>
            </Popconfirm>
            <Button icon={<ReloadOutlined />} onClick={() => void load(layer === 'all' ? undefined : layer)} />
          </Space>
        </Space>
        {!consentGranted && (
          <Alert
            style={{ marginTop: 12 }}
            type="info"
            showIcon
            message="未开启个人记忆"
            description="开启后可保存您的偏好与操作习惯（含对话中说「记住这个」的内容），并在相关任务中自动参考；敏感信息（工资/合同金额/联系方式等）不会入记忆。可随时撤回并清除。"
          />
        )}
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12, fontSize: 12 }}>
          个人 {stats.personal} 条 · 组织 {stats.dept} 条 · 自动沉淀 {stats.auto_consolidated} 条 ·
          累计参考 {stats.references} 次；90 天未引用自动降权，180 天未引用自动归档
        </Typography.Text>
      </Card>

      {consentGranted && (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Space.Compact style={{ width: '100%' }}>
            <Input
              placeholder="添加一条记忆，如：汇报偏好按周汇总、默认查华东区数据……"
              value={newContent}
              maxLength={200}
              onChange={(e) => setNewContent(e.target.value)}
              onPressEnter={() => void handleAdd()}
            />
            <Select
              value={newKind}
              options={KIND_OPTIONS}
              style={{ width: 120 }}
              onChange={(v) => setNewKind(v)}
            />
            <Button
              type="primary"
              icon={<PlusOutlined />}
              loading={adding}
              disabled={!newContent.trim()}
              onClick={() => void handleAdd()}
            >
              记住
            </Button>
          </Space.Compact>
        </Card>
      )}

      <Card size="small">
        <div style={{ marginBottom: 12 }}>
          <Segmented
            value={layer}
            onChange={(v) => setLayer(v as 'all' | 'L2' | 'L3')}
            options={[
              { value: 'all', label: '全部' },
              { value: 'L2', label: '个人记忆' },
              { value: 'L3', label: '组织记忆' }
            ]}
          />
        </div>
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
        {loading ? (
          <div style={{ textAlign: 'center', padding: 24 }}>
            <Spin />
          </div>
        ) : items.length === 0 ? (
          <Empty description="暂无记忆；对话中说「记住这个……」或手动添加" />
        ) : (
          items.map((entry) => (
            <Card
              key={entry.id}
              size="small"
              style={{ marginBottom: 8, background: '#fafafa' }}
              styles={{ body: { padding: '10px 14px' } }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', flex: 1 }}>
                  {entry.content}
                </Typography.Paragraph>
                <Space size={4}>
                  {entry.layer === 'L2' && (
                    <Tooltip title="提炼为本科室共享的组织记忆（需科室管理员）">
                      <Button
                        size="small"
                        icon={<RiseOutlined />}
                        onClick={() => void handlePromote(entry)}
                      />
                    </Tooltip>
                  )}
                  {(entry.layer === 'L2' || entry.source === 'promoted') && (
                    <Popconfirm
                      title="删除这条记忆？"
                      okText="删除"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => void handleDelete(entry)}
                    >
                      <Button size="small" danger icon={<DeleteOutlined />} />
                    </Popconfirm>
                  )}
                </Space>
              </div>
              <Space size={4} wrap style={{ marginTop: 6 }}>
                <Tag color={entry.layer === 'L2' ? 'blue' : 'gold'}>
                  {entry.layer === 'L2' ? '个人' : '组织'}
                </Tag>
                <Tag color={KIND_COLORS[entry.kind] ?? 'default'}>{KIND_OPTIONS.find((k) => k.value === entry.kind)?.label ?? entry.kind}</Tag>
                {sourceTag(entry.source)}
                {entry.decayed && <Tag color="default">长期未用·已降权</Tag>}
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  引用 {entry.use_count} 次 · {fmtTime(entry.created_at)}
                </Typography.Text>
              </Space>
            </Card>
          ))
        )}
      </Card>
    </div>
  )
}
