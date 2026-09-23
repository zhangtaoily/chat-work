// 类型定义：与后端契约精确对齐（PLAN P3.4，PRD 7.3 移动端）
// SSE 事件契约来自 packages/protocol/events/chat-events.ts（判别联合）
// Memory/Inbox 字段来自 services/agent_core（agent_core/memory/store.py、agent_core/automation/store.py）

/** 阶段进度事件：流水线阶段推进 */
export interface StageProgressEvent {
  type: 'stage_progress'
  stage: string
  message: string
}

/** 草稿卡事件：参数草稿（字段来源徽标：default/memory/ask/computed） */
export interface DraftCardEvent {
  type: 'draft_card'
  draft: Record<string, unknown>
  missing_fields: string[]
  draft_version: number
}

/** 确认卡事件：HITL 挂起（POST /confirmations/{token} 恢复执行） */
export interface ConfirmCardEvent {
  type: 'confirm_card'
  confirm_token: string
  payload: Record<string, unknown>
  expires_at: number
}

/** 计划卡事件：Plan 模式执行计划（复用 /confirmations/{token}） */
export interface PlanCardEvent {
  type: 'plan_card'
  plan_token: string
  payload: Record<string, unknown>
  expires_at: number
}

/** 变更卡事件：草稿字段修改 diff */
export interface DiffCardEvent {
  type: 'diff_card'
  changes: Array<{ field: string; old_value: unknown; new_value: unknown }>
  draft_version: number
}

/** 物料候选卡事件：模糊检索 Top-N（必选不猜） */
export interface MaterialCandidatesEvent {
  type: 'material_candidates'
  candidates: Array<Record<string, unknown>>
  line_key: string
}

/** 单据工作台事件：复杂单据（移动端精简为摘要展示，编辑留在桌面端） */
export interface DocWorkbenchEvent {
  type: 'doc_workbench'
  header: Record<string, unknown>
  lines: Array<Record<string, unknown>>
}

/** 终态事件：最终回复 */
export interface FinalEvent {
  type: 'final'
  text: string
  cards: Array<Record<string, unknown>>
}

/** SSE 事件判别联合 */
export type ChatEvent =
  | StageProgressEvent
  | DraftCardEvent
  | ConfirmCardEvent
  | PlanCardEvent
  | DiffCardEvent
  | MaterialCandidatesEvent
  | DocWorkbenchEvent
  | FinalEvent

/** 对话请求体（POST /chat，SSO_REQUIRED=false 时直传 user_id） */
export interface ChatRequest {
  session_id: string
  user_id: string
  message: string
  mode?: 'ask' | 'plan' | 'act'
}

/** 确认请求体（POST /confirmations/{token}） */
export interface ConfirmRequest {
  action: 'confirm' | 'reject'
  modified?: Record<string, unknown>
}

/** 对话消息（UI 渲染用：用户消息 or 助手事件序列） */
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text?: string
  events?: ChatEvent[]
  /** 时间戳（毫秒） */
  at: number
}

/** 自动化信箱条目（GET /automations/inbox，automation/store.py InboxEntry） */
export interface InboxEntry {
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

/** 记忆条目（GET /memory/list items，memory/store.py MemoryEntry） */
export interface MemoryEntry {
  id: string
  layer: 'L2' | 'L3'
  owner: string
  dept: string
  kind: string
  content: string
  source: string
  status: 'active' | 'archived'
  decayed: boolean
  created_by: string
  created_at: string
  last_used_at: string | null
  use_count: number
}

/** 记忆面板统计（GET /memory/list stats） */
export interface MemoryStats {
  personal: number
  dept: number
  auto_consolidated: number
  references: number
}

/** 记忆清单响应 */
export interface MemoryListResponse {
  items: MemoryEntry[]
  count: number
  stats: MemoryStats
  consent: { granted: boolean; at: string | null }
}

/** 审批卡片（小程序展示用，来自 final.cards 的结构化摘要） */
export interface ApprovalItem {
  id: string
  title: string
  docType: string
  amount: number
  status: 'pending' | 'approved' | 'rejected'
  applicant: string
  dept: string
  createdAt: string
  summary: string
}
