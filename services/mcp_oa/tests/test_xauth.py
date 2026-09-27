"""X-Chat-Auth 服务端验签单测（P1-1）：guard() 全分支。

覆盖：
- 无请求上下文 / FastMCP 请求外 / 请求无 token → 放行（自动化与本地冒烟兼容）
- token 验签失败（篡改 / 过期）→ 拒绝
- token 有效 + user_id 一致 → 放行并返回工号
- token 有效 + user_id 不一致 → 越权拒绝（防越权访问他人数据）
- X-Chat-Auth 与 Authorization: Bearer 双头取值与优先级
"""

import asyncio
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import xauth

# 测试密钥对：公钥桩替代 JWKS 拉取（seam 与 agent_core auth 测试一致）
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_token(user: str = "E1001", **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "iss": xauth.SSO_ISSUER,
        "aud": xauth.SSO_AUDIENCE,
        "sub": user,
        "preferred_username": user,
        "exp": int(time.time()) + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, _KEY, algorithm="RS256")


@pytest.fixture(autouse=True)
def _seam(monkeypatch: pytest.MonkeyPatch):
    """验签公钥桩 + 用例后恢复无实例状态。"""

    async def fake_key(token: str) -> Any:
        return _KEY.public_key()

    monkeypatch.setattr(xauth, "get_verification_key", fake_key)
    yield
    xauth.setup(None)


class _Req:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class _RC:
    def __init__(self, request: Any) -> None:
        self.request = request


class _Ctx:
    def __init__(self, request_context: Any) -> None:
        self.request_context = request_context


class _MCP:
    """FastMCP 桩：get_context 返回预置请求上下文。"""

    def __init__(self, request_context: Any) -> None:
        self._rc = request_context

    def get_context(self) -> _Ctx:
        return _Ctx(self._rc)


def _with_request(headers: dict[str, str]) -> None:
    xauth.setup(_MCP(_RC(_Req(headers))))


# ---------- 放行分支（自动化 / 本地冒烟兼容） ----------


def test_guard_no_context_passes() -> None:
    """无 MCP 实例（直调 / 单测）→ 放行。"""
    assert _run(xauth.guard("E1001")) is None


def test_guard_outside_request_passes() -> None:
    """FastMCP 请求外 get_context（request_context=None）→ 放行。"""
    xauth.setup(_MCP(None))
    assert _run(xauth.guard("E1001")) is None


def test_guard_without_token_passes() -> None:
    """有请求但无 X-Chat-Auth / Bearer → 放行（自动化定时任务）。"""
    _with_request({})
    assert _run(xauth.guard("E1001")) is None


# ---------- 拒绝分支 ----------


def test_guard_invalid_token_rejected() -> None:
    """token 篡改 / 非法 → 拒绝。"""
    _with_request({"x-chat-auth": "not-a-jwt"})
    with pytest.raises(ValueError, match="身份校验失败"):
        _run(xauth.guard("E1001"))


def test_guard_expired_token_rejected() -> None:
    """token 过期（远超 ±60s leeway）→ 拒绝。"""
    _with_request({"x-chat-auth": _make_token(exp=int(time.time()) - 3600)})
    with pytest.raises(ValueError, match="身份校验失败"):
        _run(xauth.guard("E1001"))


def test_guard_wrong_signature_rejected() -> None:
    """他签 token（如伪造服务签发）→ 验签拒绝。"""
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {
            "iss": xauth.SSO_ISSUER,
            "aud": xauth.SSO_AUDIENCE,
            "sub": "E9999",
            "preferred_username": "E1001",
            "exp": int(time.time()) + 600,
        },
        other,
        algorithm="RS256",
    )
    _with_request({"x-chat-auth": forged})
    with pytest.raises(ValueError, match="身份校验失败"):
        _run(xauth.guard("E1001"))


def test_guard_other_user_rejected() -> None:
    """token 有效但目标 user_id 不是本人 → 越权拒绝（防越权核心分支）。"""
    _with_request({"x-chat-auth": _make_token("E1001")})
    with pytest.raises(ValueError, match="越权访问被拒绝"):
        _run(xauth.guard("E2002"))


# ---------- 一致性放行与双头取值 ----------


def test_guard_same_user_passes() -> None:
    """token 身份与 user_id 一致 → 放行，返回工号。"""
    _with_request({"x-chat-auth": _make_token("E1001")})
    assert _run(xauth.guard("E1001")) == "E1001"


def test_guard_no_user_arg_checks_validity_only() -> None:
    """guard() 不带 user_id（只读工具）：仅验签，无越权面。"""
    _with_request({"x-chat-auth": _make_token("E1001")})
    assert _run(xauth.guard()) == "E1001"


def test_guard_bearer_header_supported() -> None:
    """Authorization: Bearer 同样接受（与 agent_core 双通道口径一致）。"""
    _with_request({"authorization": f"Bearer {_make_token('E1001')}"})
    assert _run(xauth.guard("E1001")) == "E1001"


def test_guard_bearer_takes_priority() -> None:
    """双头并存：Authorization 优先（对齐 agent_core bearer_token）。"""
    _with_request(
        {
            "authorization": f"Bearer {_make_token('E1001')}",
            "x-chat-auth": _make_token("E2002"),
        }
    )
    assert _run(xauth.guard("E1001")) == "E1001"
