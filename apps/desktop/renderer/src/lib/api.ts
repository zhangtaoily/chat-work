// 会话 API 客户端：SSE 流式消费（POST /chat）+ HITL 确认（POST /confirmations/{token}）
// SSE 帧格式对齐 agent_core/api/main.py：`data: {json}\n\n`，终止帧 `data: [DONE]`
import type { ChatEvent, FinalEvent } from '@chat-work/protocol'

const API_BASE = import.meta.env.VITE_AGENT_CORE_URL ?? 'http://localhost:8011'
// 供管理后台等页面跳转使用（与 API 同源）
export const AGENT_CORE_URL = API_BASE

// 桌面端 preload 桥（Electron 注入）；web 冒烟模式（vite dev:web）为 undefined → 不带鉴权直传
const bridge = window.chatwork

// 版本号一次性拉取并缓存（authedFetch 每个请求都带 X-Client-Version，服务端强制升级协商）
let clientVersionPromise: Promise<string> | null = null
function clientVersion(): Promise<string> {
  const cached = clientVersionPromise
  if (cached) return cached
  const fresh = bridge ? bridge.getAppVersion().catch(() => '') : Promise.resolve('')
  clientVersionPromise = fresh
  return fresh
}

/**
 * 带鉴权 fetch：附加 Bearer 头（临近过期由主进程自动静默刷新，PRD 8.5.4）与
 * X-Client-Version 版本头（PRD 5.5.6 版本协商）；
 * 401 时强制刷新并重试一次（刷新失败返回 null → 原样透传 401，由调用方提示重新登录）
 */
async function authedFetch(url: string, init: RequestInit): Promise<Response> {
  const token = await bridge?.getAccessToken()
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const version = await clientVersion()
  if (version) headers.set('X-Client-Version', version)
  let resp = await fetch(url, { ...init, headers })
  if (resp.status === 401 && bridge) {
    const fresh = await bridge.refreshAccessToken()
    if (fresh) {
      headers.set('Authorization', `Bearer ${fresh}`)
      resp = await fetch(url, { ...init, headers })
    }
  }
  return resp
}

export interface ChatRequest {
  session_id: string
  user_id: string
  message: string
  /** 会话锁定分身工号（选择后本会话无需每条 @；消息内显式 @ 优先） */
  twin_emp_no?: string | null
}

export interface ConfirmationResult {
  status: 'ok' | 'cancelled'
  message?: string
  final?: FinalEvent
}

/** 确认卡一次性语义：pop 后重复提交 / TTL 过期 → 404 */
export class ConfirmationExpiredError extends Error {
  constructor() {
    super('确认卡不存在或已过期')
    this.name = 'ConfirmationExpiredError'
  }
}

/** 强制升级（PRD 5.5.6）：服务端判定客户端版本过旧（client_version_too_low），需更新后使用 */
export class ClientVersionTooLowError extends Error {
  constructor(message = '当前版本过旧，请更新到最新版后使用') {
    super(message)
    this.name = 'ClientVersionTooLowError'
  }
}

/** 灰度放量（PRD 5.5.6）：用户暂未命中小流量范围（gray_percent_exceeded） */
export class GrayGateError extends Error {
  constructor(message = '灰度放量中，您暂未在小流量范围，请稍候') {
    super(message)
    this.name = 'GrayGateError'
  }
}

// 403 门禁响应体解析：{"code": "...", "message": "..."} → 专用错误（对齐 agent_core /chat 门禁）
async function gateErrorFrom(resp: Response): Promise<Error | null> {
  if (resp.status !== 403) return null
  let code = ''
  let message = ''
  try {
    const body = (await resp.json()) as { code?: string; message?: string }
    code = body.code ?? ''
    message = body.message ?? ''
  } catch {
    // 非 JSON 响应体按未知 403 处理
  }
  if (code === 'client_version_too_low') return new ClientVersionTooLowError(message || undefined)
  if (code === 'gray_percent_exceeded') return new GrayGateError(message || undefined)
  return null
}

