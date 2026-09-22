"""审计日志测试（PLAN P1.2，PRD 10）：

- audit.record / recent：字段完整性、最新在前、user_id/action 过滤、
  params_digest 截断、contextvar 操作者注入（set_actor/reset_actor）
- mcp_client.call_mcp_tool 单点收口埋点：tool_call ok/error 全量落审计
  （fake MCP 会话，不依赖真实服务）
- 图级埋点：HITL 确认卡签发 confirm_issued、确认执行 confirm_approved
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import patch

import pytest

from agent_core import audit
from agent_core.guardrail import confirm_store
from agent_core.mcp_client import McpToolError, call_mcp_tool
from agent_core.pipeline import slots
from agent_core.pipeline.graph import build_graph


@pytest.fixture(autouse=True)
def _clean_audit() -> Any:
    asyncio.run(audit.clear())
    yield
    asyncio.run(audit.clear())


# ---- record / recent / actor ----


async def test_record_fields_and_recent_order() -> None:
    """最新在前；字段完整（time/action/tool/params_digest/result）。"""
    await audit.record("tool_call", tool="oa__approve", params={"approval_id": "AP-1"})
    await audit.record("auth_success", user_id="E1001", session_id="s1")
    items = await audit.recent()
    assert items[0]["action"] == "auth_success"
    assert items[0]["user_id"] == "E1001"
    assert items[0]["session_id"] == "s1"
    assert items[0]["result"] == "ok"
    assert items[0]["time"]  # ISO 时间戳
    assert items[-1]["action"] == "tool_call"
    assert items[-1]["tool"] == "oa__approve"
    assert items[-1]["params_digest"] == '{"approval_id": "AP-1"}'


async def test_recent_filters() -> None:
    await audit.record("tool_call", user_id="E1001", session_id="s1")
    await audit.record("tool_call", user_id="E1002", session_id="s2")
    await audit.record("permission_denied", user_id="E1001", session_id="s1")

    mine = await audit.recent(user_id="E1001")
    assert [e["action"] for e in mine] == ["permission_denied", "tool_call"]
    denials = await audit.recent(action="tool_call", user_id="E1002")
    assert len(denials) == 1
    assert await audit.recent(user_id="E9999") == []


async def test_actor_context_roundtrip() -> None:
    """API 入口 set_actor → 埋点自动携带操作者；reset 后不再携带。"""
    token = audit.set_actor({"user_id": "E1007", "session_id": "s7"})
    try:
        await audit.record("tool_call", tool="t")
    finally:
        audit.reset_actor(token)
    await audit.record("tool_call", tool="t")
    items = await audit.recent()
    assert items[1]["user_id"] == "E1007"
    assert items[1]["session_id"] == "s7"
    assert items[0]["user_id"] is None  # reset 后 contextvar 已恢复


async def test_explicit_params_override_actor() -> None:
    """显式 user_id 优先于 actor（confirm_denied 记录确认人而非发起人）。"""
    token = audit.set_actor({"user_id": "E1001", "session_id": "s1"})
    try:
        await audit.record("confirm_denied", user_id="E1002")
    finally:
        audit.reset_actor(token)
    entry = (await audit.recent(1))[0]
    assert entry["user_id"] == "E1002"
    assert entry["session_id"] == "s1"  # 未显式给出的字段仍取 actor


async def test_digest_truncates() -> None:
    """参数摘要与 detail 截断 300 字符（防全量泄漏）。"""
    await audit.record(
        "tool_call", params={"k": "x" * 500}, detail="d" * 400
    )
    entry = (await audit.recent(1))[0]
    assert len(entry["params_digest"]) == 300
    assert len(entry["detail"]) == 300


# ---- mcp_client 单点收口埋点 ----


class _FakeSession:
    """MCP ClientSession 桩：initialize/call_tool 返回预置结果。"""

    def __init__(self, read: Any, write: Any, result: Any) -> None:
        self._result = result

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return self._result


def _patch_mcp(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    @asynccontextmanager
    async def fake_http(url: str) -> Any:
        yield None, None, None

    monkeypatch.setattr("agent_core.mcp_client.streamablehttp_client", fake_http)
    monkeypatch.setattr(
        "agent_core.mcp_client.ClientSession",
        lambda r, w: _FakeSession(r, w, result),
    )


async def test_mcp_client_audits_ok_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功调用：result=ok、参数摘要、耗时入审计。"""
    _patch_mcp(
        monkeypatch,
        SimpleNamespace(isError=False, structuredContent={"result": [1, 2]}, content=[]),
    )
    result = await call_mcp_tool("http://mcp-oa", "oa__query_pending_approvals", {"user_id": "E1"})
    assert result == [1, 2]
    entry = (await audit.recent(1))[0]
    assert entry["action"] == "tool_call"
    assert entry["tool"] == "oa__query_pending_approvals"
    assert entry["result"] == "ok"
    assert entry["duration_ms"] is not None and entry["duration_ms"] >= 0
    assert entry["params_digest"] == '{"user_id": "E1"}'


