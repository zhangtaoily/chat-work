"""一次性诊断探针：走完整 MCP 链路（http://127.0.0.1:8001/mcp）调
oa__submit_leave_request，复现用户真实提交路径；配合 mcp_oa 服务端
全链路日志（stderr → 后台任务 output.log）定位 E9 500。

用法：uv run python _probe_mcp_submit.py
- 成功：真实在 E9 dev 建单（测试单，需在 E9 流程中心删除）
- 失败：打印工具错误全文（含 E9 HTTP 状态与响应体）
"""

import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(Path(__file__).resolve().parent / ".env")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# 模拟真实链路：桌面端 Keycloak dev 登录身份 E1001（映射表应转 7849）
_PARAMS = {
    "user_id": "E1001",
    "leave_type": "comp",
    "start_time": "2026-10-05T08:30:00",
    "end_time": "2026-10-05T16:30:00",
    "duration_days": 1.0,
    "reason": "家里有事",
    "idempotency_key": "probe-mcp-e1001-20260924-1",
    "tx_reason": 0,
}


async def main() -> None:
    async with streamablehttp_client("http://127.0.0.1:8001/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("oa__submit_leave_request", _PARAMS)
            print("isError:", result.isError)
            for item in result.content:
                print("content:", getattr(item, "text", item))


asyncio.run(main())
