"""MCP Client：调 mcp-u8（Streamable HTTP，ARCHITECTURE 4.1 阶段6 执行）。

只读查询技能（科目余额/凭证明细，P3.1 PRD 7.1）：无 HITL/幂等需求，直接执行。
"""

from typing import Any

from agent_core.config import settings
from agent_core.mcp_client import call_mcp_tool

__all__ = ["call_u8_tool"]


async def call_u8_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """调用 mcp-u8 工具（只读查询，业务校验错误以 McpToolError 抛出）。"""
    return await call_mcp_tool(f"{settings.mcp_u8_url}/mcp", tool_name, arguments)
