"""X-Chat-Auth 全链路单测（P1-1）：agent_core 发送端透传与鉴权接线。

- call_mcp_tool：有登录态 → 连接 headers 携带 X-Chat-Auth；无 → 不带（None）
- _authenticate_or_401：鉴权通过即把 Bearer JWT 写入 contextvar（后续 MCP 调用透传）
- 鉴权异常 / 无凭证：contextvar 必须被清空，不残留上个请求的 token
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from agent_core import mcp_client
from agent_core.api import main
from agent_core.api.auth import AuthContext


class _FakeSession:
    """MCP ClientSession 桩：initialize/call_tool 返回预置结果。"""

    def __init__(self, read: Any, write: Any) -> None:
        pass

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return SimpleNamespace(isError=False, structuredContent={"ok": True}, content=[])


def _patch_mcp(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    @asynccontextmanager
    async def fake_http(url: str, headers: dict[str, str] | None = None) -> Any:
        captured["url"] = url
        captured["headers"] = headers
        yield None, None, None

    monkeypatch.setattr("agent_core.mcp_client.streamablehttp_client", fake_http)
    monkeypatch.setattr("agent_core.mcp_client.ClientSession", _FakeSession)


_AUTH = AuthContext(
    user_id="E1001",
    idp="mock",
    idp_sub="E1001",
    dept="信息技术部",
    roles=["staff"],
    perm_ver=1,
    jti="j-1",
    session_id="s-1",
)


async def test_call_mcp_tool_forwards_xchatauth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有登录态：每次 MCP 连接 headers 携带 X-Chat-Auth（服务端持公钥自行验签）。"""
    captured: dict[str, Any] = {}
    _patch_mcp(monkeypatch, captured)
    mcp_client.set_caller_token("tok-abc")
    try:
        result = await mcp_client.call_mcp_tool("http://mcp-oa", "oa__query_leave_balance", {})
    finally:
        mcp_client.set_caller_token(None)
    assert result == {"ok": True}
    assert captured["url"] == "http://mcp-oa"
    assert captured["headers"] == {"X-Chat-Auth": "tok-abc"}


async def test_call_mcp_tool_no_token_sends_no_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未登录（本地冒烟）：headers 为 None，不透传。"""
    captured: dict[str, Any] = {}
    _patch_mcp(monkeypatch, captured)
    mcp_client.set_caller_token(None)
    result = await mcp_client.call_mcp_tool("http://mcp-oa", "oa__query_leave_balance", {})
    assert result == {"ok": True}
    assert captured["headers"] is None


async def test_authenticate_or_401_sets_caller_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """鉴权通过：Bearer JWT 写入 contextvar，后续 call_mcp_tool 透传同一 token。"""
    captured: dict[str, Any] = {}
    _patch_mcp(monkeypatch, captured)

    async def fake_authenticate(request: Any) -> AuthContext:
        return _AUTH

    monkeypatch.setattr(main, "authenticate", fake_authenticate)
    monkeypatch.setattr(main, "sso_required", lambda: False)
    request = SimpleNamespace(headers={"authorization": "Bearer tok-xyz"})
    auth = await main._authenticate_or_401(request)
    assert auth is not None and auth.user_id == "E1001"
    assert mcp_client._caller_token.get() == "tok-xyz"

    result = await mcp_client.call_mcp_tool("http://mcp-oa", "oa__query_leave_balance", {})
    assert result == {"ok": True}
    assert captured["headers"] == {"X-Chat-Auth": "tok-xyz"}
    mcp_client.set_caller_token(None)


async def test_authenticate_or_401_anonymous_clears_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无凭证（冒烟模式）：返回 None 且 contextvar 置空，不残留上个请求身份。"""
    mcp_client.set_caller_token("tok-stale")

    async def fake_authenticate(request: Any) -> AuthContext | None:
        return None

    monkeypatch.setattr(main, "authenticate", fake_authenticate)
    monkeypatch.setattr(main, "sso_required", lambda: False)
    auth = await main._authenticate_or_401(SimpleNamespace(headers={}))
    assert auth is None
    assert mcp_client._caller_token.get() is None


async def test_authenticate_or_401_sso_required_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSO_REQUIRED：无 token → 401（HTTPException），contextvar 不设值。"""
    from fastapi import HTTPException

    async def fake_authenticate(request: Any) -> AuthContext | None:
        return None

    monkeypatch.setattr(main, "authenticate", fake_authenticate)
    monkeypatch.setattr(main, "sso_required", lambda: True)
    with pytest.raises(HTTPException) as exc_info:
        await main._authenticate_or_401(SimpleNamespace(headers={}))
    assert exc_info.value.status_code == 401
    assert mcp_client._caller_token.get() is None