export async function streamChat(
  req: ChatRequest,
  onEvent: (event: ChatEvent) => void
): Promise<void> {
  const resp = await authedFetch(`${API_BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req)
  })
  if (resp.status === 401) {
    throw new Error('登录已过期，请重新登录（HTTP 401）')
  }
  const gateErr = await gateErrorFrom(resp)
  if (gateErr) {
    throw gateErr
  }
  if (!resp.ok || !resp.body) {
    throw new Error(`对话请求失败（HTTP ${resp.status}）`)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let sep = buffer.indexOf('\n\n')
    while (sep >= 0) {
      const frame = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      dispatchFrame(frame, onEvent)
      sep = buffer.indexOf('\n\n')
    }
  }
}

function dispatchFrame(frame: string, onEvent: (event: ChatEvent) => void): void {
  // 仅取 data: 行（服务端每帧单行 data，无 event:/id: 字段）
  const data = frame
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())
    .join('')
  if (!data || data === '[DONE]') return
  try {
    onEvent(JSON.parse(data) as ChatEvent)
  } catch {
    // 非 JSON 帧忽略（保持流稳定）
  }
}

export async function submitConfirmation(
  token: string,
  action: 'confirm' | 'reject'
): Promise<ConfirmationResult> {
  const resp = await authedFetch(`${API_BASE}/confirmations/${token}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action })
  })
  if (resp.status === 404) {
    throw new ConfirmationExpiredError()
  }
  if (!resp.ok) {
    throw new Error(`确认请求失败（HTTP ${resp.status}）`)
  }
  return (await resp.json()) as ConfirmationResult
}

// ---- 通用：FastAPI HTTPException 的 detail 中文文案透传 ----
async function detailError(resp: Response, fallback: string): Promise<Error> {
  let message = fallback
  try {
    const body = (await resp.json()) as { detail?: unknown }
    if (typeof body.detail === 'string' && body.detail) message = body.detail
  } catch {
    // 非 JSON 响应体按 fallback 处理
  }
  return new Error(message)
}

// ---- 会话锁定分身（P1-2.3：会话框选择固定分身，无需每条 @）----
export interface TwinCandidate {
  emp_no: string
  name: string
  dept: string
  profile: Record<string, string>
}

export interface TwinListResult {
  items: TwinCandidate[]
  count: number
}

export async function listTwins(): Promise<TwinListResult> {
  const resp = await authedFetch(`${API_BASE}/twins`, {})
  if (!resp.ok) throw await detailError(resp, `分身清单加载失败（HTTP ${resp.status}）`)
  return (await resp.json()) as TwinListResult
}

// ---- 对话模型（多模型可视化配置：员工自选，PRD 5.6）----
export interface ChatModelOption {
  name: string
  model: string
  base_url: string
  enabled: boolean
  api_key_masked: string
}

export interface ChatModelsResult {
  items: ChatModelOption[]
  current: string | null
  default: string | null
}

export async function listChatModels(): Promise<ChatModelsResult> {
  const resp = await authedFetch(`${API_BASE}/models`, {})
  if (!resp.ok) throw await detailError(resp, `模型清单加载失败（HTTP ${resp.status}）`)
  return (await resp.json()) as ChatModelsResult
}

export async function selectChatModel(name: string | null): Promise<{ current: string | null }> {
  const resp = await authedFetch(`${API_BASE}/me/model`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name })
  })
  if (!resp.ok) throw await detailError(resp, `切换失败（HTTP ${resp.status}）`)
  return (await resp.json()) as { current: string | null }
}

// ---- 技能市场（PLAN P2.3，PRD 3.4/3.5）----
export interface SkillStats {
  calls: number
  success: number
  failed: number
}

export interface SkillMeta {
  name: string
  title: string
  version: string
  rw: 'r' | 'rw'
  category: string
  dept_scope: string | null
  status: string
  published_at: string | null
  stats: SkillStats
}

export interface SkillListResult {
  items: SkillMeta[]
  count: number
}

