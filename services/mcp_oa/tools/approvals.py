"""审批工具：oa__query_pending_approvals / oa__approve。"""

from typing import Any

from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """注册审批相关工具（骨架占位）。"""

    @mcp.tool()
    async def oa__query_pending_approvals(user_id: str) -> list[dict[str, Any]]:
        """查询用户待办审批列表（只读，对应桌面端审批待办视图）。

        TODO: 经 adapters/oa_client 调 OA OpenAPI。
        """
        raise NotImplementedError

    @mcp.tool()
    async def oa__approve(
        approval_id: str, action: str, comment: str = ""
    ) -> dict[str, Any]:
        """审批操作（同意/驳回；写入类：HITL 确认后执行）。

        TODO: 幂等 + 审计留痕（谁在何时以何身份处理）。
        """
        raise NotImplementedError
