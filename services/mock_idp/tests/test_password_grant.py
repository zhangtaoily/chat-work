"""Password Grant 移动端直登测试（PRD 8.5.10，小程序无重定向回调场景）。

覆盖：直登成功（claims 与授权码流程同构，aud=desktop 资源口径）、
错密码/未知工号同文案（防枚举）、非法 client_id 401、缺参 400、
工号归一化（小写）、停用账号拒绝与恢复。
"""

from typing import Any

import httpx
import jwt as pyjwt
import pytest

from mock_idp import users
from mock_idp.main import app
from mock_idp.store import store

TOKEN = "/realms/chat-work/protocol/openid-connect/token"


async def password_grant(client: httpx.AsyncClient, **form: Any) -> dict[str, Any]:
    resp = await client.post(TOKEN, data=form)
    return {"status": resp.status_code, **resp.json()}


def _form(username: str, password: str) -> dict[str, str]:
    return {
        "grant_type": "password",
        "client_id": "chat-work-miniapp",
        "username": username,
        "password": password,
    }


@pytest.fixture
def _isolated_store() -> None:
    store._codes.clear()
    store._sessions.clear()


@pytest.fixture
async def client(_isolated_store) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_password_grant_success(client: httpx.AsyncClient) -> None:
    """直登成功：claims 与授权码流程同构（aud=chat-work-desktop 资源消费者口径）。"""
    tokens = await password_grant(client, **_form("E1001", users.SEED_PASSWORD))
    assert tokens["status"] == 200
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 30 * 60
    assert tokens["refresh_token"]  # 一并签发（小程序忽略，MVP 不做续期）
    claims = pyjwt.decode(
        tokens["access_token"],
        options={"verify_signature": False},
        audience="chat-work-desktop",
    )
    assert claims["sub"] == "E1001"
    assert claims["idp"] == "ad"
    assert claims["perm_ver"] == 17
    assert "roles" in claims and "jti" in claims and "sid" in claims


async def test_password_grant_wrong_password(client: httpx.AsyncClient) -> None:
    """错密码 → 400 invalid_grant。"""
    tokens = await password_grant(client, **_form("E1001", "WrongPass123"))
    assert tokens["status"] == 400
    assert tokens["error"] == "invalid_grant"
    assert tokens["error_description"] == "工号或密码错误"


async def test_password_grant_unknown_emp_no_same_message(client: httpx.AsyncClient) -> None:
    """未知工号与错密码同文案（防用户枚举，PRD 8.5.6 口径）。"""
    unknown = await password_grant(client, **_form("E9999", users.SEED_PASSWORD))
    wrong = await password_grant(client, **_form("E1001", "WrongPass123"))
    assert unknown["status"] == wrong["status"] == 400
    assert unknown["error_description"] == wrong["error_description"] == "工号或密码错误"


async def test_password_grant_invalid_client(client: httpx.AsyncClient) -> None:
    """非法 client_id → 401 invalid_client。"""
    tokens = await password_grant(
        client, grant_type="password", client_id="chat-work-web",
        username="E1001", password=users.SEED_PASSWORD,
    )
    assert tokens["status"] == 401
    assert tokens["error"] == "invalid_client"


async def test_password_grant_missing_fields(client: httpx.AsyncClient) -> None:
    """缺 username/password → 400 invalid_request。"""
    tokens = await password_grant(
        client, grant_type="password", client_id="chat-work-miniapp",
        username="E1001", password="",
    )
    assert tokens["status"] == 400
    assert tokens["error"] == "invalid_request"


async def test_password_grant_normalizes_emp_no(client: httpx.AsyncClient) -> None:
    """工号归一化：小写 e1001 可登录，sub 签发为大写 E1001。"""
    tokens = await password_grant(client, **_form(" e1001 ", users.SEED_PASSWORD))
    assert tokens["status"] == 200
    claims = pyjwt.decode(
        tokens["access_token"], options={"verify_signature": False}, audience="chat-work-desktop"
    )
    assert claims["sub"] == "E1001"


async def test_password_grant_disabled_user(client: httpx.AsyncClient) -> None:
    """停用账号拒绝登录；恢复后可登录（与登录页同口径）。"""
    try:
        users.set_enabled("E1002", False)
        tokens = await password_grant(client, **_form("E1002", users.SEED_PASSWORD))
        assert tokens["status"] == 400
        assert tokens["error_description"] == "账号已停用，请联系管理员"
    finally:
        users.set_enabled("E1002", True)  # 测试隔离：恢复种子状态
    tokens = await password_grant(client, **_form("E1002", users.SEED_PASSWORD))
    assert tokens["status"] == 200
