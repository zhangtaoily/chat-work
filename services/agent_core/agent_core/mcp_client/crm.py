"""MCP Client：调 mcp-crm（Streamable HTTP，ARCHITECTURE 4.1 阶段6 执行）。

唯一写入类业务服务（PRD 6.1.1 销售订单录入）：写路径由 HITL 确认后
携带幂等键调用，mcp-crm 侧兜底重复提交（PRD 8.7）；读路径（客户
匹配/360/跟单）直接执行。
"""

from typing import Any

from agent_core.config import settings
from agent_core.mcp_client import McpToolError, call_mcp_tool

__all__ = ["McpToolError", "call_crm_tool"]


async def call_crm_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """调用 mcp-crm 工具（写入类由 HITL 确认后携带幂等键调用）。"""
    return await call_mcp_tool(f"{settings.mcp_crm_url}/mcp", tool_name, arguments)
