"""mcp-crm 服务入口：MCP Server（Streamable HTTP，ARCHITECTURE 4.2）。

统一职责（所有 mcp-* 一致）：
1. inputSchema 校验（参数/枚举与 packages/protocol/tools/crm__*.json 对齐，CI 校验）
2. 枚举值对齐（单据类型/付款方式枚举白名单校验）
3. Service Account 调 CRM + 「代理人」双标记（PRD 8.5.5，HTTP 适配器）
4. 写入幂等（idempotency.py，保留 24h）
5. 健康探测 + tools 注册

工具目录（PRD 6.1.1，Phase 2 首批业务系统扩展）：
- tools/customers.py  crm__search_customers / crm__get_customer_360（只读）
- tools/orders.py      crm__submit_sales_order（唯一写入）/ crm__query_order_progress（跟单）
"""

import os

from mcp.server.fastmcp import FastMCP

from adapters.crm_client import get_adapter
from tools.customers import register as register_customers
from tools.orders import register as register_orders

# 创建 MCP Server（Streamable HTTP 传输，经 APISIX /mcp/crm 路由）
mcp = FastMCP(
    name="mcp-crm",
    instructions="CRM 销售：客户搜索/360、销售订单录入（幂等+审批流）与跟单查询。"
    "写入操作由 agent-core 在 HITL 确认后携带幂等键调用。",
    host=os.environ.get("MCP_CRM_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_CRM_PORT", "8003")),
)

# 工具注册（adapter 单例在闭包内复用；CRM_MODE 决定 mock/http）
assert get_adapter() is not None
register_customers(mcp)
register_orders(mcp)


def main() -> None:
    """启动服务：Streamable HTTP（/mcp 端点，MVP 降级 SSE 由客户端协商）。"""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
