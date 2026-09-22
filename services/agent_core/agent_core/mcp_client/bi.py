"""MCP Client：调 mcp-bi（Streamable HTTP，ARCHITECTURE 4.1 阶段6 执行）。

只读查询技能（bi_query）：无 HITL/幂等需求，直接执行。
"""

from typing import Any

from agent_core.config import settings
from agent_core.mcp_client import call_mcp_tool

__all__ = ["call_bi_tool"]


async def call_bi_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """调用 mcp-bi 工具（只读查询，业务校验错误以 McpToolError 抛出）。"""
    return await call_mcp_tool(f"{settings.mcp_bi_url}/mcp", tool_name, arguments)
