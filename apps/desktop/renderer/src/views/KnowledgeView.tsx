// 知识库页（PLAN P2.5，PRD 9.5）：文档列表/搜索/上传 + RAG 独立检索
import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Empty,
  Form,
  Input,
  message,
  Modal,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography
} from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  listKnowledge,
  searchKnowledge,
  uploadKnowledge,
  type KnowledgeDoc,
  type KnowledgeHit
} from '../lib/api'

const SPACES = [
  { value: 'group', label: '全员空间' },
  { value: 'dept', label: '科室空间' }
]

const CLASSIFICATIONS = [
  { value: 'D1', label: 'D1（公开）' },
  { value: 'D2', label: 'D2（内部）' },
  { value: 'D3', label: 'D3（敏感）' }
]

function fmtTime(value: string | null): string {
  if (!value) return '-'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
}

function statusTag(status: string) {
  if (status === 'published') return <Tag color="green">已发布</Tag>
  if (status === 'draft') return <Tag color="orange">草稿</Tag>
  if (status === 'deprecated') return <Tag>已废弃</Tag>
  return <Tag>{status}</Tag>
}

export default function KnowledgeView() {
  // 文档列表
  const [docs, setDocs] = useState<KnowledgeDoc[]>([])
  const [loading, setLoading] = useState(false)
  const [q, setQ] = useState('')
  const [space, setSpace] = useState<string | undefined>()
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [form] = Form.useForm()
  // 知识检索
  const [hits, setHits] = useState<KnowledgeHit[] | null>(null)
  const [searching, setSearching] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const result = await listKnowledge({ q: q || undefined, space })
      setDocs(result.items)
    } catch (err) {
      message.error(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [q, space])

  useEffect(() => {
    void load()
  }, [load])

  const handleUpload = async () => {
    try {
      const values = await form.validateFields()
      setUploading(true)
      const tags = String(values.tags ?? '')
        .split(/[,，]/)
        .map((t: string) => t.trim())
        .filter(Boolean)
      await uploadKnowledge({
        title: values.title,
        content: values.content,
        space: values.space,
        classification: values.classification,
        tags
      })
      message.success('文档已上传（草稿态，管理员审核后发布）')
      setUploadOpen(false)
      form.resetFields()
      await load()
    } catch (err) {
      if (err instanceof Error) message.error(err.message)
    } finally {
      setUploading(false)
    }
  }

  const handleSearch = async (query: string) => {
    if (!query.trim()) return
    setSearching(true)
    try {
      const result = await searchKnowledge(query, 5)
      setHits(result.items)
    } catch (err) {
      message.error(err instanceof Error ? err.message : '检索失败')
    } finally {
      setSearching(false)
    }
  }

  const docTable = (
    <>
      <Space style={{ marginBottom: 16 }} wrap>
        <Input.Search
          allowClear
          placeholder="搜索标题/内容"
          style={{ width: 220 }}
          onSearch={setQ}
        />
        <Select
          allowClear
          placeholder="空间"
          style={{ width: 130 }}
          options={SPACES}
          value={space}
          onChange={setSpace}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading} />
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setUploadOpen(true)}
        >
          上传文档
        </Button>
      </Space>
      {loading ? (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
        </div>
      ) : docs.length === 0 ? (
        <Empty description="暂无文档" />
      ) : (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {docs.map((doc) => (
            <div
              key={doc.doc_id}
              style={{
                padding: '12px 16px',
                background: '#fff',
                border: '1px solid #f0f0f0',
                borderRadius: 8
              }}
            >
              <Space size={8} wrap>
                <Typography.Text strong>{doc.title}</Typography.Text>
                <Tag color={doc.space === 'group' ? 'blue' : 'purple'}>
                  {doc.space === 'group' ? '全员' : `科室 ${doc.dept_scope ?? ''}`}
                </Tag>
                <Tag>{doc.classification}</Tag>
                {statusTag(doc.status)}
                {doc.tags.map((tag) => (
                  <Tag key={tag} color="cyan">
                    {tag}
                  </Tag>
                ))}
              </Space>
              <div style={{ marginTop: 4 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {fmtTime(doc.updated_at)} 更新 · v{doc.version}
                </Typography.Text>
              </div>
            </div>
          ))}
        </Space>
      )}
    </>
  )

  const searchPane = (
    <>
      <Input.Search
        placeholder="输入问题，例如：年假天数怎么算"
        enterButton="检索"
        style={{ maxWidth: 560, marginBottom: 16 }}
        loading={searching}
        onSearch={(value) => void handleSearch(value)}
      />
      {hits === null ? (
        <Typography.Text type="secondary">检索已发布文档的语义相似片段（Top-5）</Typography.Text>
      ) : hits.length === 0 ? (
        <Empty description="未命中（已达相似度阈值门槛）" />
      ) : (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {hits.map((hit, idx) => (
            <div
              key={`${hit.doc_id}-${idx}`}
              style={{
                padding: '12px 16px',
                background: '#fff',
                border: '1px solid #f0f0f0',
                borderRadius: 8
              }}
            >
              <Space size={8} wrap>
                <Typography.Text strong>{hit.title}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {hit.section}
                </Typography.Text>
                <Tag color="geekblue">相似度 {hit.score}</Tag>
                <Tag>{hit.classification}</Tag>
              </Space>
              <Typography.Paragraph
                style={{ marginTop: 8, marginBottom: 0, whiteSpace: 'pre-wrap' }}
              >
                {hit.text}
              </Typography.Paragraph>
            </div>
          ))}
        </Space>
      )}
    </>
  )

  return (
    <div style={{ padding: 24, maxWidth: 960, margin: '0 auto', width: '100%' }}>
      <Tabs
        items={[
          { key: 'docs', label: '文档列表', children: docTable },
          { key: 'search', label: '知识检索', children: searchPane }
        ]}
      />
      <Modal
        title="上传知识文档"
        open={uploadOpen}
        confirmLoading={uploading}
        onOk={() => void handleUpload()}
        onCancel={() => setUploadOpen(false)}
        okText="上传"
        cancelText="取消"
      >
        <Form form={form} layout="vertical" initialValues={{ space: 'group', classification: 'D2' }}>
          <Form.Item name="title" label="标题" rules={[{ required: true, message: '请输入标题' }]}>
            <Input placeholder="文档标题" />
          </Form.Item>
          <Form.Item
            name="content"
            label="内容"
            rules={[{ required: true, message: '请输入内容' }]}
          >
            <Input.TextArea rows={6} placeholder="Markdown 或纯文本内容" />
          </Form.Item>
          <Space size={12}>
            <Form.Item name="space" label="空间" style={{ minWidth: 140 }}>
              <Select options={SPACES} />
            </Form.Item>
            <Form.Item name="classification" label="密级" style={{ minWidth: 150 }}>
              <Select options={CLASSIFICATIONS} />
            </Form.Item>
          </Space>
          <Form.Item name="tags" label="标签（逗号分隔，可选）">
            <Input placeholder="例如：考勤, 制度" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
