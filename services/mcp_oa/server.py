"""mcp-oa 服务入口：MCP Server（Streamable HTTP，MVP 降级为 SSE，ARCHITECTURE 4.2）。

统一职责（所有 mcp-* 一致）：
1. inputSchema 校验（参数/枚举与 packages/protocol/tools/oa__*.json 对齐，CI 校验）
2. 枚举值对齐（假期类型等枚举白名单校验）
3. Service Account 调 OA + 「代理人」双标记（PRD 8.5.5，HTTP 适配器）
4. 写入幂等（idempotency.py，保留 24h）
5. 健康探测 + tools 注册

MVP 工具目录（PRD 6.1 / ARCHITECTURE v1.8）：
- tools/leave.py      oa__query_leave_balance / oa__submit_leave_request（唯一写入表单）
- tools/approvals.py  oa__query_pending_approvals / oa__approve
- tools/activity.py   oa__query_activity_log（个人周报数据源，PRD 5.2 场景 4）
- tools/expense.py / tools/purchase.py  Phase 2（PRD 6.1.2），不实现
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# 服务级配置（OA_MODE=e9 / E9_* 等），启动目录无关，固定读本目录 .env
load_dotenv(Path(__file__).resolve().parent / ".env")

# 全链路诊断日志（e9_client / tools 经 root logger 输出到 stderr）：
# 请求/响应/4xx-5xx 响应体全记录，E9 5xx 排查不再盲人摸象
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("mcp_oa")

from adapters.oa_client import get_adapter
from tools.activity import register as register_activity
from tools.approvals import register as register_approvals
from tools.leave import register as register_leave

# 创建 MCP Server（Streamable HTTP 传输，经 APISIX /mcp/oa 路由）
mcp = FastMCP(
    name="mcp-oa",
    instructions="OA 流程审批：请假（查余额/提交）与待办审批（查询/处理）。"
    "写入操作由 agent-core 在 HITL 确认后携带幂等键调用。",
    host=os.environ.get("MCP_OA_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_OA_PORT", "8001")),
)

# 工具注册（adapter 单例在闭包内复用；OA_MODE 决定 mock/http）
assert get_adapter() is not None
register_leave(mcp)
register_approvals(mcp)
register_activity(mcp)


def main() -> None:
    """启动服务：Streamable HTTP（/mcp 端点，MVP 降级 SSE 由客户端协商）。"""
    logger.info(
        "mcp-oa 启动：pid=%s OA_MODE=%s 监听 http://%s:%s/mcp",
        os.getpid(),
        os.environ.get("OA_MODE", "mock"),
        os.environ.get("MCP_OA_HOST", "127.0.0.1"),
        os.environ.get("MCP_OA_PORT", "8001"),
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