export async function listSkills(
  params: { category?: string | undefined; q?: string | undefined } = {}
): Promise<SkillListResult> {
  const qs = new URLSearchParams()
  if (params.category) qs.set('category', params.category)
  if (params.q) qs.set('q', params.q)
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  const resp = await authedFetch(`${API_BASE}/skills${suffix}`, {})
  if (!resp.ok) throw await detailError(resp, `技能市场加载失败（HTTP ${resp.status}）`)
  return (await resp.json()) as SkillListResult
}

export async function mySkills(): Promise<SkillListResult> {
  const resp = await authedFetch(`${API_BASE}/skills/mine`, {})
  if (!resp.ok) throw await detailError(resp, `我的技能加载失败（HTTP ${resp.status}）`)
  return (await resp.json()) as SkillListResult
}

export async function installSkill(name: string): Promise<void> {
  const resp = await authedFetch(`${API_BASE}/skills/${encodeURIComponent(name)}/install`, {
    method: 'POST'
  })
  if (!resp.ok) throw await detailError(resp, `安装失败（HTTP ${resp.status}）`)
}

export async function uninstallSkill(name: string): Promise<void> {
  const resp = await authedFetch(`${API_BASE}/skills/${encodeURIComponent(name)}/install`, {
    method: 'DELETE'
  })
  if (!resp.ok) throw await detailError(resp, `卸载失败（HTTP ${resp.status}）`)
}

// ---- 知识库（PLAN P2.5，PRD 9.5）----
export interface KnowledgeDoc {
  doc_id: string
  title: string
  space: 'group' | 'dept'
  dept_scope: string | null
  classification: string
  status: string
  content: string
  file_type: string
  tags: string[]
  version: number
  created_at: string
  updated_at: string
}

export interface KnowledgeListResult {
  items: KnowledgeDoc[]
  count: number
}

export interface KnowledgeHit {
  doc_id: string
  title: string
  section: string
  text: string
  score: number
  classification: string
}

export async function listKnowledge(
  filters: {
    space?: string | undefined
    q?: string | undefined
    status?: string | undefined
  } = {}
): Promise<KnowledgeListResult> {
  const qs = new URLSearchParams()
  if (filters.space) qs.set('space', filters.space)
  if (filters.q) qs.set('q', filters.q)
  if (filters.status) qs.set('status', filters.status)
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  const resp = await authedFetch(`${API_BASE}/knowledge/docs${suffix}`, {})
  if (!resp.ok) throw await detailError(resp, `知识库加载失败（HTTP ${resp.status}）`)
  return (await resp.json()) as KnowledgeListResult
}

export interface KnowledgeUploadRequest {
  title: string
  content: string
  space?: 'group' | 'dept'
  dept_scope?: string | null
  classification?: 'D1' | 'D2' | 'D3'
  file_type?: string
  tags?: string[]
}

export async function uploadKnowledge(req: KnowledgeUploadRequest): Promise<KnowledgeDoc> {
  const resp = await authedFetch(`${API_BASE}/knowledge/docs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req)
  })
  if (!resp.ok) throw await detailError(resp, `上传失败（HTTP ${resp.status}）`)
  return (await resp.json()) as KnowledgeDoc
}

export async function searchKnowledge(
  query: string,
  topK = 3
): Promise<{ items: KnowledgeHit[]; count: number }> {
  const resp = await authedFetch(`${API_BASE}/knowledge/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, top_k: topK })
  })
  if (!resp.ok) throw await detailError(resp, `检索失败（HTTP ${resp.status}）`)
  return (await resp.json()) as { items: KnowledgeHit[]; count: number }
}

// ---- 自动化任务（PLAN P2.4，PRD 3.6：定时执行只读技能）----
export interface AutomationTask {
  id: string
  name: string
  skill: string
  params: Record<string, unknown>
  schedule: { type?: string; interval_minutes?: number; event_name?: string } & Record<
    string,
    unknown
  >
  owner: string
  channel: Record<string, unknown> | null
  status: 'active' | 'paused'
  created_at: string
  last_run_at: string | null
  next_run_at: string | null
  failure_count: number
  stats: { runs: number; success: number; failed: number }
}

