// 会话流数据模型：一轮对话 = 用户消息 + 助手回合（阶段时间线/草稿卡/确认卡/终态）
// SSE 事件增量归并（applyEvent）→ React 不可变更新
import type { ChatEvent, ConfirmCardEvent, DraftCardEvent } from '@chat-work/protocol'

export interface StageStep {
  stage: string
  message: string
}

export type ConfirmState = 'pending' | 'confirmed' | 'rejected' | 'expired'

export interface UserTurn {
  id: string
  role: 'user'
  text: string
}

export interface AssistantTurn {
  id: string
  role: 'assistant'
  stages: StageStep[]
  draft?: DraftCardEvent
  confirm?: ConfirmCardEvent
  confirmState: ConfirmState
  finalText?: string
  finalCards?: Array<Record<string, unknown>>
  failed?: string
}

export type Turn = UserTurn | AssistantTurn

let seq = 0
export function nextTurnId(prefix: string): string {
  seq += 1
  return `${prefix}-${seq}`
}

export function isAssistant(turn: Turn): turn is AssistantTurn {
  return turn.role === 'assistant'
}

/** SSE 事件归并进助手回合 */
export function applyEvent(turn: AssistantTurn, event: ChatEvent): AssistantTurn {
  switch (event.type) {
    case 'stage_progress':
      return { ...turn, stages: [...turn.stages, { stage: event.stage, message: event.message }] }
    case 'draft_card':
      return { ...turn, draft: event }
    case 'confirm_card':
      return { ...turn, confirm: event, confirmState: 'pending' }
    case 'final':
      return { ...turn, finalText: event.text, finalCards: event.cards }
    default:
      // diff_card / material_candidates / doc_workbench 为 Phase 2 事件，暂不渲染
      return turn
  }
}
