// 会话主界面：消息流 + SSE 消费 + HITL 确认交互（PRD 5.2 场景 1-1）
// Electron：SSO 登录门 + 身份由 id_token.sub 注入（PRD 8.5）；web 冒烟模式无桥直传（MVP 惯例）
import { Alert, Button, ConfigProvider, Input, Layout, Menu, Modal, Space, Spin, Tag, Tooltip, Typography } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import {
  AppstoreOutlined,
  AudioOutlined,
  BookOutlined,
  BulbOutlined,
  ClockCircleOutlined,
  CloudServerOutlined,
  CommentOutlined,
  DownloadOutlined,
  LogoutOutlined,
  SafetyOutlined,
  SendOutlined,
  SettingOutlined,
  SoundOutlined
} from '@ant-design/icons'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { AuthStatus } from '../../main/auth-core'
import type { UpdaterState } from '../../main/updater'
import { ChatTurnView } from './chat/ChatTurn'
import {
  applyEvent,
  isAssistant,
  nextTurnId,
  type AssistantTurn,
  type Turn,
  type UserTurn
} from './chat/chatModel'
import {
  AGENT_CORE_URL,
  ClientVersionTooLowError,
  ConfirmationExpiredError,
  streamChat,
  submitConfirmation
} from './lib/api'
import {
  cancelSpeak,
  createRecognizer,
  speak,
  speechInputSupported,
  type Recognizer
} from './lib/speech'
import MarketView from './views/MarketView'
import KnowledgeView from './views/KnowledgeView'
import AutomationView from './views/AutomationView'
import MemoryView from './views/MemoryView'
import SettingsView from './views/SettingsView'

const bridge = window.chatwork

// web 冒烟模式（vite dev:web，无 preload 桥）沿用 MVP 直传身份
const WEB_SMOKE_USER_ID = 'u001'

// 左侧导航视图（对齐 prototype.html：工作台/个人两组菜单）
type ViewKey = 'chat' | 'market' | 'kb' | 'auto' | 'memory' | 'settings'

const viewTitles: Record<ViewKey, string> = {
  chat: 'Chat-Work 企业内网 AI Agent',
  market: '技能市场',
  kb: '知识库',
  auto: '自动化',
  memory: '我的记忆',
  settings: '设置'
}

const menuItems = [
  {
    type: 'group' as const,
    label: '工作台',
    children: [
      { key: 'chat', icon: <CommentOutlined />, label: '会话' },
      { key: 'market', icon: <AppstoreOutlined />, label: '技能市场' },
      { key: 'kb', icon: <BookOutlined />, label: '知识库' },
      { key: 'auto', icon: <ClockCircleOutlined />, label: '自动化' },
      {
        key: 'admin',
        icon: <CloudServerOutlined />,
        label: (
          <span>
            管理后台 <Tag style={{ marginInlineStart: 4 }}>管理员</Tag>
          </span>
        )
      }
    ]
  },
  {
    type: 'group' as const,
    label: '个人',
    children: [
      { key: 'memory', icon: <BulbOutlined />, label: '我的记忆' },
      { key: 'settings', icon: <SettingOutlined />, label: '设置' }
    ]
  }
]

function loggedOutStatus(): AuthStatus {
  return { loggedIn: false, userId: '', userName: '', expiresAt: 0 }
}

// 语音输入能力（Chromium 内核可用；web 冒烟同为 Chromium，能力一致）
const SPEECH_INPUT_OK = speechInputSupported()
// 回复播报开关持久化（localStorage）
const TTS_PREF_KEY = 'chatwork.tts'

