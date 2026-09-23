"""生产报工查询工具：mes__query_work_orders / mes__query_production_reports（只读，PRD 7.1）。

契约源：packages/protocol/tools/mes__query_work_orders.json
        packages/protocol/tools/mes__query_production_reports.json
（参数/枚举/必填与此保持一致，CI 校验）

两工具口径：工单列表给「进度全景」（计划/完工/良品/不良），报工明细给
「单工单操作工流水」（谁/何时/报了多少/工时）；只读，无写入路径。
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from adapters.mes_client import WO_STATUSES, get_adapter


def register(mcp: FastMCP) -> None:
    """注册生产工单与报工明细查询工具。"""
    adapter = get_adapter()

    @mcp.tool()
    async def mes__query_work_orders(
        sku: str | None = None, status: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """查询生产工单进度（只读）：计划量/完工量/良品/不良/状态/工位。

        Args:
            sku: 商品 SKU 过滤（如 SKU-C），缺省查全部
            status: 工单状态过滤（pending 未开工 / running 生产中 / done 已完工 / closed 已结案）
            limit: 返回条数上限（默认 20，最大 50）
        """
        if status is not None and status not in WO_STATUSES:
            raise ValueError(f"status 须为 {'/'.join(WO_STATUSES)} 之一，收到：{status}")
        return await adapter.query_work_orders(sku, status, min(max(limit, 1), 50))

    @mcp.tool()
    async def mes__query_production_reports(
        work_order: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """查询指定工单的报工明细（只读）：操作工/报工时间/良品/不良/工时。

        Args:
            work_order: 工单号（如 MO20260902002），必填
            limit: 返回条数上限（默认 20，最大 50）
        """
        if not work_order.strip():
            raise ValueError("work_order 不能为空")
        return await adapter.query_production_reports(work_order, min(max(limit, 1), 50))
