"""库存预警工具：wms__query_stock_alerts（只读，PRD 6.1）。

契约源：packages/protocol/tools/wms__query_stock_alerts.json
（参数/枚举/必填与此保持一致，CI 校验）

预警类型：low_stock（低于安全库存）/ expiry（效期临期，FEFO 建议）。
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from adapters.wms_client import get_adapter


def register(mcp: FastMCP) -> None:
    """注册库存预警查询工具。"""
    adapter = get_adapter()

    @mcp.tool()
    async def wms__query_stock_alerts(sku: str | None = None) -> list[dict[str, Any]]:
        """查询 WMS 库存预警（只读）：缺料（低于安全库存）与效期临期，含处置建议。

        Args:
            sku: 可选，限定商品编码（如 SKU-C）；缺省返回全部预警
        """
        return await adapter.query_stock_alerts(sku)
