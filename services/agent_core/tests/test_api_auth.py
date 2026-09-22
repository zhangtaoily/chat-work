"""SSO JWT 验签与 API 鉴权测试（PRD 8.5.4/8.5.5/8.5.7）。

测试自建 RSA 密钥 + 注入 JWKS（monkeypatch auth._fetch_jwks），
覆盖：验签正例、过期、错误签名/算法、jti 黑名单（进程内 + Redis 共享）、
preferred_username 工号优先（Keycloak sub=UUID）、SSO_REQUIRED 开关、
身份以 sub 覆盖请求体、确认人一致性。
"""

import asyncio
import time
import uuid
from typing import Any

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from agent_core import audit
from agent_core.api import auth as auth_mod
from agent_core.api.main import app
from agent_core.guardrail import confirm_store

ISSUER = auth_mod.SSO_ISSUER
AUDIENCE = auth_mod.SSO_AUDIENCE

# rsa_key fixture 移至 tests/conftest.py（多测试模块共享）


def make_token(rsa_key: Any, **overrides: Any) -> str:
    """按 PRD 8.5.4 claims 结构签发测试 token。"""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": "E1001",
        "idp": "ad",
        "idp_sub": "zhangsan@corp.com",
        "dept": "事业部A/销售科",
        "roles": ["employee"],
        "perm_ver": 17,
        "aud": AUDIENCE,
        "sid": "sess-1",
        "iat": now,
        "exp": now + 1800,
        "jti": "jti-1",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return pyjwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-kid"})


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_sso_required(monkeypatch: pytest.MonkeyPatch) -> None:
    auth_mod.set_sso_required(False)


# ---- verify_token 单元 ----


@pytest.mark.asyncio
async def test_verify_token_valid(rsa_key: Any) -> None:
    ctx = await auth_mod.verify_token(make_token(rsa_key))
    assert ctx.user_id == "E1001"
    assert ctx.dept == "事业部A/销售科"
    assert ctx.perm_ver == 17
    assert ctx.roles == ["employee"]


@pytest.mark.asyncio
async def test_verify_token_expired(rsa_key: Any) -> None:
    with pytest.raises(pyjwt.ExpiredSignatureError):
        await auth_mod.verify_token(make_token(rsa_key, exp=int(time.time()) - 3600))


@pytest.mark.asyncio
async def test_verify_token_wrong_issuer(rsa_key: Any) -> None:
    with pytest.raises(pyjwt.InvalidIssuerError):
        await auth_mod.verify_token(make_token(rsa_key, iss="https://evil.internal"))


@pytest.mark.asyncio
async def test_verify_token_wrong_audience(rsa_key: Any) -> None:
    with pytest.raises(pyjwt.InvalidAudienceError):
        await auth_mod.verify_token(make_token(rsa_key, aud="other-app"))


@pytest.mark.asyncio
async def test_verify_token_wrong_signature(rsa_key: Any) -> None:
    """错误密钥签发的 token 拒绝（伪造场景）。"""
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(pyjwt.InvalidSignatureError):
        await auth_mod.verify_token(make_token(other))


@pytest.mark.asyncio
async def test_verify_token_hs256_forged(rsa_key: Any) -> None:
    """alg 混淆攻击（HS256 伪造）拒绝。"""
    now = int(time.time())
    token = pyjwt.encode(
        {"iss": ISSUER, "sub": "E1001", "aud": AUDIENCE, "exp": now + 600, "jti": "x"},
        "secret",
        algorithm="HS256",
        headers={"kid": "test-kid"},
    )
    with pytest.raises(pyjwt.InvalidAlgorithmError):
        await auth_mod.verify_token(token)


@pytest.mark.asyncio
async def test_verify_token_clock_leeway(rsa_key: Any) -> None:
    """时钟偏移 ±60 秒容差（PRD 8.5.7）：30 秒前过期的 token 仍接受。"""
    token = make_token(rsa_key, exp=int(time.time()) - 30)
    ctx = await auth_mod.verify_token(token)
    assert ctx.user_id == "E1001"


@pytest.mark.asyncio
async def test_jti_blacklist_rejects(rsa_key: Any) -> None:
    """jti 黑名单（吊销/强制下线，PRD 8.5.7）。"""
    token = make_token(rsa_key)
    claims = pyjwt.decode(token, options={"verify_signature": False})
    auth_mod.revoke_jti(str(claims["jti"]), float(claims["exp"]))
    with pytest.raises(pyjwt.InvalidTokenError, match="吊销"):
        await auth_mod.verify_token(token)


