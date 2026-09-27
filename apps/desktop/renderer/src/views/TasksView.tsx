// 工作任务页（P1-2 @分身布置任务）：双视角列表（布置给我的/我布置的）+ 状态推进
// 权限在服务端 store 层：接收人可开始/完成，布置人可取消；此处按钮仅做视角裁剪
import { useCallback, useEffect, useState } from 'react'
import { Button, Empty, message, Popconfirm, Space, Spin, Tabs, Tag, Typography } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { myAssignments, updateAssignmentStatus, type WorkTask } from '../lib/api'

const STATUS_TAG: Record<WorkTask['status'], { color: string; label: string }> = {
  pending: { color: 'orange', label: '待开始' },
  in_progress: { color: 'blue', label: '进行中' },
  done: { color: 'green', label: '已完成' },
  cancelled: { color: 'default', label: '已取消' }
}

function fmtTime(value: string): string {
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
}

function statusTag(status: WorkTask['status']) {
  const t = STATUS_TAG[status] ?? { color: 'default', label: status }
  return <Tag color={t.color}>{t.label}</Tag>
}

function taskCard(
  task: WorkTask,
  perspective: 'mine' | 'assigned',
  onAdvance: (task: WorkTask, status: 'in_progress' | 'done' | 'cancelled', okMsg: string) => void
) {
  return (
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
        <Typography.Text strong>{task.title}</Typography.Text>
        {statusTag(task.status)}
        {task.deadline ? <Tag color="geekblue">截止 {task.deadline}</Tag> : null}
      </Space>
      {task.detail ? (
        <div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {task.detail}
          </Typography.Text>
        </div>
      ) : null}
      <div style={{ marginTop: 4 }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {perspective === 'mine'
            ? `布置人：${task.assigner_name}`
            : `接收人：${task.assignee_name}`}{' '}
          · {fmtTime(task.created_at)}
          {task.done_note ? ` · 备注：${task.done_note}` : ''}
        </Typography.Text>
      </div>
      <Space size={8} style={{ marginTop: 8 }}>
        {perspective === 'mine' && task.status === 'pending' ? (
          <Button
            size="small"
            onClick={() => void onAdvance(task, 'in_progress', '已开始')}
          >
            开始
          </Button>
        ) : null}
        {perspective === 'mine' && (task.status === 'pending' || task.status === 'in_progress') ? (
          <Button
            size="small"
            type={task.status === 'in_progress' ? 'primary' : 'default'}
            onClick={() => void onAdvance(task, 'done', '已完成')}
          >
            完成
          </Button>
        ) : null}
        {perspective === 'assigned' &&
        (task.status === 'pending' || task.status === 'in_progress') ? (
          <Popconfirm
            title="确认取消该任务？"
            onConfirm={() => void onAdvance(task, 'cancelled', '已取消')}
          >
            <Button size="small" danger>
              取消
            </Button>
          </Popconfirm>
        ) : null}
        {task.status === 'done' || task.status === 'cancelled' ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {fmtTime(task.updated_at)}
          </Typography.Text>
        ) : null}
      </Space>
    </div>
  )
}

export default function TasksView() {
  const [overview, setOverview] = useState<{ mine: WorkTask[]; assigned: WorkTask[] }>({
    mine: [],
    assigned: []
  })
  const [loading, setLoading] = useState(false)
  const [tab, setTab] = useState<'mine' | 'assigned'>('mine')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const result = await myAssignments()
      setOverview({ mine: result.assigned_to_me, assigned: result.assigned_by_me })
    } catch (err) {
      message.error(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const withAction = async (
    task: WorkTask,
    status: 'in_progress' | 'done' | 'cancelled',
    okMsg: string
  ) => {
    try {
      await updateAssignmentStatus(task.id, status)
      message.success(okMsg)
      await load()
    } catch (err) {
      message.error(err instanceof Error ? err.message : '操作失败')
    }
  }

  const listPane = (items: WorkTask[], perspective: 'mine' | 'assigned') =>
    loading ? (
      <div style={{ textAlign: 'center', padding: 48 }}>
        <Spin />
      </div>
    ) : items.length === 0 ? (
      <Empty
        description={
          perspective === 'mine'
            ? '暂无布置给你的任务（领导 @你的分身布置后会出现在这里）'
            : '暂无你布置的任务（@对方分身布置后会出现在这里）'
        }
      />
    ) : (
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {items.map((task) => taskCard(task, perspective, withAction))}
      </Space>
    )

  return (
    <div style={{ padding: 24, maxWidth: 960, margin: '0 auto', width: '100%' }}>
      <Space style={{ marginBottom: 8 }}>
        <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading} />
      </Space>
      <Tabs
        activeKey={tab}
        onChange={(key) => setTab(key as 'mine' | 'assigned')}
        items={[
          { key: 'mine', label: `布置给我的（${overview.mine.length}）`, children: listPane(overview.mine, 'mine') },
          {
            key: 'assigned',
            label: `我布置的（${overview.assigned.length}）`,
            children: listPane(overview.assigned, 'assigned')
          }
        ]}
      />
    </div>
  )
}
