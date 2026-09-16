// 应用外壳：AntD ConfigProvider（中文）+ 最小占位页
// TODO: 引用 @chat-work/ui 的业务卡片（确认卡/摘要卡/BI 卡…）渲染会话流；
//       SSE 消费见 lib/api（@chat-work/api-client 封装 + fetch ReadableStream 解析）
import { ConfigProvider, Layout, Typography } from 'antd'
import zhCN from 'antd/locale/zh_CN'

export default function App() {
  return (
    <ConfigProvider locale={zhCN}>
      <Layout style={{ minHeight: '100vh' }}>
        <Layout.Header>
          <Typography.Title level={4} style={{ color: '#fff', margin: 0 }}>
            Chat-Work 企业内网 AI Agent
          </Typography.Title>
        </Layout.Header>
        <Layout.Content style={{ padding: 24 }}>
          {/* TODO: 会话流 + 思考折叠块（renderer/src/chat/），业务卡片来自 packages/ui */}
          <Typography.Paragraph>骨架占位页：会话界面建设中…</Typography.Paragraph>
        </Layout.Content>
      </Layout>
    </ConfigProvider>
  )
}
