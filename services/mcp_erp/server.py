"""mcp-erp 服务入口：MCP Server（Streamable HTTP，ARCHITECTURE 4.2）。

统一职责（所有 mcp-* 一致）：
1. inputSchema 校验（参数/枚举与 packages/protocol/tools/erp__*.json 对齐，CI 校验）
2. 只读语义：Phase 2 ERP 场景全部只读，无写入/幂等需求
3. Service Account 调 ERP + 「代理人」双标记（PRD 8.5.5，HTTP 适配器）
4. 健康探测 + tools 注册

工具目录（PRD 6.1 优先场景）：
- tools/inventory.py  erp__query_inventory（库存查询，含缺料预警过滤）
- tools/purchase.py   erp__query_purchase_orders（采购订单，含在途）
- tools/voucher.py    erp__query_voucher_summary（财务凭证摘要，按月）
"""

import os

from mcp.server.fastmcp import FastMCP

from adapters.erp_client import get_adapter
from tools.inventory import register as register_inventory
from tools.purchase import register as register_purchase
from tools.voucher import register as register_voucher

# 创建 MCP Server（Streamable HTTP 传输，经 APISIX /mcp/erp 路由）
mcp = FastMCP(
    name="mcp-erp",
    instructions="ERP 查询（只读）：库存（含缺料预警）、采购订单（含在途）、财务凭证摘要。"
    "全部为只读查询，无写入操作。",
    host=os.environ.get("MCP_ERP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_ERP_PORT", "8004")),
)

# 工具注册（adapter 单例在闭包内复用；ERP_MODE 决定 mock/http）
assert get_adapter() is not None
register_inventory(mcp)
register_purchase(mcp)
register_voucher(mcp)


def main() -> None:
    """启动服务：Streamable HTTP（/mcp 端点，MVP 降级 SSE 由客户端协商）。"""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
