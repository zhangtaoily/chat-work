"""客户查询工具：oa__query_customers / oa__get_customer_defaults。

契约源：packages/protocol/tools/oa__query_customers.json（只读，_meta.rw: read）。
"""

from typing import Any

from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """注册客户相关工具（骨架占位）。"""

    @mcp.tool()
    async def oa__query_customers(customer_name: str) -> list[dict[str, Any]]:
        """按客户名称模糊查询 OA 客户档案（枚举对齐：录入单据前先验证客户存在）。

        TODO: 经 adapters/oa_client 调 OA OpenAPI。
        """
        raise NotImplementedError

    @mcp.tool()
    async def oa__get_customer_defaults(customer_name: str) -> dict[str, Any]:
        """读取客户默认值（收货地址/付款条件/联系人等，供确认卡「客户默认」来源徽标）。

        TODO: 经 adapters/oa_client 调 OA OpenAPI。
        """
        raise NotImplementedError
