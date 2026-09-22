"""采购订单工具：erp__query_purchase_orders（只读，PRD 6.1）。

契约源：packages/protocol/tools/erp__query_purchase_orders.json
（参数/枚举/必填与此保持一致，CI 校验）
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from adapters.erp_client import PO_STATUSES, get_adapter


def register(mcp: FastMCP) -> None:
    """注册采购订单查询工具。"""
    adapter = get_adapter()

    @mcp.tool()
    async def erp__query_purchase_orders(
        sku: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """查询 ERP 采购订单（只读）：供应商/数量/金额/预计到货/状态，支持在途过滤。

        Args:
            sku: 商品编码精确查询（如 SKU-C）
            status: 可选，采购状态（draft=草稿 / approved=已审批 /
                in_transit=在途 / received=已入库）；缺省返回全部
            limit: 最多返回条数（1-100，默认 20）
        """
        if status is not None and status not in PO_STATUSES:
            raise ValueError(f"无效采购状态：{status}，可选值 {'/'.join(PO_STATUSES)}")
        limit = max(1, min(int(limit), 100))
        return await adapter.query_purchase_orders(sku, status, limit)
