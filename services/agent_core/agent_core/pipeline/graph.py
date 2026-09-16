"""LangGraph 对话流水线：七节点状态图（ARCHITECTURE 4.1）。

intent → route → extract → validate → hitl → execute → format
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class ChatState(TypedDict, total=False):
    """流水线全局状态（概念对齐 ARCHITECTURE 4.1）。"""

    auth: dict[str, Any]          # 网关透传的 JWT 解析结果（工号/科室/权限版本）
    session_id: str
    messages: list[dict[str, Any]]
    intent: dict[str, Any] | None         # 阶段1输出：意图识别
    skill: dict[str, Any] | None          # 阶段2输出：技能优先，未命中降级工具
    draft: dict[str, Any] | None          # 阶段3输出：参数草稿 + 缺失必填列表
    validation: dict[str, Any] | None     # 阶段4输出：JSON Schema + 枚举对齐结果
    confirm_token: str | None             # 阶段5输出：HITL 待确认状态（Redis，TTL 10 分钟）
    tool_result: dict[str, Any] | None    # 阶段6输出：MCP tools/call 结果
    final: dict[str, Any] | None          # 阶段7输出：卡片/文本


async def intent_node(state: ChatState) -> dict[str, Any]:
    """阶段1 意图识别：小模型路由。TODO"""
    raise NotImplementedError


async def route_node(state: ChatState) -> dict[str, Any]:
    """阶段2 技能/工具路由：Skill Registry 匹配 → 降级 Tool Registry。TODO"""
    raise NotImplementedError


async def extract_node(state: ChatState) -> dict[str, Any]:
    """阶段3 参数提取：LLM 抽参 + 记忆/知识注入（RAG）。TODO"""
    raise NotImplementedError


async def validate_node(state: ChatState) -> dict[str, Any]:
    """阶段4 Schema 校验：pydantic + protocol JSON Schema（枚举对齐）。TODO"""
    raise NotImplementedError


async def hitl_node(state: ChatState) -> dict[str, Any]:
    """阶段5 HITL：写入类→挂起等确认；询问类→直接出追问。TODO"""
    raise NotImplementedError


async def execute_node(state: ChatState) -> dict[str, Any]:
    """阶段6 执行：MCP tools/call（带幂等键）。TODO"""
    raise NotImplementedError


async def format_node(state: ChatState) -> dict[str, Any]:
    """阶段7 格式化：渲染卡片 JSON（前端按 SSE 事件类型渲染）。TODO"""
    raise NotImplementedError


def build_graph() -> StateGraph:
    """构建七节点流水线图（骨架：节点函数体 TODO）。"""
    graph = StateGraph(ChatState)
    graph.add_node("intent", intent_node)
    graph.add_node("route", route_node)
    graph.add_node("extract", extract_node)
    graph.add_node("validate", validate_node)
    graph.add_node("hitl", hitl_node)
    graph.add_node("execute", execute_node)
    graph.add_node("format", format_node)
    graph.add_edge(START, "intent")
    graph.add_edge("intent", "route")
    graph.add_edge("route", "extract")
    graph.add_edge("extract", "validate")
    graph.add_edge("validate", "hitl")
    graph.add_edge("hitl", "execute")
    graph.add_edge("execute", "format")
    graph.add_edge("format", END)
    return graph
