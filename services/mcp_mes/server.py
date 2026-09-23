"""mcp-mes 服务入口：MCP Server（Streamable HTTP，ARCHITECTURE 4.2）。

统一职责（所有 mcp-* 一致）：
1. inputSchema 校验（参数/枚举与 packages/protocol/tools/mes__*.json 对齐，CI 校验）
2. 只读语义：P3.1 MES 场景全部只读，无写入/幂等需求（PRD 7.1）
3. Service Account 调 MES + 「代理人」双标记（PRD 8.5.5，HTTP 适配器）
4. 健康探测 + tools 注册

工具目录（PLAN P3.1，PRD 7.1 新增系统对接：MES 生产报工查询）：
- tools/production.py  mes__query_work_orders（工单进度，含良品/不良）
                       mes__query_production_reports（单工单报工明细）
"""

import os

from mcp.server.fastmcp import FastMCP

from adapters.mes_client import get_adapter
from tools.production import register as register_production

# 创建 MCP Server（Streamable HTTP 传输，经 agent-core 内网直连）
mcp = FastMCP(
    name="mcp-mes",
    instructions="MES 查询（只读）：生产工单进度（计划/完工/良品/不良）、单工单报工明细。"
    "全部为只读查询，无写入操作（报工写入仍由 MES 原生终端完成）。",
    host=os.environ.get("MCP_MES_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_MES_PORT", "8006")),
)

# 工具注册（adapter 单例在闭包内复用；MES_MODE 决定 mock/http）
assert get_adapter() is not None
register_production(mcp)


def main() -> None:
    """启动服务：Streamable HTTP（/mcp 端点，MVP 降级 SSE 由客户端协商）。"""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
