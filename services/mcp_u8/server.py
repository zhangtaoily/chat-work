"""mcp-u8 服务入口：MCP Server（Streamable HTTP，ARCHITECTURE 4.2）。

统一职责（所有 mcp-* 一致）：
1. inputSchema 校验（参数/枚举与 packages/protocol/tools/u8__*.json 对齐，CI 校验）
2. 只读语义：P3.1 U8 场景全部只读，严禁写入（PRD 8.2：JDBC 只读封装）
3. Service Account 调 U8 + 「代理人」双标记（PRD 8.5.5，HTTP 适配器）
4. 健康探测 + tools 注册

工具目录（PLAN P3.1，PRD 7.1 新增系统对接：U8 财务总账辅助查询）：
- tools/gl.py  u8__query_gl_balance（科目余额表：期初/本期/期末）
               u8__query_voucher_detail（凭证明细：借贷分录 + 平衡校验）
"""

import os

from mcp.server.fastmcp import FastMCP

from adapters.u8_client import get_adapter
from tools.gl import register as register_gl

# 创建 MCP Server（Streamable HTTP 传输，经 agent-core 内网直连）
mcp = FastMCP(
    name="mcp-u8",
    instructions="U8 财务查询（只读）：科目余额表（期初/本期借/贷/期末）、凭证明细（借贷分录 + 平衡校验）。"
    "全部为只读查询，严禁写入（PRD 8.2：U8 只读场景）。",
    host=os.environ.get("MCP_U8_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_U8_PORT", "8007")),
)

# 工具注册（adapter 单例在闭包内复用；U8_MODE 决定 mock/http）
assert get_adapter() is not None
register_gl(mcp)


def main() -> None:
    """启动服务：Streamable HTTP（/mcp 端点，MVP 降级 SSE 由客户端协商）。"""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
