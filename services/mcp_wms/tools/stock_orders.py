"""出入库单查询工具：wms__query_stock_orders（只读，PRD 6.1）。

契约源：packages/protocol/tools/wms__query_stock_orders.json
（参数/枚举/必填与此保持一致，CI 校验）
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from adapters.wms_client import ORDER_TYPES, get_adapter


async def query_stock_orders(
    order_type: str, keyword: str | None, days: int, limit: int
) -> list[dict[str, Any]]:
    """出入库单查询全链路（枚举/窗口校验 → adapter）。独立函数便于直测。"""
    if order_type not in ORDER_TYPES:
        raise ValueError(f"无效单据类型：{order_type}，可选值 {'/'.join(ORDER_TYPES)}")
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    return await get_adapter().query_stock_orders(order_type, keyword, days, limit)


def register(mcp: FastMCP) -> None:
    """注册出入库单查询工具。"""

    @mcp.tool()
    async def wms__query_stock_orders(
        order_type: str,
        keyword: str | None = None,
        days: int = 30,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """查询 WMS 出入库单（只读）：单号/商品/数量/往来方/关联单据/状态，按日期倒序。

        Args:
            order_type: 单据类型（inbound=入库 / outbound=出库）
            keyword: 可选，模糊匹配单号/商品编码/品名/往来方
            days: 查询最近 N 天的单据（1-365，默认 30）
            limit: 最多返回条数（1-100，默认 20）
        """
        return await query_stock_orders(order_type, keyword, days, limit)
