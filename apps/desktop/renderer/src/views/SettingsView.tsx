// 设置页（最小可用版）：客户端信息 + 管理后台跳转 + 检查更新
import { useEffect, useState } from 'react'
import { Button, Card, Descriptions, message, Space, Tag, Typography } from 'antd'
import { CloudServerOutlined, ReloadOutlined } from '@ant-design/icons'
import type { UpdaterState } from '../../../main/updater'
import { AGENT_CORE_URL } from '../lib/api'

const bridge = window.chatwork

export default function SettingsView() {
  const [version, setVersion] = useState('（检测中）')
  const [updateState, setUpdateState] = useState<UpdaterState | null>(null)
  const [checking, setChecking] = useState(false)

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
