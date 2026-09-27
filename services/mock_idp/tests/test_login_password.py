"""工号+密码登录与内网账号管理端点测试（mock dev 用，生产切 Keycloak）。"""

import base64
import hashlib
from dataclasses import replace
from typing import Any

import httpx
import jwt as pyjwt
import pytest

from mock_idp import users
from mock_idp.main import app
from mock_idp.store import store

AUTH = "/realms/chat-work/protocol/openid-connect/auth"
TOKEN = "/realms/chat-work/protocol/openid-connect/token"
RESET_PWD = "/internal/users/{emp_no}/password"
SET_STATUS = "/internal/users/{emp_no}/status"
REDIRECT = "http://127.0.0.1:52301/callback"


def _make_pkce() -> tuple[str, str]:
    """返回 (verifier, challenge)（S256，可走完整 token 兑换）。"""
    verifier = (
        base64.urlsafe_b64encode(hashlib.sha256(b"v" * 16).digest())
        .rstrip(b"=")
        .decode()
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _auth_form(emp_no: str, password: str, challenge: str = "x") -> dict[str, str]:
    """authorize 表单（默认 challenge="x"，仅验证登录环节；走 token 兑换需传真实 challenge）。"""
    return {
        "response_type": "code",
        "client_id": "chat-work-desktop",
        "redirect_uri": REDIRECT,
        "scope": "openid profile",
        "state": "s1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "nonce": "",
        "emp_no": emp_no,
        "password": password,
    }


@pytest.fixture(autouse=True)
def _isolated() -> None:
    """清空授权码存储 + 快照恢复 HR_USERS（启停/重置不泄漏到其他用例）。"""
    store._codes.clear()
    store._sessions.clear()
    snapshot: dict[str, tuple[bool, str, float]] = {
        emp_no: (u.enabled, u.password_hash, u.password_set_at)
        for emp_no, u in users.HR_USERS.items()
    }
    yield
    for emp_no, (enabled, password_hash, password_set_at) in snapshot.items():
        users.HR_USERS[emp_no] = replace(
            users.HR_USERS[emp_no],
            enabled=enabled,
            password_hash=password_hash,
            password_set_at=password_set_at,
        )


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client: httpx.AsyncClient, emp_no: str, password: str) -> httpx.Response:
    return await client.post(AUTH, data=_auth_form(emp_no, password))


# ---- 登录页与密码校验 ----


async def test_login_page_renders_required_password_field(client: httpx.AsyncClient) -> None:
    resp = await client.get(AUTH, params=_auth_form("E1001", ""))
    assert resp.status_code == 200
    assert 'type="password"' in resp.text
    assert 'name="password"' in resp.text
    assert 'name="emp_no"' in resp.text
    assert resp.text.count("required") >= 2  # 工号与密码均必填


async def test_login_success_with_correct_password(client: httpx.AsyncClient) -> None:
    """正确密码 → 发授权码 → 兑换 token（sub=工号）。"""
    verifier, challenge = _make_pkce()
    resp = await client.post(
        AUTH, data=_auth_form("E1001", users.SEED_PASSWORD, challenge=challenge)
    )
    assert resp.status_code == 302, resp.text
    assert "code=ac_" in resp.headers["location"]

    from urllib.parse import parse_qs, urlparse

    code = parse_qs(urlparse(resp.headers["location"]).query)["code"][0]
    token_resp = await client.post(
        TOKEN,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": "chat-work-desktop",
            "code_verifier": verifier,
        },
    )
    assert token_resp.status_code == 200
    claims: dict[str, Any] = pyjwt.decode(
        token_resp.json()["access_token"],
        options={"verify_signature": False},
        audience="chat-work-desktop",
    )
    assert claims["sub"] == "E1001"


async def test_login_rejects_wrong_password(client: httpx.AsyncClient) -> None:
    """错误密码 → 401，统一回显（不区分工号/密码哪个错）。"""
    resp = await _login(client, "E1001", "WrongPass@999")
    assert resp.status_code == 401
    assert "工号或密码错误" in resp.text


async def test_login_rejects_missing_password(client: httpx.AsyncClient) -> None:
    """空密码 → 401 统一回显（浏览器 required 之外的服务端兜底）。"""
    resp = await _login(client, "E1001", "")
    assert resp.status_code == 401
    assert "工号或密码错误" in resp.text


async def test_login_unknown_emp_no_returns_generic_error(client: httpx.AsyncClient) -> None:
    """未知工号与错误密码回显一致 → 防用户枚举。"""
    unknown = await _login(client, "E9999", users.SEED_PASSWORD)
    wrong_pwd = await _login(client, "E1001", "WrongPass@999")
    assert unknown.status_code == wrong_pwd.status_code == 401
    assert "工号或密码错误" in unknown.text
    assert unknown.text == wrong_pwd.text


# ---- 账号启停 ----


async def test_login_rejects_disabled_account(client: httpx.AsyncClient) -> None:
    """停用账号（密码正确）→ 回显「账号已停用」。"""
    resp = await client.post(SET_STATUS.format(emp_no="E1001"), json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json() == {"emp_no": "E1001", "enabled": False}

    login = await _login(client, "E1001", users.SEED_PASSWORD)
    assert login.status_code == 401
    assert "账号已停用，请联系管理员" in login.text

    # 重新启用后可登录
    resume = await client.post(SET_STATUS.format(emp_no="E1001"), json={"enabled": True})
    assert resume.json() == {"emp_no": "E1001", "enabled": True}
    assert (await _login(client, "E1001", users.SEED_PASSWORD)).status_code == 302


# ---- 内网管理：重置口令 ----


async def test_reset_password_new_works_old_fails(client: httpx.AsyncClient) -> None:
    """重置口令后：新密码可登录，旧密码（种子密码）失败。"""
    resp = await client.post(
        RESET_PWD.format(emp_no="E1002"), json={"new_password": "NewPass@2026"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"emp_no": "E1002", "updated": True}

    new_pwd = await _login(client, "E1002", "NewPass@2026")
    assert new_pwd.status_code == 302, new_pwd.text

    old_pwd = await _login(client, "E1002", users.SEED_PASSWORD)
    assert old_pwd.status_code == 401
    assert "工号或密码错误" in old_pwd.text


async def test_reset_password_rejects_too_short(client: httpx.AsyncClient) -> None:
    resp = await client.post(RESET_PWD.format(emp_no="E1002"), json={"new_password": "short"})
    assert resp.status_code == 400
    assert "8" in resp.json()["error"]


async def test_internal_endpoints_unknown_emp_no_404(client: httpx.AsyncClient) -> None:
    pwd = await client.post(RESET_PWD.format(emp_no="E9999"), json={"new_password": "NewPass@2026"})
    status = await client.post(SET_STATUS.format(emp_no="E9999"), json={"enabled": False})
    assert pwd.status_code == status.status_code == 404
