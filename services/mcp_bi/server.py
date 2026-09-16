"""mcp-bi 服务入口：MCP Server（Streamable HTTP，MVP 降级为 SSE，ARCHITECTURE 4.2）。

工具契约源：packages/protocol/tools/bi__execute_query.json（只读）。
schemas/ 由 CI 从 packages/protocol 同步，本地不手工维护。
"""

from mcp.server.fastmcp import FastMCP

# 创建 MCP Server（经 APISIX /mcp/bi 路由）
mcp = FastMCP(name="mcp-bi")

# TODO: 注册工具（tools/bi_query.py）
# from tools.bi_query import register as register_bi_query
# register_bi_query(mcp)


def main() -> None:
    """启动服务（骨架占位）。"""
    # TODO: mcp.run(transport="streamable-http")，监听内网端口
    raise NotImplementedError("TODO: Streamable HTTP 启动")


if __name__ == "__main__":
    main()