export interface AutomationListResult {
  items: AutomationTask[]
  count: number
}

export interface InboxMessage {
  id: string
  kind: string
  task_id: string
  task_name: string
  skill: string
  user_id: string
  ok: boolean
  text: string
  run_at: string
  read: boolean
}

export async function listAutomations(
  status?: 'active' | 'paused'
): Promise<AutomationListResult> {
  const suffix = status ? `?status=${status}` : ''
  const resp = await authedFetch(`${API_BASE}/automations${suffix}`, {})
  if (!resp.ok) throw await detailError(resp, `自动化任务加载失败（HTTP ${resp.status}）`)
  return (await resp.json()) as AutomationListResult
}

export interface AutomationCreateRequest {
  name: string
  skill: string
  params?: Record<string, unknown>
  schedule: Record<string, unknown>
  scope?: 'personal'
}

export async function createAutomation(req: AutomationCreateRequest): Promise<AutomationTask> {
  const resp = await authedFetch(`${API_BASE}/automations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req)
  })
  if (!resp.ok) throw await detailError(resp, `创建失败（HTTP ${resp.status}）`)
  return (await resp.json()) as AutomationTask
}

export async function pauseAutomation(taskId: string): Promise<AutomationTask> {
  const resp = await authedFetch(
    `${API_BASE}/automations/${encodeURIComponent(taskId)}/pause`,
    { method: 'POST' }
  )
  if (!resp.ok) throw await detailError(resp, `暂停失败（HTTP ${resp.status}）`)
  return (await resp.json()) as AutomationTask
}

export async function resumeAutomation(taskId: string): Promise<AutomationTask> {
  const resp = await authedFetch(
    `${API_BASE}/automations/${encodeURIComponent(taskId)}/resume`,
    { method: 'POST' }
  )
  if (!resp.ok) throw await detailError(resp, `恢复失败（HTTP ${resp.status}）`)
  return (await resp.json()) as AutomationTask
}

export async function deleteAutomation(taskId: string): Promise<void> {
  const resp = await authedFetch(`${API_BASE}/automations/${encodeURIComponent(taskId)}`, {
    method: 'DELETE'
  })
  if (!resp.ok) throw await detailError(resp, `删除失败（HTTP ${resp.status}）`)
}

export async function automationInbox(limit = 50): Promise<{ items: InboxMessage[]; count: number }> {
  const resp = await authedFetch(`${API_BASE}/automations/inbox?limit=${limit}`, {})
  if (!resp.ok) throw await detailError(resp, `信箱加载失败（HTTP ${resp.status}）`)
  return (await resp.json()) as { items: InboxMessage[]; count: number }
}

export async function markInboxRead(): Promise<number> {
  const resp = await authedFetch(`${API_BASE}/automations/inbox/read`, { method: 'POST' })
  if (!resp.ok) throw await detailError(resp, `标记已读失败（HTTP ${resp.status}）`)
  const body = (await resp.json()) as { marked?: number }
  return body.marked ?? 0
}

export async function fireAutomationEvent(
  event: string,
  payload: Record<string, unknown> = {}
): Promise<{ matched: number; results?: Array<Record<string, unknown>> }> {
  const resp = await authedFetch(`${API_BASE}/automations/events/fire`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event, payload })
  })
  if (!resp.ok) throw await detailError(resp, `触发失败（HTTP ${resp.status}）`)
  return (await resp.json()) as { matched: number; results?: Array<Record<string, unknown>> }
}

// ---- 我的记忆（PLAN P3.3，PRD 9.1-9.3：L2/L3 记忆管理）----
export interface MemoryEntry {
  id: string
  layer: 'L2' | 'L3'
  owner: string
  dept: string | null
  kind: string
  content: string
  source: string
  status: string
  decayed: boolean
  created_at: string
  last_used_at: string
  use_count: number
}

export interface MemoryStats {
  personal: number
  dept: number
  auto_consolidated: number
  references: number
}

export interface MemoryListResult {
  items: MemoryEntry[]
  count: number
  stats: MemoryStats
  consent: { granted: boolean; at: string | null }
}

export async function listMemory(layer?: 'L2' | 'L3'): Promise<MemoryListResult> {
  const suffix = layer ? `?layer=${layer}` : ''
  const resp = await authedFetch(`${API_BASE}/memory/list${suffix}`, {})
  if (!resp.ok) throw await detailError(resp, `记忆加载失败（HTTP ${resp.status}）`)
  return (await resp.json()) as MemoryListResult
}

export async function addMemory(content: string, kind = 'preference'): Promise<MemoryEntry> {
  const resp = await authedFetch(`${API_BASE}/memory/add`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, kind })
  })
  if (!resp.ok) throw await detailError(resp, `写入失败（HTTP ${resp.status}）`)
  return (await resp.json()) as MemoryEntry
}

export async function deleteMemory(entryId: string): Promise<void> {
  const resp = await authedFetch(`${API_BASE}/memory/${encodeURIComponent(entryId)}`, {
    method: 'DELETE'
  })
  if (!resp.ok) throw await detailError(resp, `删除失败（HTTP ${resp.status}）`)
}

export async function clearMemory(): Promise<number> {
  const resp = await authedFetch(`${API_BASE}/memory/clear`, { method: 'POST' })
  if (!resp.ok) throw await detailError(resp, `清除失败（HTTP ${resp.status}）`)
  const body = (await resp.json()) as { purged?: number }
  return body.purged ?? 0
}

export async function setMemoryConsent(granted: boolean): Promise<{ purged: number }> {
  const resp = await authedFetch(`${API_BASE}/memory/consent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ granted })
  })
  if (!resp.ok) throw await detailError(resp, `设置失败（HTTP ${resp.status}）`)
  return (await resp.json()) as { purged: number }
}

