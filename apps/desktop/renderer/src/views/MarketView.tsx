// 技能市场页（PLAN P2.3，PRD 3.4/3.5）：广场浏览/搜索/一键安装/我的技能
import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Empty,
  Input,
  List,
  message,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { installSkill, listSkills, mySkills, uninstallSkill, type SkillMeta } from '../lib/api'

const CATEGORIES = [
  { value: 'official', label: '官方技能' },
  { value: 'dept', label: '科室技能' }
]

function statusTag(status: string) {
  if (status === 'published') return <Tag color="green">已上架</Tag>
  if (status === 'draft') return <Tag color="orange">待评审</Tag>
  if (status === 'deprecated') return <Tag>已下架</Tag>
  return <Tag>{status}</Tag>
}

function SkillItem({
  skill,
  installed,
  busy,
  onInstall,
  onUninstall
}: {
  skill: SkillMeta
  installed: boolean
  busy: boolean
  onInstall: (name: string) => void
  onUninstall: (name: string) => void
}) {
  const successRate =
    skill.stats.calls > 0 ? Math.round((skill.stats.success / skill.stats.calls) * 100) : null
  return (
    <List.Item
      actions={
        skill.status === 'published'
          ? [
              installed ? (
                <Button
                  key="uninstall"
                  size="small"
                  disabled={busy}
                  onClick={() => onUninstall(skill.name)}
                >
                  卸载
                </Button>
              ) : (
                <Button
                  key="install"
                  size="small"
                  type="primary"
                  loading={busy}
                  onClick={() => onInstall(skill.name)}
                >
                  一键安装
                </Button>
              )
            ]
          : []
      }
    >
      <List.Item.Meta
        title={
          <Space size={8}>
            <Typography.Text strong>{skill.title}</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {skill.name}@{skill.version}
            </Typography.Text>
            <Tag color={skill.rw === 'rw' ? 'red' : 'blue'}>{skill.rw === 'rw' ? '读写' : '只读'}</Tag>
            {skill.dept_scope ? <Tag color="purple">{skill.dept_scope}</Tag> : null}
            {statusTag(skill.status)}
          </Space>
        }
        description={
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            调用 {skill.stats.calls} 次
            {successRate !== null ? ` · 成功率 ${successRate}%` : ''}
          </Typography.Text>
        }
      />
    </List.Item>
  )
}

export default function MarketView() {
  const [tab, setTab] = useState<'market' | 'mine'>('market')
  const [q, setQ] = useState('')
  const [category, setCategory] = useState<string | undefined>()
  const [items, setItems] = useState<SkillMeta[]>([])
  const [installedNames, setInstalledNames] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [busyName, setBusyName] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      // 广场页并行拉「我的技能」以判定已安装态；我的技能页仅拉本人清单
      const [listResult, mineResult] = await Promise.all([
        tab === 'market'
          ? listSkills({ q: q || undefined, category })
          : Promise.resolve({ items: [] as SkillMeta[] }),
        mySkills()
      ])
      const mine = tab === 'mine' ? mineResult.items : listResult.items
      setItems(mine)
      setInstalledNames(new Set(mineResult.items.map((s) => s.name)))
    } catch (err) {
      message.error(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [tab, q, category])

  useEffect(() => {
    void load()
  }, [load])

  const handleInstall = async (name: string) => {
    setBusyName(name)
    try {
      await installSkill(name)
      message.success(`技能 ${name} 已安装`)
      await load()
    } catch (err) {
      message.error(err instanceof Error ? err.message : '安装失败')
    } finally {
      setBusyName('')
    }
  }

  const handleUninstall = async (name: string) => {
    setBusyName(name)
    try {
      await uninstallSkill(name)
      message.success(`技能 ${name} 已卸载`)
      await load()
    } catch (err) {
      message.error(err instanceof Error ? err.message : '卸载失败')
    } finally {
      setBusyName('')
    }
  }

  return (
    <div style={{ padding: 24, maxWidth: 960, margin: '0 auto', width: '100%' }}>
      <Tabs
        activeKey={tab}
        onChange={(key) => setTab(key as 'market' | 'mine')}
        items={[
          { key: 'market', label: '技能广场' },
          { key: 'mine', label: '我的技能' }
        ]}
        tabBarExtraContent={
          <Space>
            {tab === 'market' ? (
              <>
                <Input.Search
                  allowClear
                  placeholder="搜索技能名称"
                  style={{ width: 200 }}
                  onSearch={setQ}
                />
                <Select
                  allowClear
                  placeholder="分类"
                  style={{ width: 120 }}
                  options={CATEGORIES}
                  value={category}
                  onChange={setCategory}
                />
              </>
            ) : null}
            <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading} />
          </Space>
        }
      />
      {loading ? (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
        </div>
      ) : items.length === 0 ? (
        <Empty description={tab === 'market' ? '暂无匹配技能' : '尚未安装任何技能'} />
      ) : (
        <List
          dataSource={items}
          renderItem={(skill) => (
            <SkillItem
              skill={skill}
              installed={installedNames.has(skill.name)}
              busy={busyName === skill.name}
              onInstall={(name) => void handleInstall(name)}
              onUninstall={(name) => void handleUninstall(name)}
            />
          )}
        />
      )}
    </div>
  )
}
