// 我的记忆页：P3 占位（PLAN 未启动，PRD 3.7 用户记忆画像）
import { Card, Empty, Space, Tag, Typography } from 'antd'

export default function MemoryView() {
  return (
    <div style={{ padding: 24, maxWidth: 760, margin: '0 auto', width: '100%' }}>
      <Card size="small">
        <Empty
          description={
            <Space direction="vertical" size={4}>
              <Typography.Text strong>我的记忆 · Phase 3 规划中</Typography.Text>
              <Typography.Text type="secondary">
                跨会话长期记忆、用户偏好画像与记忆治理将在下一阶段提供
              </Typography.Text>
            </Space>
          }
        >
          <Tag color="blue">P3</Tag>
        </Empty>
      </Card>
    </div>
  )
}
