"""操作日志工具：oa__query_activity_log（只读，个人工作周报数据源）。

契约源：packages/protocol/tools/oa__query_activity_log.json
（PRD 5.2 场景 4 前置依赖①：OA 提供按人查询操作日志 API）
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from adapters.oa_client import get_adapter


def register(mcp: FastMCP) -> None:
    """注册操作日志相关工具。"""
    adapter = get_adapter()

    @mcp.tool()
    async def oa__query_activity_log(user_id: str, days: int = 7) -> list[dict[str, Any]]:
        """查询用户最近 N 天的 OA 操作日志（只读，个人周报数据源）。

        Args:
            user_id: 员工工号
            days: 回溯天数（1-31，默认 7）
        """
        if not 1 <= days <= 31:
            raise ValueError("days 取值范围 1-31")
        return await adapter.list_activity_log(user_id, days)
