"""mcp-oa 服务入口：MCP Server（Streamable HTTP，MVP 降级为 SSE，ARCHITECTURE 4.2）。

统一职责（所有 mcp-* 一致）：
1. inputSchema 校验（pydantic，加载 packages/protocol 中的 JSON）
2. 枚举值对齐（客户名/商品编码先调 OA 查询接口验证存在）
3. Service Account 调 OA + 「代理人」双标记（PRD 8.5.5）
4. 写入幂等（idempotency.py，PG 表，保留 24h）
5. 健康探测 + tools 热注册

schemas/ 目录说明：本目录存放从 packages/protocol/tools/ 同步的 oa__*.json
（唯一工具 Schema 源），由 CI 自动同步与校验，本地不手工维护。
"""

from mcp.server.fastmcp import FastMCP

# 创建 MCP Server（Streamable HTTP 传输，经 APISIX /mcp/oa 路由）
mcp = FastMCP(name="mcp-oa")

# TODO: 注册工具（tools/customers.py、tools/sales_order.py、tools/approvals.py）
# from tools.approvals import register as register_approvals
# from tools.customers import register as register_customers
# from tools.sales_order import register as register_sales_order
# register_customers(mcp)
# register_sales_order(mcp)
# register_approvals(mcp)


def main() -> None:
    """启动服务（骨架占位）。"""
    # TODO: mcp.run(transport="streamable-http")，监听内网端口
    raise NotImplementedError("TODO: Streamable HTTP 启动")


if __name__ == "__main__":
    main()
