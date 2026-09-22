"""MCP Client：多 Server 连接池 / 能力协商（只走 MCP 协议，不 import 各 mcp_* 内部代码）。

通用调用逻辑（各 mcp_* Server 共用）：每次调用建立连接
（MVP 工具调用量小，可接受；Phase 2 演进为连接池 + 能力协商）。

审计埋点（PLAN P1.2，PRD 10）：本函数是全部 tools/call 的单点收口，
调用前后记录 user/session/tool/参数摘要/结果/耗时；操作者身份取
audit contextvar（API 入口 set_actor 注入），未注入（直连图测试/
本地冒烟）时 user_id 为 None。
"""

import json
import time
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agent_core import audit


class McpToolError(RuntimeError):
    """MCP 工具执行失败（isError=True，含工具侧业务校验错误）。"""


async def call_mcp_tool(url: str, tool_name: str, arguments: dict[str, Any]) -> Any:
    """调用 MCP 工具并解析结果（优先 structuredContent，回退文本 JSON）。"""
    # 异常与解析全部移出 async with 块：anyio TaskGroup 的 __aexit__ 会把
    # 块内抛出的异常包装成 ExceptionGroup，导致上游 except McpToolError 接不住。
    error: McpToolError | None = None
    result: Any = None
    started = time.perf_counter()
    async with (
        streamablehttp_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        call = await session.call_tool(tool_name, arguments)

    if call.isError:
        texts = [c.text for c in call.content if hasattr(c, "text")]
        msg = "; ".join(texts) or f"工具 {tool_name} 执行失败"
        # 剥离 MCP SDK 包装前缀，保留工具侧业务错误原文（用户可见文案）
        prefix = f"Error executing tool {tool_name}: "
        msg = msg.removeprefix(prefix)
        error = McpToolError(msg)
    elif call.structuredContent is not None:
        sc = call.structuredContent
        # FastMCP 结构化输出：非 dict 返回被包装为 {"result": <value>}
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            result = sc["result"]
        else:
            result = sc
    elif call.content and hasattr(call.content[0], "text"):
        result = json.loads(call.content[0].text)

    # 审计：tool_call 全量（读/写均记），错误原文入 detail（截断）
    duration_ms = int((time.perf_counter() - started) * 1000)
    await audit.record(
        "tool_call",
        tool=tool_name,
        params=arguments,
        result="error" if error is not None else "ok",
        duration_ms=duration_ms,
        detail=str(error) if error is not None else "",
    )
    if error is not None:
        raise error
    return result