const EXAMPLE_PROMPTS = [
  '我下周三想请一天年假，家里有事',
  '查一下我的待办审批',
  '同意第 2 条',
  '我还有几天年假？'
]

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [confirmBusy, setConfirmBusy] = useState(false)
  // 当前视图（默认会话；对齐 prototype 左侧导航）
  const [view, setView] = useState<ViewKey>('chat')
  // null = 认证状态检测中；web 冒烟模式无桥，视为已登录的直传身份
  const [auth, setAuth] = useState<AuthStatus | null>(
    bridge
      ? null
      : { loggedIn: true, userId: WEB_SMOKE_USER_ID, userName: 'MVP 直传', expiresAt: 0 }
  )
  const [authBusy, setAuthBusy] = useState(false)
  const [authError, setAuthError] = useState('')
  // 强制升级弹窗（服务端 client_version_too_low 门禁触发，PRD 5.5.6）
  const [forceUpdate, setForceUpdate] = useState(false)
  const [updateBusy, setUpdateBusy] = useState(false)
  const [updateMsg, setUpdateMsg] = useState('')
  // PLAN P3.5，PRD 语音交互：语音输入（SpeechRecognition）+ 回复播报（speechSynthesis）
  const [listening, setListening] = useState(false)
  const [voiceHint, setVoiceHint] = useState('')
  const recognizerRef = useRef<Recognizer | null>(null)
  // 播报开关（localStorage 持久化，车间/仓库免手场景）
  const [ttsOn, setTtsOn] = useState(() => localStorage.getItem(TTS_PREF_KEY) === 'on')
  // 会话 ID 全轮固定（幂等键组成部分）；刷新即新会话
  const sessionRef = useRef(crypto.randomUUID())
  const streamRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!bridge) return
    bridge
      .getAuthStatus()
      .then(setAuth)
      .catch(() => setAuth(loggedOutStatus()))
  }, [])

  const handleLogin = useCallback(async () => {
    if (!bridge) return
    setAuthBusy(true)
    setAuthError('')
    try {
      setAuth(await bridge.login())
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : '登录失败，请重试')
    } finally {
      setAuthBusy(false)
    }
  }, [])

  const handleLogout = useCallback(async () => {
    if (!bridge) return
    setAuthBusy(true)
    try {
      await bridge.logout()
      setAuth(loggedOutStatus())
    } finally {
      setAuthBusy(false)
    }
  }, [])

  // 管理后台：独立 Web 页面（PRD 5.6 分阶段形态），权限由服务端校验
  const openAdmin = useCallback(() => {
    const url = `${AGENT_CORE_URL}/admin`
    if (bridge) {
      void bridge.openExternal(url)
    } else {
      window.open(url, '_blank')
    }
  }, [])

  const handleMenuClick = useCallback(
    (info: { key: string }) => {
      if (info.key === 'admin') {
        openAdmin()
        return
      }
      setView(info.key as ViewKey)
    },
    [openAdmin]
  )

  useEffect(() => {
    streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight })
  }, [turns])

  const patchAssistant = useCallback(
    (turnId: string, patch: (turn: AssistantTurn) => AssistantTurn) => {
      setTurns((prev) =>
        prev.map((turn) =>
          turn.id === turnId && isAssistant(turn) ? patch(turn) : turn
        )
      )
    },
    []
  )

  const appendAssistant = useCallback((partial: Partial<AssistantTurn>) => {
    const turn: AssistantTurn = {
      id: nextTurnId('a'),
      role: 'assistant',
      stages: [],
      confirmState: 'pending',
      ...partial
    }
    setTurns((prev) => [...prev, turn])
  }, [])

  // 语音输入：点击开始听写，实时文本进输入框；说完停顿自动结束，再次点击手动停止
  const toggleListening = useCallback(() => {
    if (listening) {
      recognizerRef.current?.stop()
      return
    }
    setVoiceHint('')
    const rec = createRecognizer({
      onPartial: (text) => setInput(text),
      onError: (message) => setVoiceHint(message),
      onEnd: () => {
        recognizerRef.current = null
        setListening(false)
      }
    })
    recognizerRef.current = rec
    try {
      rec.start()
      setListening(true)
    } catch {
      // start 抛错（如重复启动），回退收起
      recognizerRef.current = null
      setListening(false)
    }
  }, [listening])

  // 播报开关切换（关闭时打断正在进行的播报）
  const toggleTts = useCallback(() => {
    setTtsOn((prev) => {
      const next = !prev
      localStorage.setItem(TTS_PREF_KEY, next ? 'on' : 'off')
      if (!next) cancelSpeak()
      return next
    })
  }, [])

  // 发送一段文本（输入框回车 / 示例快捷键 / 待办行内按钮共用）
  const sendText = useCallback(
    async (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || streaming) return
      setInput('')
      setStreaming(true)

      const userTurn: UserTurn = { id: nextTurnId('u'), role: 'user', text: trimmed }
      const assistantId = nextTurnId('a')
      setTurns((prev) => [
        ...prev,
        userTurn,
        { id: assistantId, role: 'assistant', stages: [], confirmState: 'pending' } satisfies AssistantTurn
      ])

      try {
        await streamChat(
          {
            session_id: sessionRef.current,
            user_id: auth?.loggedIn ? auth.userId : WEB_SMOKE_USER_ID,
            message: trimmed
          },
          (event) => {
            patchAssistant(assistantId, (turn) => applyEvent(turn, event))
            // final 回复播报（车间/仓库免手场景，PLAN P3.5）
            if (event.type === 'final' && ttsOn) speak(event.text)
          }
        )
      } catch (err) {
        // 强制升级门禁：弹更新引导而非普通失败提示（PRD 5.5.6 强制升级口径）
        if (err instanceof ClientVersionTooLowError) {
          setForceUpdate(true)
        }
        patchAssistant(assistantId, (turn) => ({
          ...turn,
          failed: err instanceof Error ? err.message : '网络异常，请检查 agent-core 服务'
        }))
      } finally {
        setStreaming(false)
      }
    },
    [streaming, patchAssistant, auth?.userId, ttsOn]
  )

  const send = useCallback(() => {
    void sendText(input)
  }, [sendText, input])

  // 强制升级：检查 → 下载 → 退出安装（electron-updater，内网 update-server 通道）
  const handleUpdate = useCallback(async () => {
    if (!bridge) return
    setUpdateBusy(true)
    setUpdateMsg('')
    try {
      const checked: UpdaterState = await bridge.updaterCheck()
      if (checked.status === 'error') {
        setUpdateMsg(checked.message ?? '检查更新失败')
        return
      }
      if (checked.status === 'not-available') {
        setUpdateMsg('未发现自动更新通道，请到内网门户下载最新版')
        return
      }
      if (checked.status !== 'downloaded') {
        setUpdateMsg('正在下载更新…')
        const downloaded = await bridge.updaterDownload()
        if (downloaded.status === 'error') {
          setUpdateMsg(downloaded.message ?? '下载更新失败')
          return
        }
      }
      setUpdateMsg('下载完成，即将退出并安装…')
      await bridge.updaterInstall()
    } catch (err) {
      setUpdateMsg(err instanceof Error ? err.message : '更新失败，请稍后重试')
    } finally {
      setUpdateBusy(false)
    }
  }, [])

  const patchAssistantByToken = useCallback(
    (token: string, state: AssistantTurn['confirmState']) => {
      setTurns((prev) =>
        prev.map((turn) =>
          isAssistant(turn) && turn.confirm?.confirm_token === token
            ? { ...turn, confirmState: state }
            : turn
        )
      )
    },
    []
  )

  const handleConfirm = useCallback(
    async (token: string, action: 'confirm' | 'reject') => {
      setConfirmBusy(true)
      try {
        const result = await submitConfirmation(token, action)
        patchAssistantByToken(token, action === 'confirm' ? 'confirmed' : 'rejected')
        if (action === 'confirm' && result.final) {
          appendAssistant({
            finalText: result.final.text,
            finalCards: result.final.cards
          })
        } else if (action === 'reject') {
          appendAssistant({ finalText: result.message ?? '已取消提交', finalCards: [] })
        }
      } catch (err) {
        if (err instanceof ConfirmationExpiredError) {
          patchAssistantByToken(token, 'expired')
        } else {
          appendAssistant({
            failed: err instanceof Error ? err.message : '确认请求失败'
          })
        }
      } finally {
        setConfirmBusy(false)
      }
    },
    [appendAssistant, patchAssistantByToken]
  )

  const handleExpire = useCallback(
    (token: string) => {
      patchAssistantByToken(token, 'expired')
    },
    [patchAssistantByToken]
  )

  // 认证状态检测中
  if (!auth) {
    return (
      <ConfigProvider locale={zhCN}>
        <div
          style={{
            height: '100vh',
            display: 'flex',
            flexDirection: 'column',
            gap: 16,
            alignItems: 'center',
            justifyContent: 'center'
          }}
        >
          <Spin size="large" />
          <Typography.Text type="secondary">正在检查登录状态…</Typography.Text>
        </div>
      </ConfigProvider>
    )
  }

  // SSO 登录门（仅 Electron 模式；PRD 8.5.8 桌面端 Loopback 登录）
  if (bridge && !auth.loggedIn) {
    return (
      <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#1677ff' } }}>
        <div
          style={{
            height: '100vh',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: '#f5f5f5'
          }}
        >
          <Space direction="vertical" size={16} align="center">
            <Typography.Title level={3} style={{ margin: 0 }}>
              Chat-Work 企业内网 AI Agent
            </Typography.Title>
            <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
              请使用企业账号（SSO）登录后使用
            </Typography.Paragraph>
            {authError ? <Alert type="error" showIcon message={authError} /> : null}
            <Button
              type="primary"
              size="large"
              icon={<SafetyOutlined />}
              loading={authBusy}
              onClick={() => {
                void handleLogin()
              }}
            >
              使用企业账号登录
            </Button>
          </Space>
        </div>
      </ConfigProvider>
    )
  }

  return (
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#1677ff' } }}>
      <Layout style={{ height: '100vh' }}>
        <Layout.Sider width={200} style={{ overflow: 'auto' }}>
          <div
            style={{
              height: 32,
              margin: 16,
              color: '#fff',
              fontWeight: 600,
              textAlign: 'center',
              lineHeight: '32px',
              background: 'rgba(255,255,255,0.2)',
              borderRadius: 6
            }}
          >
            Chat-Work Agent
          </div>
          <Menu
            theme="dark"
            mode="inline"
            selectedKeys={[view]}
            onClick={handleMenuClick}
            items={menuItems}
          />
        </Layout.Sider>
        <Layout>
          <Layout.Header
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              background: '#001529'
            }}
          >
            <Typography.Title level={4} style={{ color: '#fff', margin: 0 }}>
              {view === 'chat' ? 'Chat-Work 企业内网 AI Agent' : viewTitles[view]}
            </Typography.Title>
            <Space size={8}>
              <Tag color="blue">
                当前用户：{auth.userName ? `${auth.userName}（${auth.userId}）` : auth.userId}
              </Tag>
              {bridge ? (
                <Button
                  size="small"
                  ghost
                  icon={<LogoutOutlined />}
                  loading={authBusy}
                  onClick={() => {
                    void handleLogout()
                  }}
                >
                  登出
                </Button>
              ) : (
                <Tag color="purple">MVP · OA 技能已接入</Tag>
              )}
            </Space>
          </Layout.Header>

          <Layout.Content
            style={{
              display: 'flex',
              flexDirection: 'column',
              background: '#f5f5f5',
              overflow: 'hidden'
            }}
          >
            {view === 'chat' ? (
              <>
                <div
                  ref={streamRef}
                  style={{
                    flex: 1,
                    overflowY: 'auto',
                    padding: '24px 16%',
                    boxSizing: 'border-box'
                  }}
                >
                  {turns.length === 0 ? (
                    <div style={{ textAlign: 'center', marginTop: 80 }}>
                      <Typography.Title level={3} type="secondary">
                        你好，我是你的工作助手
                      </Typography.Title>
                      <Typography.Paragraph type="secondary">
                        可以帮你提交请假申请、查询待办审批（写入操作需二次确认）
                      </Typography.Paragraph>
                      <Space wrap style={{ justifyContent: 'center' }}>
                        {EXAMPLE_PROMPTS.map((prompt) => (
                          <Button
                            key={prompt}
                            onClick={() => {
                              setInput(prompt)
                            }}
                          >
                            {prompt}
                          </Button>
                        ))}
                      </Space>
                    </div>
                  ) : (
                    turns.map((turn) => (
                      <ChatTurnView
                        key={turn.id}
                        turn={turn}
                        confirmBusy={confirmBusy}
                        streaming={streaming}
                        onConfirm={(token, action) => {
                          void handleConfirm(token, action)
                        }}
                        onExpire={handleExpire}
                        onQuickReply={(text) => {
                          void sendText(text)
                        }}
                      />
                    ))
                  )}
                </div>

                <div
                  style={{ padding: '12px 16%', background: '#fff', borderTop: '1px solid #f0f0f0' }}
                >
                  {voiceHint ? (
                    <Typography.Text
                      type="danger"
                      style={{ display: 'block', fontSize: 12, marginBottom: 4 }}
                    >
                      {voiceHint}
                    </Typography.Text>
                  ) : null}
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Tooltip
                      title={
                        !SPEECH_INPUT_OK
                          ? '当前环境不支持语音输入'
                          : listening
                            ? '停止语音输入'
                            : '语音输入'
                      }
                    >
                      <Button
                        size="large"
                        icon={<AudioOutlined />}
                        type={listening ? 'primary' : 'default'}
                        danger={listening}
                        disabled={!SPEECH_INPUT_OK || streaming}
                        onClick={toggleListening}
                      />
                    </Tooltip>
                    <Space.Compact style={{ flex: 1 }}>
                      <Input
                        size="large"
                        value={input}
                        placeholder={
                          listening
                            ? '正在聆听，请讲话…'
                            : streaming
                              ? '助手正在处理…'
                              : '输入消息，或点击左侧麦克风语音输入'
                        }
                        disabled={streaming}
                        onChange={(e) => setInput(e.target.value)}
                        onPressEnter={() => {
                          void send()
                        }}
                      />
                      <Tooltip title={ttsOn ? '回复播报：已开启' : '回复播报：已关闭'}>
                        <Button
                          size="large"
                          icon={<SoundOutlined />}
                          type={ttsOn ? 'primary' : 'default'}
                          ghost={ttsOn}
                          onClick={toggleTts}
                        />
                      </Tooltip>
                      <Button
                        type="primary"
                        size="large"
                        icon={<SendOutlined />}
                        loading={streaming}
                        onClick={() => {
                          void send()
                        }}
                      >
                        发送
                      </Button>
                    </Space.Compact>
                  </div>
                </div>
              </>
            ) : (
              <div style={{ flex: 1, overflowY: 'auto' }}>
                {view === 'market' ? (
                  <MarketView />
                ) : view === 'kb' ? (
                  <KnowledgeView />
                ) : view === 'auto' ? (
                  <AutomationView />
                ) : view === 'memory' ? (
                  <MemoryView />
                ) : (
                  <SettingsView />
                )}
              </div>
            )}
          </Layout.Content>
        </Layout>
      </Layout>

      {/* 强制升级弹窗（client_version_too_low）：不可关闭，更新后重启生效（PRD 5.5.6） */}
      <Modal
        open={forceUpdate}
        title="需要更新客户端"
        closable={false}
        keyboard={false}
        maskClosable={false}
        footer={
          <Button
            type="primary"
            icon={<DownloadOutlined />}
            loading={updateBusy}
            onClick={() => {
              void handleUpdate()
            }}
          >
            立即更新
          </Button>
        }
      >
        <Typography.Paragraph>
          当前版本过旧，已被服务端强制升级策略阻断，请更新到最新版后继续使用。
        </Typography.Paragraph>
        {updateMsg ? <Alert type={updateBusy ? 'info' : 'warning'} showIcon message={updateMsg} /> : null}
      </Modal>
    </ConfigProvider>
  )
}
