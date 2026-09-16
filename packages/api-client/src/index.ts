// @chat-work/api-client：agent-core REST/SSE 客户端（骨架占位）
// TODO:
//   1. pnpm gen 从 agent_core OpenAPI（/openapi.json）生成类型（openapi-typescript）
//   2. 封装 POST /chat 的 SSE 消费：fetch + ReadableStream 解析（不用 EventSource，需带 JWT header）
//   3. 封装 POST /confirmations/{token} 确认卡提交（ARCHITECTURE 4.3）
export const API_CLIENT_VERSION = '0.1.0'
