"""FastAPI 入口：健康检查 + 对话接口占位。

对话为 SSE 流式响应，事件类型定义见 packages/protocol/events/chat-events.ts
（stage_progress / draft_card / confirm_card / diff_card / material_candidates /
doc_workbench / final）。
"""

from fastapi import FastAPI

app = FastAPI(title="agent-core", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    """健康检查（网关路由/容器探针）。"""
    return {"status": "ok", "version": "0.1.0"}


@app.post("/chat")
async def chat() -> dict[str, str]:
    """对话入口（占位）。

    TODO: SSE 流式响应——LangGraph 流水线逐阶段推送事件（见 pipeline/graph.py）；
    确认卡提交走 POST /confirmations/{token}（HITL 恢复执行，PRD 4.1/8.7）；
    同会话请求在车道队列串行执行（session_lane:{sessionId}，ARCHITECTURE 4.1）。
    """
    return {"status": "not_implemented"}