async def test_mcp_client_audits_error_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """isError 调用：result=error、工具侧错误原文入 detail，异常照常抛出。"""
    _patch_mcp(
        monkeypatch,
        SimpleNamespace(
            isError=True,
            structuredContent=None,
            content=[SimpleNamespace(text="Error executing tool oa__approve: 审批单不存在")],
        ),
    )
    with pytest.raises(McpToolError, match="审批单不存在"):
        await call_mcp_tool("http://mcp-oa", "oa__approve", {"approval_id": "AP-X"})
    entry = (await audit.recent(1))[0]
    assert entry["action"] == "tool_call"
    assert entry["result"] == "error"
    assert entry["detail"] == "审批单不存在"  # MCP SDK 包装前缀已剥离


# ---- 图级埋点：HITL 确认生命周期 ----

_ITEMS: list[dict[str, Any]] = [
    {
        "approval_id": "AP-AUDIT-0001",
        "doc_type": "leave",
        "title": "张三的年假申请（2 天）",
        "applicant": "张三",
        "submitted_at": "2026-09-12T10:00:00",
    }
]


def _mock_call_oa_tool(name: str, arguments: dict[str, Any]) -> Any:
    if name == "oa__query_pending_approvals":
        return [dict(it) for it in _ITEMS]
    if name == "oa__approve":
        return {
            "approval_id": arguments["approval_id"],
            "doc_no": "LV-2026-0912",
            "status": "approved",
            "message": "已同意",
        }
    raise AssertionError(f"未预期的工具调用：{name}")


async def test_confirm_issued_audit() -> None:
    """HITL 确认卡签发 → confirm_issued 审计（user/tool/参数摘要）。

    confirm_approved 落点在 API 层（POST /confirmations），由
    test_api_auth.py::test_confirm_approved_audited_and_executes 覆盖。
    """
    user, session = "u-audit", "s-audit"
    await slots.set_query_result(user, session, _ITEMS, "approval")
    with patch(
        "agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool
    ):
        graph = build_graph().compile()
        suspended = await graph.ainvoke(
            {"user_id": user, "session_id": session, "message": "同意第 1 条"}
        )
    assert suspended.get("confirm_token") is not None
    issued = await audit.recent(action="confirm_issued")
    assert len(issued) == 1
    assert issued[0]["tool"] == "oa__approve"
    assert issued[0]["user_id"] == user
    assert "AP-AUDIT-0001" in issued[0]["params_digest"]


async def test_confirm_token_store_roundtrip() -> None:
    """确认卡一次性语义依赖 pop_token（回归：签发 → 读取删除 → 二次读取 None）。"""
    token = "tok-audit-1"
    snap = {"state": {"user_id": "u-audit", "session_id": "s-audit"}}
    await confirm_store.set_token(token, snap)
    assert await confirm_store.pop_token(token) == snap
    assert await confirm_store.pop_token(token) is None
