"""mcp-wms 服务入口：MCP Server（Streamable HTTP，ARCHITECTURE 4.2）。

统一职责（所有 mcp-* 一致）：
1. inputSchema 校验（参数/枚举与 packages/protocol/tools/wms__*.json 对齐，CI 校验）
2. 只读语义：Phase 2 WMS 场景全部只读，无写入/幂等需求
3. Service Account 调 WMS + 「代理人」双标记（PRD 8.5.5，HTTP 适配器）
4. 健康探测 + tools 注册

工具目录（PRD 6.1 优先场景）：
- tools/stock_orders.py  wms__query_stock_orders（出入库单查询，含关联单据）
- tools/alerts.py        wms__query_stock_alerts（库存预警：缺料/效期）
"""

import os

from mcp.server.fastmcp import FastMCP

from adapters.wms_client import get_adapter
from tools.alerts import register as register_alerts
from tools.stock_orders import register as register_stock_orders

# 创建 MCP Server（Streamable HTTP 传输，经 APISIX /mcp/wms 路由）
mcp = FastMCP(
    name="mcp-wms",
    instructions="WMS 仓储查询（只读）：出入库单（含关联销售订单/采购单）与库存预警"
    "（缺料/效期）。全部为只读查询，无写入操作。",
    host=os.environ.get("MCP_WMS_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_WMS_PORT", "8005")),
)

# 工具注册（adapter 单例在闭包内复用；WMS_MODE 决定 mock/http）
assert get_adapter() is not None
register_stock_orders(mcp)
register_alerts(mcp)


def main() -> None:
    """启动服务：Streamable HTTP（/mcp 端点，MVP 降级 SSE 由客户端协商）。"""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