export async function promoteMemory(entryId: string): Promise<MemoryEntry> {
  const resp = await authedFetch(
    `${API_BASE}/memory/${encodeURIComponent(entryId)}/promote`,
    { method: 'POST' }
  )
  if (!resp.ok) throw await detailError(resp, `升级失败（HTTP ${resp.status}）`)
  const body = (await resp.json()) as { entry: MemoryEntry }
  return body.entry
}

/** 导出 MEMORY.md（ARCHITECTURE 4.9 可迁移）：Blob 下载 */
export async function exportMemoryMd(userId: string): Promise<void> {
  const resp = await authedFetch(`${API_BASE}/memory/export.md`, {})
  if (!resp.ok) throw await detailError(resp, `导出失败（HTTP ${resp.status}）`)
  const blob = await resp.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `MEMORY-${userId}.md`
  a.click()
  URL.revokeObjectURL(url)
}

// ---- 工作任务（P1-2 @分身布置任务：双视角列表 + 状态推进）----
export interface WorkTask {
  id: string
  title: string
  detail: string
  assigner: string
  assigner_name: string
  assignee: string
  assignee_name: string
  deadline: string
  status: 'pending' | 'in_progress' | 'done' | 'cancelled'
  created_at: string
  updated_at: string
  done_note: string
}

export interface WorkTaskOverview {
  assigned_to_me: WorkTask[]
  assigned_by_me: WorkTask[]
  count: number
}

export async function myAssignments(): Promise<WorkTaskOverview> {
  const resp = await authedFetch(`${API_BASE}/assignments`, {})
  if (!resp.ok) throw await detailError(resp, `工作任务加载失败（HTTP ${resp.status}）`)
  return (await resp.json()) as WorkTaskOverview
}

export async function updateAssignmentStatus(
  taskId: string,
  status: 'in_progress' | 'done' | 'cancelled',
  note = ''
): Promise<WorkTask> {
  const resp = await authedFetch(
    `${API_BASE}/assignments/${encodeURIComponent(taskId)}/status`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, note })
    }
  )
  if (!resp.ok) throw await detailError(resp, `状态推进失败（HTTP ${resp.status}）`)
  return (await resp.json()) as WorkTask
}
