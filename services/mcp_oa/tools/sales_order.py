"""销售订单录入工具：oa__create_sales_order。

契约源：packages/protocol/tools/oa__create_sales_order.json
（写入类：humanConfirmation=required + idempotencyRequired=true——
Agent 必须先出确认卡，幂等键随 tools/call 传递，PRD 8.7）。
"""

from typing import Any

from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """注册销售订单工具（骨架占位）。"""

    @mcp.tool()
    async def oa__create_sales_order(**params: Any) -> dict[str, Any]:
        """创建 OA 销售订单（幂等写入，安全要点）：

        - 幂等：调用方携带 _idempotencyKey，PG 幂等表（idempotency.py）先查后写，
          重复提交直接返回已生成的单据编号（记录保留 24h，PRD 8.7）；
        - 校验：pydantic 加载 protocol JSON 做 inputSchema 校验 + 枚举对齐
          （客户/商品先查 OA 确认存在，交货日期必填且无默认值由 Agent 追问）。

        TODO: 单事务写入 OA（Service Account + 「代理人」双标记）+ 审计留痕。
        """
        raise NotImplementedError