@pytest.mark.asyncio
async def test_verify_token_preferred_username_wins(rsa_key: Any) -> None:
    """Keycloak sub 为 UUID：preferred_username（=工号）优先作为 user_id（PRD 8.5.4）。"""
    token = make_token(
        rsa_key,
        sub="3fa85f64-5717-4562-b3fc-2c963f66afa6",
        preferred_username="E1001",
    )
    ctx = await auth_mod.verify_token(token)
    assert ctx.user_id == "E1001"


class _FakeRedis:
    """模拟 redis.asyncio 客户端（jti 黑名单跨 worker 共享路径）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0


@pytest.mark.asyncio
async def test_jti_blacklist_redis_shared(
    rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis 黑名单共享（多 worker，PRD 8.5.7）：吊销双写 Redis，
    另一 worker 进程内黑名单为空，仅凭 Redis 判定拒绝。"""
    fake = _FakeRedis()
    monkeypatch.setattr(auth_mod, "_get_redis", lambda: fake)

    token = make_token(rsa_key, jti="jti-redis")
    claims = pyjwt.decode(token, options={"verify_signature": False})
    auth_mod.revoke_jti(str(claims["jti"]), float(claims["exp"]))
    await asyncio.sleep(0)  # 让 revoke 的 setex 后台任务执行

    assert f"jti:blacklist:{claims['jti']}" in fake.store  # 双写落 Redis

    monkeypatch.setattr(auth_mod, "_jti_blacklist", {})  # 模拟另一 worker：进程内为空
    with pytest.raises(pyjwt.InvalidTokenError, match="吊销"):
        await auth_mod.verify_token(token)


# ---- API 集成 ----


def test_chat_without_token_falls_back_to_body_identity(
    client: TestClient, rsa_key: Any
) -> None:
    """SSO_REQUIRED=false（默认）：无 token 走请求体直传身份（本地冒烟兼容）。"""
    resp = client.post(
        "/chat", json={"session_id": "s1", "user_id": "E1001", "message": "你好"}
    )
    assert resp.status_code == 200


