// 设置页：外观（皮肤切换）+ 对话模型选择 + 客户端信息 + 管理后台跳转 + 检查更新
import { useEffect, useState } from 'react'
import { Button, Card, Descriptions, message, Select, Space, Tag, Typography } from 'antd'
import { BgColorsOutlined, CloudServerOutlined, ReloadOutlined, RobotOutlined } from '@ant-design/icons'
import type { UpdaterState } from '../../../main/updater'
import { AGENT_CORE_URL, listChatModels, selectChatModel } from '../lib/api'
import { SKINS, type SkinKey } from '../theme'

const bridge = window.chatwork

// 「跟随默认」哨兵值（Select 不便用 null）
const FOLLOW_DEFAULT = '__default__'

interface SettingsViewProps {
  skinKey: SkinKey
  onSkinChange: (key: SkinKey) => void
}

export default function SettingsView({ skinKey, onSkinChange }: SettingsViewProps) {
  const [version, setVersion] = useState('（检测中）')
  const [updateState, setUpdateState] = useState<UpdaterState | null>(null)
  const [checking, setChecking] = useState(false)
  const [models, setModels] = useState<{ name: string; model: string }[]>([])
  const [currentModel, setCurrentModel] = useState<string | null>(null)
  const [defaultModel, setDefaultModel] = useState<string | null>(null)
  const [switching, setSwitching] = useState(false)

  useEffect(() => {
    if (!bridge) {
      setVersion('web 冒烟模式')
      return
    }
    bridge
      .getAppVersion()
      .then(setVersion)
      .catch(() => setVersion('未知'))
  }, [])

  useEffect(() => {
    listChatModels()
      .then((r) => {
        setModels(r.items.map((m) => ({ name: m.name, model: m.model })))
        setCurrentModel(r.current)
        setDefaultModel(r.default)
      })
      .catch(() => setModels([]))
  }, [])

  const handleModelChange = async (value: string) => {
    const name = value === FOLLOW_DEFAULT ? null : value
    setSwitching(true)
    try {
      const r = await selectChatModel(name)
      setCurrentModel(r.current)
      message.success(name ? `已切换使用模型 ${name}` : '已恢复跟随默认模型')
    } catch (err) {
      message.error(err instanceof Error ? err.message : '切换失败')
    } finally {
      setSwitching(false)
    }
  }

  const handleCheckUpdate = async () => {
    if (!bridge) {
      message.info('web 冒烟模式不支持自动更新')
      return
    }
    setChecking(true)
    try {
      const state = await bridge.updaterCheck()
      setUpdateState(state)
      if (state.status === 'not-available') {
        message.info('未发现自动更新通道（开发态属正常现象）')
      } else if (state.status === 'error') {
        message.error(state.message ?? '检查更新失败')
      } else {
        message.success(`发现新版本 ${state.version ?? ''}，请在弹窗引导中更新`)
      }
    } finally {
      setChecking(false)
    }
  }

  const openAdmin = () => {
    const url = `${AGENT_CORE_URL}/admin`
    if (bridge) {
      void bridge.openExternal(url)
    } else {
      window.open(url, '_blank')
    }
  }

  return (
    <div style={{ padding: 24, maxWidth: 760, margin: '0 auto', width: '100%' }}>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Card title="外观" size="small">
          <Space wrap align="center">
            <BgColorsOutlined style={{ color: 'var(--ant-color-primary, #4f46e5)' }} />
            <Typography.Text>界面皮肤</Typography.Text>
            <Select
              value={skinKey}
              onChange={onSkinChange}
              style={{ width: 180 }}
              options={(Object.keys(SKINS) as SkinKey[]).map((key) => ({
                value: key,
                label: SKINS[key].name
              }))}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              切换即时生效，默认「原型 · 靛蓝」（对齐交互原型风格）
            </Typography.Text>
          </Space>
        </Card>
        <Card title="对话模型" size="small">
          {models.length === 0 ? (
            <Typography.Text type="secondary">
              管理员尚未在系统配置中注册模型，当前走服务端默认配置（LLM_BASE_URL 环境变量）
            </Typography.Text>
          ) : (
            <Space wrap align="center">
              <RobotOutlined style={{ color: 'var(--ant-color-primary, #4f46e5)' }} />
              <Typography.Text>对话模型</Typography.Text>
              <Select
                value={currentModel ?? FOLLOW_DEFAULT}
                onChange={(v) => void handleModelChange(v)}
                loading={switching}
                style={{ width: 240 }}
                options={[
                  {
                    value: FOLLOW_DEFAULT,
                    label: defaultModel ? `跟随默认（${defaultModel}）` : '跟随默认（服务端配置）'
                  },
                  ...models.map((m) => ({ value: m.name, label: `${m.name} · ${m.model}` }))
                ]}
              />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                切换后立即对新对话生效；模型由管理员在管理后台「系统配置」中注册
              </Typography.Text>
            </Space>
          )}
        </Card>
        <Card title="客户端" size="small">
          <Descriptions column={1} size="small">
            <Descriptions.Item label="应用版本">
              <Tag color="blue">{version}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="agent-core 地址">
              <Typography.Text code>{AGENT_CORE_URL}</Typography.Text>
            </Descriptions.Item>
          </Descriptions>
          <Button
            icon={<ReloadOutlined />}
            size="small"
            loading={checking}
            onClick={() => void handleCheckUpdate()}
          >
            检查更新
          </Button>
          {updateState && updateState.status !== 'not-available' && updateState.status !== 'error' ? (
            <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
              更新状态：{updateState.status}
              {updateState.version ? ` · ${updateState.version}` : ''}
            </Typography.Paragraph>
          ) : null}
        </Card>
        <Card title="管理后台" size="small">
          <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
            技能评审、知识库审核、审计与配置等管理功能在独立 Web 后台提供（PRD 5.6），
            权限由服务端校验（system_admin / security_reviewer / knowledge_manager / auditor）。
          </Typography.Paragraph>
          <Button type="primary" icon={<CloudServerOutlined />} onClick={openAdmin}>
            打开管理后台
          </Button>
        </Card>
      </Space>
    </div>
  )
}
