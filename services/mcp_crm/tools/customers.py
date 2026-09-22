"""客户工具：crm__search_customers / crm__get_customer_360（只读，PRD 6.1.1）。

契约源：packages/protocol/tools/crm__search_customers.json
        packages/protocol/tools/crm__get_customer_360.json
（参数/枚举/必填与此保持一致，CI 校验）

规则（PRD 6.1.1）：
- 客户名称必须命中 CRM 客户库：模糊匹配列候选，多候选由 Agent 追问确认
- 客户 360 返回联系人/付款条件/最近成交价/收货地址，供订单草稿默认值链使用
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from adapters.crm_client import get_adapter


def register(mcp: FastMCP) -> None:
    """注册客户相关工具。"""
    adapter = get_adapter()

    @mcp.tool()
    async def crm__search_customers(keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        """按名称关键词模糊搜索 CRM 客户库，返回候选列表（只读）。

        Args:
            keyword: 客户名称关键词（如 "XX"），必须命中客户库
            limit: 最多返回条数（1-50，默认 10）
        """
        kw = keyword.strip()
        if not kw:
            raise ValueError("搜索关键词必填（不提供默认值）")
        limit = max(1, min(int(limit), 50))
        return await adapter.search_customers(kw, limit)

    @mcp.tool()
    async def crm__get_customer_360(customer_id: str) -> dict[str, Any]:
        """查询客户 360 视图：档案 + 联系人 + 付款条件 + 最近成交价 + 近期订单（只读）。

        Args:
            customer_id: 客户编码（来自 crm__search_customers 命中结果）
        """
        if not customer_id.strip():
            raise ValueError("customer_id 必填（来自 crm__search_customers 命中结果）")
        return await adapter.get_customer_360(customer_id.strip())
