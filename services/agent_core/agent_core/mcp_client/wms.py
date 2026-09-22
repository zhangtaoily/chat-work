"""MCP Client：调 mcp-wms（Streamable HTTP，ARCHITECTURE 4.1 阶段6 执行）。

只读查询技能（出入库单/库存预警，PRD 6.1）：无 HITL/幂等需求，直接执行。
"""

from typing import Any

from agent_core.config import settings
from agent_core.mcp_client import call_mcp_tool

__all__ = ["call_wms_tool"]


async def call_wms_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """调用 mcp-wms 工具（只读查询，业务校验错误以 McpToolError 抛出）。"""
    return await call_mcp_tool(f"{settings.mcp_wms_url}/mcp", tool_name, arguments)
