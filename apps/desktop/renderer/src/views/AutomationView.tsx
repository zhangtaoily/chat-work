// 自动化页（PLAN P2.4/P2.6，PRD 3.6）：任务列表/新建/暂停/恢复/删除/手动触发/结果信箱
import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography
} from 'antd'
import { BellOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  automationInbox,
  createAutomation,
  deleteAutomation,
  fireAutomationEvent,
  listAutomations,
  listSkills,
  markInboxRead,
  pauseAutomation,
  resumeAutomation,
  type AutomationTask,
  type InboxMessage
} from '../lib/api'

const MIN_INTERVAL_MINUTES = 15 // 防护栏：调度间隔下限（PRD 3.6.2）

function fmtTime(value: string | null): string {
  if (!value) return '-'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
}

function scheduleText(task: AutomationTask): string {
  const s = task.schedule
  if (s.type === 'event' || s.event_name) return `事件：${s.event_name ?? '-'}`
  if (s.interval_minutes) return `每 ${s.interval_minutes} 分钟`
  return JSON.stringify(s)
}

function statusTag(status: string) {
  return status === 'active' ? <Tag color="green">运行中</Tag> : <Tag color="orange">已暂停</Tag>
}

export default function AutomationView() {
  const [tab, setTab] = useState<'tasks' | 'inbox'>('tasks')
  const [tasks, setTasks] = useState<AutomationTask[]>([])
  const [loading, setLoading] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [skillOptions, setSkillOptions] = useState<{ value: string; label: string }[]>([])
  const [scheduleKind, setScheduleKind] = useState<'interval' | 'event'>('interval')
  const [fireEventName, setFireEventName] = useState('')
  const [firing, setFiring] = useState(false)
  // 信箱
  const [inbox, setInbox] = useState<InboxMessage[]>([])
  const [inboxLoading, setInboxLoading] = useState(false)
  const [form] = Form.useForm()

  const loadTasks = useCallback(async () => {
    setLoading(true)
    try {
      const result = await listAutomations()
      setTasks(result.items)
    } catch (err) {
      message.error(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadInbox = useCallback(async () => {
    setInboxLoading(true)
    try {
      const result = await automationInbox(50)
      setInbox(result.items)
    } catch (err) {
      message.error(err instanceof Error ? err.message : '信箱加载失败')
    } finally {
      setInboxLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadTasks()
  }, [loadTasks])

  useEffect(() => {
    if (tab === 'inbox') void loadInbox()
  }, [tab, loadInbox])

  // 新建弹窗打开时拉可选只读技能（PRD 3.6.2：仅只读技能可自动化）
  const openCreate = async () => {
    setCreateOpen(true)
    try {
      const result = await listSkills()
      setSkillOptions(
        result.items
          .filter((s) => s.status === 'published' && s.rw === 'r')
          .map((s) => ({ value: s.name, label: `${s.title}（${s.name}）` }))
      )
    } catch (err) {
      message.error(err instanceof Error ? err.message : '技能清单加载失败')
    }
  }

  const handleCreate = async () => {
    try {
      const values = await form.validateFields()
      setCreating(true)
      const schedule =
        scheduleKind === 'interval'
          ? { type: 'interval', interval_minutes: values.interval_minutes }
          : { type: 'event', event_name: values.event_name }
      await createAutomation({
        name: values.name,
        skill: values.skill,
        schedule
      })
      message.success('任务已创建')
      setCreateOpen(false)
      form.resetFields()
      await loadTasks()
    } catch (err) {
      if (err instanceof Error) message.error(err.message)
    } finally {
      setCreating(false)
    }
  }

  const withAction = async (action: () => Promise<unknown>, okMsg: string) => {
    try {
      await action()
      message.success(okMsg)
      await loadTasks()
    } catch (err) {
      message.error(err instanceof Error ? err.message : '操作失败')
    }
  }

  const handleFire = async () => {
    const event = fireEventName.trim()
    if (!event) return
    setFiring(true)
    try {
      const result = await fireAutomationEvent(event)
      const matched = (result as { matched?: number }).matched ?? 0
      message.success(`事件 ${event} 已触发，命中 ${matched} 个任务`)
      await loadTasks()
    } catch (err) {
      message.error(err instanceof Error ? err.message : '触发失败')
    } finally {
      setFiring(false)
    }
  }

  const handleMarkRead = async () => {
    try {
      const marked = await markInboxRead()
      message.success(`已标记 ${marked} 条为已读`)
      await loadInbox()
    } catch (err) {
      message.error(err instanceof Error ? err.message : '标记失败')
    }
  }

  const tasksPane = (
    <>
      <Space style={{ marginBottom: 16 }} wrap>
        <Button icon={<ReloadOutlined />} onClick={() => void loadTasks()} loading={loading} />
        <Button type="primary" icon={<PlusOutlined />} onClick={() => void openCreate()}>
          新建任务
        </Button>
        <Input.Search
          allowClear
          placeholder="手动触发事件名，例如 order.created"
          style={{ width: 260 }}
          value={fireEventName}
          onChange={(e) => setFireEventName(e.target.value)}
          enterButton="触发"
          loading={firing}
          onSearch={() => void handleFire()}
        />
      </Space>
      {loading ? (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
        </div>
      ) : tasks.length === 0 ? (
        <Empty description="暂无自动化任务（新建一个试试）" />
      ) : (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {tasks.map((task) => (
            <div
              key={task.id}
              style={{
                padding: '12px 16px',
                background: '#fff',
                border: '1px solid #f0f0f0',
                borderRadius: 8
              }}
            >
              <Space size={8} wrap>
                <Typography.Text strong>{task.name}</Typography.Text>
                <Tag color="geekblue">{task.skill}</Tag>
                {statusTag(task.status)}
                {task.failure_count > 0 ? (
                  <Tag color="red">连续失败 {task.failure_count} 次</Tag>
                ) : null}
              </Space>
              <div style={{ marginTop: 4 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {scheduleText(task)} · 下次 {fmtTime(task.next_run_at)} · 上次{' '}
                  {fmtTime(task.last_run_at)} · 共 {task.stats.runs} 次（成功 {task.stats.success} /
                  失败 {task.stats.failed}）
                </Typography.Text>
              </div>
              <Space size={8} style={{ marginTop: 8 }}>
                {task.status === 'active' ? (
                  <Button
                    size="small"
                    onClick={() => void withAction(() => pauseAutomation(task.id), '已暂停')}
                  >
                    暂停
                  </Button>
                ) : (
                  <Button
                    size="small"
                    type="primary"
                    onClick={() => void withAction(() => resumeAutomation(task.id), '已恢复')}
                  >
                    恢复
                  </Button>
                )}
                <Popconfirm
                  title="确认删除该任务？"
                  onConfirm={() => void withAction(() => deleteAutomation(task.id), '已删除')}
                >
                  <Button size="small" danger>
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            </div>
          ))}
        </Space>
      )}
    </>
  )

  const inboxPane = (
    <>
      <Space style={{ marginBottom: 16 }}>
        <Button
          icon={<ReloadOutlined />}
          onClick={() => void loadInbox()}
          loading={inboxLoading}
        />
        <Button icon={<BellOutlined />} onClick={() => void handleMarkRead()}>
          全部已读
        </Button>
      </Space>
      {inboxLoading ? (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
        </div>
      ) : inbox.length === 0 ? (
        <Empty description="信箱为空（任务执行结果与自动暂停通知会送到这里）" />
      ) : (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {inbox.map((msg) => (
            <div
              key={msg.id}
              style={{
                padding: '12px 16px',
                background: '#fff',
                border: '1px solid #f0f0f0',
                borderRadius: 8,
                borderLeft: `3px solid ${msg.ok ? '#52c41a' : '#ff4d4f'}`
              }}
            >
              <Space size={8} wrap>
                <Tag color={msg.ok ? 'green' : 'red'}>{msg.ok ? '成功' : '失败'}</Tag>
                <Typography.Text strong>{msg.task_name}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {msg.skill}
                </Typography.Text>
                {!msg.read ? <Tag color="blue">未读</Tag> : null}
              </Space>
              <Typography.Paragraph
                style={{ marginTop: 8, marginBottom: 0, whiteSpace: 'pre-wrap' }}
                ellipsis={{ rows: 4, expandable: true }}
              >
                {msg.text}
              </Typography.Paragraph>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {fmtTime(msg.run_at)}
              </Typography.Text>
            </div>
          ))}
        </Space>
      )}
    </>
  )

  return (
    <div style={{ padding: 24, maxWidth: 960, margin: '0 auto', width: '100%' }}>
      <Tabs
        activeKey={tab}
        onChange={(key) => setTab(key as 'tasks' | 'inbox')}
        items={[
          { key: 'tasks', label: '我的任务', children: tasksPane },
          { key: 'inbox', label: '结果信箱', children: inboxPane }
        ]}
      />
      <Modal
        title="新建自动化任务"
        open={createOpen}
        confirmLoading={creating}
        onOk={() => void handleCreate()}
        onCancel={() => setCreateOpen(false)}
        okText="创建"
        cancelText="取消"
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="任务名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="例如：库存日报推送" />
          </Form.Item>
          <Form.Item name="skill" label="技能（仅只读技能）" rules={[{ required: true, message: '请选择技能' }]}>
            <Select
              showSearch
              placeholder="选择已上架的只读技能"
              options={skillOptions}
              optionFilterProp="label"
            />
          </Form.Item>
          <Form.Item label="调度方式">
            <Segmented
              value={scheduleKind}
              onChange={(v) => setScheduleKind(v as 'interval' | 'event')}
              options={[
                { value: 'interval', label: '定时执行' },
                { value: 'event', label: '事件订阅' }
              ]}
            />
          </Form.Item>
          {scheduleKind === 'interval' ? (
            <Form.Item
              name="interval_minutes"
              label="执行间隔（分钟）"
              initialValue={MIN_INTERVAL_MINUTES}
              rules={[
                { required: true, message: '请输入间隔' },
                {
                  validator: (_rule, value) =>
                    value >= MIN_INTERVAL_MINUTES
                      ? Promise.resolve()
                      : Promise.reject(new Error(`间隔不可低于 ${MIN_INTERVAL_MINUTES} 分钟`))
                }
              ]}
            >
              <InputNumber min={MIN_INTERVAL_MINUTES} style={{ width: 160 }} />
            </Form.Item>
          ) : (
            <Form.Item
              name="event_name"
              label="订阅事件名"
              rules={[{ required: true, message: '请输入事件名' }]}
            >
              <Input placeholder="例如 order.created" />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </div>
  )
}
