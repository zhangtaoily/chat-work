// @chat-work/protocol：共享契约包（唯一工具 Schema 源，双语言之源，ARCHITECTURE 第 7 章）
// - tools/：MCP 工具 JSON Schema（oa__* / bi__* / local__*…）
//     · Python 侧：mcp server 加载 JSON 注册工具 + pydantic 运行时校验
//     · TS 侧：pnpm gen:types（json-schema-to-typescript）生成工具参数类型
// - events/：SSE 事件类型定义（chat-events.ts，判别联合）
// 契约变更 = PR 必须双端同步（CI 校验 JSON Schema 与两端代码一致性）
export type {
  ChatEvent,
  StageProgressEvent,
  DraftCardEvent,
  ConfirmCardEvent,
  DiffCardEvent,
  MaterialCandidatesEvent,
  DocWorkbenchEvent,
  FinalEvent
} from '../events/chat-events'
