"""Mock OIDC Provider 协议测试（PRD 8.5.2/8.5.4/8.5.6 安全要求全覆盖）。"""

import base64
import hashlib
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt as pyjwt
import pytest

from mock_idp import keys
from mock_idp.main import app
from mock_idp.store import store

AUTH = "/realms/chat-work/protocol/openid-connect/auth"
TOKEN = "/realms/chat-work/protocol/openid-connect/token"
CERTS = "/realms/chat-work/protocol/openid-connect/certs"
DISCOVERY = "/realms/chat-work/.well-known/openid-configuration"
REDIRECT = "http://127.0.0.1:52301/callback"


def make_pkce() -> tuple[str, str]:
    """返回 (verifier, challenge)（S256）。"""
    verifier = base64.urlsafe_b64encode(hashlib.sha256(b"v" * 16).digest())[:64]
    verifier = verifier.decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


async def authorize_and_get_code(
    client: httpx.AsyncClient,
    emp_no: str = "E1001",
    redirect_uri: str = REDIRECT,
    challenge: str | None = None,
    state: str = "s1",
) -> dict[str, str]:
    """走 authorize 提交登录，解析 302 Location 中的 code/state。"""
    verifier, _challenge = make_pkce()
    if challenge is not None:
        _, challenge = challenge, challenge  # noqa: F841 - 语义占位
    if challenge is None:
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
    resp = await client.post(
        AUTH,
        data={
            "response_type": "code",
            "client_id": "chat-work-desktop",
            "redirect_uri": redirect_uri,
            "scope": "openid profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": "",
            "emp_no": emp_no,
        },
    )
    assert resp.status_code == 302, resp.text
    loc = resp.headers["location"]
    assert loc.startswith(redirect_uri)
    query = parse_qs(urlparse(loc).query)
    return {
        "code": query["code"][0],
        "state": query["state"][0],
        "verifier": verifier,
        "location": loc,
    }


async def exchange_token(client: httpx.AsyncClient, **form: Any) -> dict[str, Any]:
    resp = await client.post(TOKEN, data=form)
    return {"status": resp.status_code, **resp.json()}


@pytest.fixture
def _isolated_store() -> None:
    store._codes.clear()
    store._sessions.clear()


@pytest.fixture
async def client(_isolated_store) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---- 发现与公钥 ----


async def test_discovery_and_certs(client: httpx.AsyncClient) -> None:
    disc = (await client.get(DISCOVERY)).json()
    assert disc["issuer"] == "http://localhost:8012/realms/chat-work"
    assert disc["code_challenge_methods_supported"] == ["S256"]

    jwks = (await client.get(CERTS)).json()
    assert jwks["keys"][0]["kty"] == "RSA"
    assert jwks["keys"][0]["alg"] == "RS256"
    assert jwks["keys"][0]["kid"].startswith("mock-idp-")


# ---- authorize：参数与安全校验 ----