def test_chat_with_valid_token_overrides_identity(
    client: TestClient, rsa_key: Any
) -> None:
    """有效 token + 请求体伪造他人 user_id → 正常受理（实际身份以 JWT sub 为准）。"""
    token = make_token(rsa_key, sub="E1002")
    resp = client.post(
        "/chat",
        json={"session_id": "s1", "user_id": "E9999", "message": "你好"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_chat_with_invalid_token_401_when_required(
    client: TestClient, rsa_key: Any
) -> None:
    """SSO_REQUIRED=true：验签失败 → 401。"""
    auth_mod.set_sso_required(True)
    resp = client.post(
        "/chat",
        json={"session_id": "s1", "user_id": "E1001", "message": "你好"},
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert resp.status_code == 401


def test_chat_without_token_401_when_required(client: TestClient) -> None:
    auth_mod.set_sso_required(True)
    resp = client.post(
        "/chat", json={"session_id": "s1", "user_id": "E1001", "message": "你好"}
    )
    assert resp.status_code == 401
    assert "缺少认证凭证" in resp.json()["detail"]


def test_chat_valid_token_passes_when_required(
    client: TestClient, rsa_key: Any
) -> None:
    auth_mod.set_sso_required(True)
    token = make_token(rsa_key)
    resp = client.post(
        "/chat",
        json={"session_id": "s1", "user_id": "whatever", "message": "你好"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_chat_x_chat_auth_header(client: TestClient, rsa_key: Any) -> None:
    """MCP 透传头 X-Chat-Auth 等效 Bearer（PRD 8.5.5）。"""
    auth_mod.set_sso_required(True)
    token = make_token(rsa_key)
    resp = client.post(
        "/chat",
        json={"session_id": "s1", "message": "你好"},
        headers={"X-Chat-Auth": token},
    )
    assert resp.status_code == 200


# ---- 审计流水 /audit（PLAN P1.2，PRD 10）----


@pytest.fixture(autouse=True)
def _clean_audit() -> Any:
    """审计内存隔离（同步上下文清空，memory 模式与事件循环无关）。"""
    asyncio.run(audit.clear())
    yield
    asyncio.run(audit.clear())


def _seed(**kwargs: Any) -> None:
    asyncio.run(audit.record(**kwargs))


def test_audit_requires_authentication(client: TestClient, rsa_key: Any) -> None:
    """恒需认证：SSO_REQUIRED=false 的本地冒烟也不放行匿名查询。"""
    _seed(action="tool_call", tool="t", user_id="E1001", session_id="s1")
    resp = client.get("/audit")
    assert resp.status_code == 401


def test_audit_employee_reads_own_records_only(
    client: TestClient, rsa_key: Any
) -> None:
    """数据级权限同一口径：普通用户仅本人记录；指定他人 → 403。"""
    _seed(action="tool_call", tool="t1", user_id="E1001", session_id="s1")
    _seed(action="auth_success", user_id="E1001", session_id="s1")
    _seed(action="tool_call", tool="t2", user_id="E1002", session_id="s2")
    headers = {"Authorization": f"Bearer {make_token(rsa_key)}"}
    resp = client.get("/audit", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert all(item["user_id"] == "E1001" for item in body["items"])

    resp = client.get("/audit", params={"user_id": "E1002"}, headers=headers)
    assert resp.status_code == 403


def test_audit_manager_reads_any_user(client: TestClient, rsa_key: Any) -> None:
    """dept_manager 可查任意用户（管理端审计者角色，PRD 10）。"""
    _seed(action="tool_call", tool="t1", user_id="E1001", session_id="s1")
    _seed(action="tool_call", tool="t2", user_id="E1002", session_id="s2")
    headers = {
        "Authorization": f"Bearer {make_token(rsa_key, sub='E1003', roles=['dept_manager'], jti='jti-mgr')}"
    }
    resp = client.get("/audit", params={"user_id": "E1002"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["user_id"] == "E1002"

    # 不带 user_id → 全量
    resp = client.get("/audit", headers=headers)
    assert resp.json()["count"] == 2


def test_audit_limit_clamped(client: TestClient, rsa_key: Any) -> None:
    """limit 上限 500 钳制（防大查询拖垮服务）。"""
    for i in range(3):
        _seed(action="tool_call", tool=f"t{i}", user_id="E1001", session_id="s1")
    headers = {"Authorization": f"Bearer {make_token(rsa_key)}"}
    resp = client.get("/audit", params={"limit": 2}, headers=headers)
    assert resp.json()["count"] == 2


# ---- 确认卡生命周期审计（PLAN P1.2：confirm_rejected/denied/approved）----


def _issue_confirm_token(user_id: str, session_id: str) -> str:
    """直接写入确认存储（绕过 /chat 全链路，聚焦确认端点审计行为）。"""
    token = uuid.uuid4().hex
    snap = {
        "state": {
            "user_id": user_id,
            "session_id": session_id,
            "tool_call": {
                "name": "oa__approve",
                "arguments": {"approval_id": "AP-2026-0001", "action": "approve"},
            },
        }
    }
    asyncio.run(confirm_store.set_token(token, snap))
    return token


def test_confirm_rejected_audited(client: TestClient, rsa_key: Any) -> None:
    """发起人主动拒绝 → confirm_rejected 审计（不执行写入）。"""
    token = _issue_confirm_token("E1001", "s1")
    headers = {"Authorization": f"Bearer {make_token(rsa_key)}"}
    resp = client.post(f"/confirmations/{token}", json={"action": "reject"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    entries = asyncio.run(audit.recent(action="confirm_rejected"))
    assert len(entries) == 1
    assert entries[0]["user_id"] == "E1001"
    assert entries[0]["tool"] == "oa__approve"
    assert "AP-2026-0001" in entries[0]["params_digest"]


def test_confirm_denied_for_mismatched_confirmer(
    client: TestClient, rsa_key: Any
) -> None:
    """确认人 ≠ 发起人 → 403 + confirm_denied 审计（越权尝试留痕）。"""
    token = _issue_confirm_token("E1001", "s1")
    headers = {"Authorization": f"Bearer {make_token(rsa_key, sub='E1002', jti='jti-x')}"}
    resp = client.post(f"/confirmations/{token}", json={"action": "confirm"}, headers=headers)
    assert resp.status_code == 403
    entries = asyncio.run(audit.recent(action="confirm_denied"))
    assert len(entries) == 1
    assert entries[0]["user_id"] == "E1002"  # 记录确认人（越权尝试者）
    assert entries[0]["result"] == "denied"
    assert "E1001" in entries[0]["detail"]  # detail 含发起人对照


def test_confirm_approved_audited_and_executes(
    client: TestClient, rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发起人确认 → 恢复图执行写入 + confirm_approved 审计。"""

    async def _mock(name: str, arguments: dict[str, Any]) -> Any:
        assert name == "oa__approve"
        return {"approval_id": arguments["approval_id"], "doc_no": "LV-1", "status": "approved"}

    monkeypatch.setattr("agent_core.pipeline.graph.call_oa_tool", _mock)
    token = _issue_confirm_token("E1001", "s1")
    headers = {"Authorization": f"Bearer {make_token(rsa_key)}"}
    resp = client.post(f"/confirmations/{token}", json={"action": "confirm"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["final"]["text"]  # format 渲染终态
    entries = asyncio.run(audit.recent(action="confirm_approved"))
    assert len(entries) == 1
    assert entries[0]["user_id"] == "E1001"
    assert entries[0]["tool"] == "oa__approve"
