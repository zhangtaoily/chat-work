"""库存查询工具：erp__query_inventory（只读，PRD 6.1）。

契约源：packages/protocol/tools/erp__query_inventory.json
（参数/枚举/必填与此保持一致，CI 校验）
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from adapters.erp_client import get_adapter


def register(mcp: FastMCP) -> None:
    """注册库存查询工具。"""
    adapter = get_adapter()

    @mcp.tool()
    async def erp__query_inventory(
        sku: str | None = None,
        keyword: str | None = None,
        below_safety: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """查询 ERP 库存（只读）：现存量/可用量/采购在途/安全库存，支持缺料预警过滤。

        Args:
            sku: 商品编码精确查询（如 SKU-C）；与 keyword 可同时使用
            keyword: 模糊匹配品名或仓库名（如 "总仓"）
            below_safety: True 时仅返回低于安全库存的行（缺料预警口径）
            limit: 最多返回条数（1-100，默认 20）
        """
        limit = max(1, min(int(limit), 100))
        return await adapter.query_inventory(sku, keyword, below_safety, limit)