async def test_authorize_rejects_non_loopback_redirect(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        AUTH,
        params={
            "response_type": "code",
            "client_id": "chat-work-desktop",
            "redirect_uri": "https://evil.example.com/cb",
            "scope": "openid",
            "state": "s",
            "code_challenge": "x",
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 400
    assert "127.0.0.1" in resp.text


async def test_authorize_rejects_plain_pkce(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        AUTH,
        params={
            "response_type": "code",
            "client_id": "chat-work-desktop",
            "redirect_uri": REDIRECT,
            "scope": "openid",
            "state": "s",
            "code_challenge": "x",
            "code_challenge_method": "plain",
        },
    )
    assert resp.status_code == 400
    assert "S256" in resp.text


async def test_authorize_missing_params(client: httpx.AsyncClient) -> None:
    resp = await client.get(AUTH, params={"response_type": "code", "client_id": "x"})
    assert resp.status_code == 400


async def test_login_unknown_emp_no_rejected(client: httpx.AsyncClient) -> None:
    """PRD 8.5.6：工号未匹配 HR 主数据 → 拒绝登录（不自动创建）。"""
    resp = await client.post(
        AUTH,
        data={
            "response_type": "code",
            "client_id": "chat-work-desktop",
            "redirect_uri": REDIRECT,
            "scope": "openid profile",
            "state": "s",
            "code_challenge": "x",
            "code_challenge_method": "S256",
            "nonce": "",
            "emp_no": "E9999",
        },
    )
    assert resp.status_code == 401
    assert "HR 主数据" in resp.text


async def test_login_success_redirects_with_code_state(client: httpx.AsyncClient) -> None:
    result = await authorize_and_get_code(client)
    assert result["state"] == "s1"
    assert result["code"].startswith("ac_")


# ---- token：PKCE 与一次性 code ----


async def test_token_exchange_success(client: httpx.AsyncClient) -> None:
    result = await authorize_and_get_code(client)
    tokens = await exchange_token(
        client,
        grant_type="authorization_code",
        code=result["code"],
        redirect_uri=REDIRECT,
        client_id="chat-work-desktop",
        code_verifier=result["verifier"],
    )
    assert tokens["status"] == 200
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 30 * 60

    claims = pyjwt.decode(
        tokens["access_token"],
        options={"verify_signature": False},
        audience="chat-work-desktop",
    )
    assert claims["sub"] == "E1001"
    assert claims["idp"] == "ad"
    assert claims["perm_ver"] == 17
    assert "roles" in claims and "jti" in claims


async def test_token_rejects_wrong_verifier(client: httpx.AsyncClient) -> None:
    result = await authorize_and_get_code(client)
    tokens = await exchange_token(
        client,
        grant_type="authorization_code",
        code=result["code"],
        redirect_uri=REDIRECT,
        client_id="chat-work-desktop",
        code_verifier="wrong-verifier-wrong-verifier-wrong-verifier-wrong",
    )
    assert tokens["status"] == 400
    assert tokens["error"] == "invalid_grant"


async def test_code_is_single_use(client: httpx.AsyncClient) -> None:
    """code 用后即焚（PRD 8.5.2 安全要求 #2）：重放返回 invalid_grant。"""
    result = await authorize_and_get_code(client)
    form = {
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT,
        "client_id": "chat-work-desktop",
        "code_verifier": result["verifier"],
    }
    first = await exchange_token(client, **form)
    assert first["status"] == 200
    replay = await exchange_token(client, **form)
    assert replay["status"] == 400
    assert "一次性" in replay["error_description"]


async def test_token_rejects_mismatched_redirect(client: httpx.AsyncClient) -> None:
    result = await authorize_and_get_code(client)
    tokens = await exchange_token(
        client,
        grant_type="authorization_code",
        code=result["code"],
        redirect_uri="http://127.0.0.1:9999/callback",
        client_id="chat-work-desktop",
        code_verifier=result["verifier"],
    )
    assert tokens["status"] == 400


# ---- refresh 轮换与复用检测 ----


async def test_refresh_rotation_and_reuse_detection(client: httpx.AsyncClient) -> None:
    """PRD 8.5.4：refresh 使用后轮换；旧 token 复用 → 整链吊销。"""
    result = await authorize_and_get_code(client)
    tokens = await exchange_token(
        client,
        grant_type="authorization_code",
        code=result["code"],
        redirect_uri=REDIRECT,
        client_id="chat-work-desktop",
        code_verifier=result["verifier"],
    )
    rt1 = tokens["refresh_token"]

    r2 = await exchange_token(
        client, grant_type="refresh_token", client_id="chat-work-desktop", refresh_token=rt1
    )
    assert r2["status"] == 200
    rt2 = r2["refresh_token"]
    assert rt2 != rt1

    # 旧 token 复用 → 被盗检测，整链吊销
    r3 = await exchange_token(
        client, grant_type="refresh_token", client_id="chat-work-desktop", refresh_token=rt1
    )
    assert r3["status"] == 400
    assert "复用" in r3["error_description"]

    # 新 token 也已被整链吊销
    r4 = await exchange_token(
        client, grant_type="refresh_token", client_id="chat-work-desktop", refresh_token=rt2
    )
    assert r4["status"] == 400


async def test_revoke_session(client: httpx.AsyncClient) -> None:
    result = await authorize_and_get_code(client)
    tokens = await exchange_token(
        client,
        grant_type="authorization_code",
        code=result["code"],
        redirect_uri=REDIRECT,
        client_id="chat-work-desktop",
        code_verifier=result["verifier"],
    )
    rt = tokens["refresh_token"]
    resp = await client.post("/realms/chat-work/protocol/openid-connect/revoke", data={"token": rt})
    assert resp.json() == {"revoked": True}
    again = await exchange_token(
        client, grant_type="refresh_token", client_id="chat-work-desktop", refresh_token=rt
    )
    assert again["status"] == 400


# ---- JWT 验签一致性（公钥能验 token） ----


async def test_access_token_verifies_with_jwks(client: httpx.AsyncClient) -> None:
    result = await authorize_and_get_code(client)
    tokens = await exchange_token(
        client,
        grant_type="authorization_code",
        code=result["code"],
        redirect_uri=REDIRECT,
        client_id="chat-work-desktop",
        code_verifier=result["verifier"],
    )
    jwks = (await client.get(CERTS)).json()
    key = pyjwt.algorithms.RSAAlgorithm.from_jwk(jwks["keys"][0])
    claims: dict[str, Any] = pyjwt.decode(
        tokens["access_token"],
        key=key,  # type: ignore[arg-type]
        algorithms=["RS256"],
        issuer="http://localhost:8012/realms/chat-work",
        audience="chat-work-desktop",
        leeway=60,
    )
    assert claims["sub"] == "E1001"
    assert claims["exp"] > time.time()
    assert claims["iss"] == "http://localhost:8012/realms/chat-work"
