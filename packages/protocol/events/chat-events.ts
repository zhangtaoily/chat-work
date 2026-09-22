// SSE 事件契约：agent-core → 桌面端流式推送（ARCHITECTURE 4.1 / 第 7 章）
// 判别联合（discriminated union）：前端按 type 字段分发到对应卡片渲染器（packages/ui）

/** 阶段进度事件：流水线阶段推进（渲染「思考与执行」折叠块） */
export interface StageProgressEvent {
  type: 'stage_progress'
  /** 当前阶段：intent/route/extract/validate/hitl/execute/format */
  stage: string
  /** 阶段描述（如「正在识别意图…」） */
  message: string
}

/** 草稿卡事件：参数草稿（字段来源徽标：default/memory/ask/computed） */
export interface DraftCardEvent {
  type: 'draft_card'
  /** 草稿内容（表单字段键值对 + 来源标记） */
  draft: Record<string, unknown>
  /** 缺失必填字段列表（驱动 Agent 追问） */
  missing_fields: string[]
  /** 草稿版本（用户修改 +1，幂等键随版本变化） */
  draft_version: number
}

/** 确认卡事件：HITL 挂起等待用户确认（POST /confirmations/{token} 恢复执行） */
export interface ConfirmCardEvent {
  type: 'confirm_card'
  /** 确认令牌（Redis confirm:{token}，TTL 10 分钟） */
  confirm_token: string
  /** 待确认的表单内容（草稿快照） */
  payload: Record<string, unknown>
  /** 过期时间戳（毫秒） */
  expires_at: number
}

/** 计划卡事件：Plan 模式执行计划（PRD 3.3，批准后恢复执行 PLAN P2.6） */
export interface PlanCardEvent {
  type: 'plan_card'
  /** 计划令牌（复用 confirm_store，POST /confirmations/{token} 批准/拒绝） */
  plan_token: string
  /** 计划内容：skill_title + 步骤数组（tool/system/rw/requires_confirm） */
  payload: Record<string, unknown>
  /** 过期时间戳（毫秒） */
  expires_at: number
}

/** 变更卡事件：草稿字段修改 diff（旧值删除线 → 新值，金额联动重算） */
export interface DiffCardEvent {
  type: 'diff_card'
  /** 字段级变更列表 */
  changes: Array<{ field: string; old_value: unknown; new_value: unknown }>
  /** 变更后的草稿版本 */
  draft_version: number
}

/** 物料候选卡事件：模糊检索 Top-N 候选（必选不猜，Phase 2 ERP） */
export interface MaterialCandidatesEvent {
  type: 'material_candidates'
  /** 候选列表（编码/名称/规格/库存/协议价） */
  candidates: Array<Record<string, unknown>>
  /** 关联的子表行标识（用户选定后回填） */
  line_key: string
}

/** 单据工作台事件：复杂单据全屏工作台（Phase 2：主表分组折叠 + 子表 + 孙表钻取） */
export interface DocWorkbenchEvent {
  type: 'doc_workbench'
  /** 主表（含分组元数据 groups，驱动分组折叠） */
  header: Record<string, unknown>
  /** 子表行（每行 material_ref 指向已选物料候选；孙表 batches 懒加载） */
  lines: Array<Record<string, unknown>>
}

/** 终态事件：最终回复（文本 + 卡片，本轮对话结束） */
export interface FinalEvent {
  type: 'final'
  /** 回复文本 */
  text: string
  /** 附带卡片（单据摘要卡/成功卡等，可为空数组） */
  cards: Array<Record<string, unknown>>
}

/** SSE 事件判别联合（全部事件类型） */
export type ChatEvent =
  | StageProgressEvent
  | DraftCardEvent
  | ConfirmCardEvent
  | PlanCardEvent
  | DiffCardEvent
  | MaterialCandidatesEvent
  | DocWorkbenchEvent
  | FinalEvent
