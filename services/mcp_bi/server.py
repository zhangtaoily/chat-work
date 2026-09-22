"""mcp-bi 服务入口：MCP Server（Streamable HTTP，MVP 降级为 SSE，ARCHITECTURE 4.2）。

统一职责（所有 mcp-* 一致）：
1. inputSchema 校验（参数与 packages/protocol/tools/bi__execute_query.json 对齐，CI 校验）
2. 只读语义：仅 SELECT 查询，无写入/幂等需求
3. 数据权限过滤（区域白名单，PRD 2.3）+ 热点查询缓存 5 分钟（PRD R6）
4. 健康探测 + tools 注册
"""

import os

from mcp.server.fastmcp import FastMCP

from tools.bi_query import register as register_bi_query

# 创建 MCP Server（Streamable HTTP 传输，经 APISIX /mcp/bi 路由）
mcp = FastMCP(
    name="mcp-bi",
    instructions="BI 数据查询（只读）：自然语言 → 查询语义 → KPI/趋势/拆分序列。"
    "数据范围按服务账号权限过滤，热点查询 5 分钟内缓存复用。",
    host=os.environ.get("MCP_BI_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_BI_PORT", "8002")),
)

register_bi_query(mcp)


def main() -> None:
    """启动服务：Streamable HTTP（/mcp 端点，MVP 降级 SSE 由客户端协商）。"""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
