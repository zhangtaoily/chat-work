"""MCP Client：调 mcp-oa（Streamable HTTP，ARCHITECTURE 4.1 阶段6 执行）。

Agent Core 只通过 MCP 协议调工具（设计原则 #4），
不 import 各 mcp_* 内部代码（Monorepo 边界规则）。
"""

from typing import Any

from agent_core.config import settings
from agent_core.mcp_client import McpToolError, call_mcp_tool

__all__ = ["McpToolError", "call_oa_tool"]


async def call_oa_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """调用 mcp-oa 工具（写入类由 HITL 确认后携带幂等键调用）。"""
    return await call_mcp_tool(f"{settings.mcp_oa_url}/mcp", tool_name, arguments)
