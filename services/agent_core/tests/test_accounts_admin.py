"""管理后台「数字分身账号」工作台测试（P0-3）。

门禁：未登录 302；org_admin 管理 / system_admin 只读 / 其他角色 403。
分身开关（twin toggle + 审计）；冻结三合一（frozen 标记 + token denylist
立即 401 + IdP 停用打桩）与自冻结防护；密码重置（长度校验 + IdP 打桩 +
审计）；技能授权授予/回收（deny 语义 → permissions.check_skill_grant
写路径拒绝）；CSRF 防护（伪造 token 状态不变）。
"""

import asyncio
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote
from uuid import uuid4

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from agent_core import audit
from agent_core.accounts import store as accounts_store
from agent_core.api import admin as admin_mod
from agent_core.api import auth as auth_mod
from agent_core.api.auth import AuthContext
from agent_core.api.main import app
from agent_core.pipeline import permissions
from agent_core.skills import store as skill_store

ISSUER = auth_mod.SSO_ISSUER
AUDIENCE = auth_mod.SSO_AUDIENCE


def make_token(rsa_key: Any, **overrides: Any) -> str:
    """按 PRD 8.5.4 claims 结构签发测试 token（与 test_admin_web 同口径）。"""
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


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """测试隔离：会话/denylist/账号态/技能市场清空 + 审计清空。"""
    auth_mod.set_sso_required(False)
    admin_mod._sessions.clear()
    admin_mod._flows.clear()
    auth_mod._user_deny.clear()
    accounts_store.reset()
    skill_store.reset()
    asyncio.run(audit.clear())
    yield
    asyncio.run(audit.clear())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def ctx_of(user: str, roles: list[str]) -> AuthContext:
    return AuthContext(
        user_id=user,
        idp="ad",
        idp_sub=f"{user}@corp.com",
        dept="信息科",
        roles=list(roles),
        perm_ver=17,
        jti=uuid4().hex,
        session_id=f"web-{user}",
    )


def login_as(client: TestClient, user: str, roles: list[str]) -> dict[str, Any]:
    sess = admin_mod.create_session(ctx_of(user, roles))
    client.cookies.set(admin_mod.SESSION_COOKIE, sess["sid"])
    return sess


def go(client: TestClient, url: str, **kw: Any) -> Any:
    kw.setdefault("follow_redirects", False)
    return client.get(url, **kw)


def post(client: TestClient, url: str, data: dict[str, str], **kw: Any) -> Any:
    kw.setdefault("follow_redirects", False)
    return client.post(url, data=data, **kw)


# ---- 门禁（PRD 5.6.1 套路：页面级角色矩阵）----


def test_unauthenticated_redirects_to_login(client: TestClient) -> None:
    r = go(client, "/admin/accounts")
    assert r.status_code == 302
    assert "/admin/login" in r.headers["location"]


def test_non_admin_role_403(client: TestClient) -> None:
    login_as(client, "E8004", ["auditor"])
    assert go(client, "/admin/accounts").status_code == 403


def test_system_admin_readonly(client: TestClient) -> None:
    sess = login_as(client, "E8001", ["system_admin"])
    r = go(client, "/admin/accounts")
    assert r.status_code == 200
    assert "查看" in r.text and "/admin/accounts/E1001/profile" not in r.text  # 列表仅查看入口
    r = go(client, "/admin/accounts/E1001")
    assert r.status_code == 200
    assert "只读" in r.text and "/admin/accounts/E1001/profile" not in r.text
    r = post(client, "/admin/accounts/E1001/twin", {"_csrf": sess["csrf"], "value": "false"})
    assert r.status_code == 403
    assert accounts_store.is_twin_enabled("E1001")


def test_org_admin_page_lists_directory(client: TestClient) -> None:
    login_as(client, "E8001", ["org_admin"])
    r = go(client, "/admin/accounts")
    assert r.status_code == 200
    assert "数字分身账号" in r.text  # 导航项
    assert len(accounts_store.list_accounts()) == 13
    for frag in ("E1001", "张三", "王五", "E8002", "开启", "正常"):
        assert frag in r.text


# ---- 账号详情页（独立编辑入口，替代列表页内联表单）----


def test_account_detail_renders_edit_forms(client: TestClient) -> None:
    """org_admin 打开详情页：基本信息 + 全部编辑表单集中于此。"""
    login_as(client, "E8001", ["org_admin"])
    r = go(client, "/admin/accounts/E1001")
    assert r.status_code == 200
    for frag in ("张三", "销售科", "保存档案", "重置密码", "分身开启", "关闭分身", "授予"):
        assert frag in r.text
    for action in ("twin", "freeze", "profile", "grants", "password"):
        assert f"/admin/accounts/E1001/{action}" in r.text


def test_account_detail_unknown_404(client: TestClient) -> None:
    login_as(client, "E8001", ["org_admin"])
    assert go(client, "/admin/accounts/E9999").status_code == 404


def test_account_detail_role_gate(client: TestClient) -> None:
    """auditor 无账号工作台角色：详情页 403（与列表页同一门禁）。"""
    login_as(client, "E8004", ["auditor"])
    assert go(client, "/admin/accounts/E1001").status_code == 403


def test_post_redirects_back_to_detail(client: TestClient) -> None:
    """详情页发起的操作回跳详情页（不回列表页），flash 提示在原页展示。"""
    sess = login_as(client, "E8001", ["org_admin"])
    r = post(client, "/admin/accounts/E1001/profile", {"_csrf": sess["csrf"], "education": "本科"})
    assert r.status_code == 302
    assert r.headers["location"].startswith("/admin/accounts/E1001")
    assert "msg=" in r.headers["location"]
    assert accounts_store.profile_of("E1001") == {"education": "本科"}


# ---- 分身开关 ----


def test_twin_toggle(client: TestClient) -> None:
    sess = login_as(client, "E8001", ["org_admin"])
    r = post(client, "/admin/accounts/E1001/twin", {"_csrf": sess["csrf"], "value": "false"})
    assert r.status_code == 302
    assert not accounts_store.is_twin_enabled("E1001")
    events = asyncio.run(audit.recent(50))
    assert any(e["action"] == "account_twin" and e["user_id"] == "E8001" for e in events)
    r = post(client, "/admin/accounts/E1001/twin", {"_csrf": sess["csrf"], "value": "true"})
    assert r.status_code == 302
    assert accounts_store.is_twin_enabled("E1001")


def test_unknown_account_twin_rejected(client: TestClient) -> None:
    sess = login_as(client, "E8001", ["org_admin"])
    r = post(client, "/admin/accounts/E9999/twin", {"_csrf": sess["csrf"], "value": "false"})
    assert r.status_code == 302
    assert "err=" in r.headers["location"]


# ---- 冻结三合一（frozen + token denylist + IdP 停用）----


def test_freeze_revokes_tokens_and_idp(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, rsa_key: Any
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_idp(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((path, payload))
        return {"status": "ok"}

    monkeypatch.setattr(admin_mod, "_idp_post", fake_idp)
    sess = login_as(client, "E8001", ["org_admin"])

    r = post(client, "/admin/accounts/E1002/freeze", {"_csrf": sess["csrf"], "action": "freeze"})
    assert r.status_code == 302
    assert accounts_store.is_frozen("E1002")
    assert "E1002" in auth_mod._user_deny
    assert calls == [("/internal/users/E1002/status", {"enabled": False})]

    # 冻结前签发的 token 立即被拒（401 文案统一，不暴露细节）
    token = make_token(rsa_key, sub="E1002", jti="jti-frz-1")
    with pytest.raises(pyjwt.InvalidTokenError, match="账号已被停用"):
        asyncio.run(auth_mod.verify_token(token))

    r = post(client, "/admin/accounts/E1002/freeze", {"_csrf": sess["csrf"], "action": "unfreeze"})
    assert r.status_code == 302
    assert not accounts_store.is_frozen("E1002")
    assert "E1002" not in auth_mod._user_deny
    assert calls[-1] == ("/internal/users/E1002/status", {"enabled": True})
    asyncio.run(auth_mod.verify_token(token))  # 解冻后同一 token 恢复可用


def test_self_freeze_blocked(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_idp(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((path, payload))
        return {}

    monkeypatch.setattr(admin_mod, "_idp_post", fake_idp)
    sess = login_as(client, "E8001", ["org_admin"])
    r = post(client, "/admin/accounts/E8001/freeze", {"_csrf": sess["csrf"], "action": "freeze"})
    assert r.status_code == 302
    assert "err=" in r.headers["location"]
    assert not accounts_store.is_frozen("E8001")
    assert not calls


def test_freeze_idp_down_still_effective(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """IdP 不可达：agent 侧冻结照常生效，回跳提示携带失败说明。"""

    async def broken_idp(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("connect timeout")

    monkeypatch.setattr(admin_mod, "_idp_post", broken_idp)
    sess = login_as(client, "E8001", ["org_admin"])
    r = post(client, "/admin/accounts/E1002/freeze", {"_csrf": sess["csrf"], "action": "freeze"})
    assert r.status_code == 302
    assert accounts_store.is_frozen("E1002")
    assert "E1002" in auth_mod._user_deny
    assert "IdP" in r.headers["location"]


# ---- 密码重置（IdP 联动）----


def test_password_reset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_idp(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((path, payload))
        return {"status": "ok"}

    monkeypatch.setattr(admin_mod, "_idp_post", fake_idp)
    sess = login_as(client, "E8001", ["org_admin"])

    r = post(client, "/admin/accounts/E1001/password", {"_csrf": sess["csrf"], "password": "short"})
    assert r.status_code == 302
    assert "err=" in r.headers["location"]
    assert not calls

    r = post(client, "/admin/accounts/E1001/password", {"_csrf": sess["csrf"], "password": "NewPass@2026"})
    assert r.status_code == 302
    assert "msg=" in r.headers["location"]
    assert calls == [("/internal/users/E1001/password", {"password": "NewPass@2026"})]
    events = asyncio.run(audit.recent(50))
    assert any(e["action"] == "account_password_reset" for e in events)


# ---- 档案补录（籍贯/学历/专业/岗位，PRD 8.5.6 admin 来源）----


def test_account_profile_update_and_audit(client: TestClient) -> None:
    sess = login_as(client, "E8001", ["org_admin"])
    fields = {
        "native_place": "浙江杭州",
        "education": "本科",
        "major": "市场营销",
        "position": "销售代表",
    }
    r = post(client, "/admin/accounts/E1001/profile", {"_csrf": sess["csrf"], **fields})
    assert r.status_code == 302
    assert "msg=" in r.headers["location"]
    assert accounts_store.profile_of("E1001") == fields
    # 列表视图带档案，页面渲染 tag 形式展示
    assert accounts_store.list_accounts()[0]["profile"]["education"] == "本科"
    login_as(client, "E8001", ["org_admin"])
    r = go(client, "/admin/accounts")
    assert "籍贯:浙江杭州" in r.text and "专业:市场营销" in r.text
    events = asyncio.run(audit.recent(50))
    assert any(e["action"] == "account_profile" and e["user_id"] == "E8001" for e in events)


def test_account_profile_whitelist_and_clear(client: TestClient) -> None:
    sess = login_as(client, "E8001", ["org_admin"])
    r = post(
        client,
        "/admin/accounts/E1002/profile",
        {"_csrf": sess["csrf"], "education": "硕士", "hacker_field": "x"},
    )
    assert r.status_code == 302
    assert accounts_store.profile_of("E1002") == {"education": "硕士"}
    # 空串=清除该字段；全部清空后档案移除
    r = post(client, "/admin/accounts/E1002/profile", {"_csrf": sess["csrf"], "education": ""})
    assert r.status_code == 302
    assert accounts_store.profile_of("E1002") == {}
    assert "已清空" in unquote(r.headers["location"])
    assert all(a["profile"] == {} for a in accounts_store.list_accounts())


def test_account_profile_unknown_account(client: TestClient) -> None:
    sess = login_as(client, "E8001", ["org_admin"])
    r = post(client, "/admin/accounts/E9999/profile", {"_csrf": sess["csrf"], "education": "本科"})
    assert r.status_code == 302
    assert "err=" in r.headers["location"]


def test_account_profile_role_gate(client: TestClient) -> None:
    """system_admin 只读：档案 POST 被拒且状态不变（同 twin/freeze 门禁口径）。"""
    sess = login_as(client, "E8001", ["system_admin"])
    r = post(client, "/admin/accounts/E1001/profile", {"_csrf": sess["csrf"], "education": "博士"})
    assert r.status_code == 403
    assert accounts_store.profile_of("E1001") == {}


# ---- 技能授权绑定（deny 语义，P0-2/P0-3 联动）----


def test_grant_then_revoke_blocks_write_path(client: TestClient) -> None:
    sess = login_as(client, "E8001", ["org_admin"])
    name = next(m["name"] for m in skill_store.list_meta() if m["rw"] == "write")
    auth: dict[str, Any] = {"user_id": "E1001", "roles": ["employee"]}

    # 授予前：deny 语义默认放行（不破坏既有链路）
    assert permissions.check_skill_grant(auth, {"name": name}) is None
    r = post(
        client,
        "/admin/accounts/E1001/grants",
        {"_csrf": sess["csrf"], "action": "grant", "skill": name},
    )
    assert r.status_code == 302
    assert "msg=" in r.headers["location"]
    assert skill_store.has_grant("E1001", name)
    assert permissions.check_skill_grant(auth, {"name": name}) is None

    # 回收 → 写路径立即拒绝（permission 节点消费同一函数）
    r = post(
        client,
        "/admin/accounts/E1001/grants",
        {"_csrf": sess["csrf"], "action": "revoke", "skill": name, "note": "越权风险"},
    )
    assert r.status_code == 302
    assert skill_store.is_revoked("E1001", name)
    message = permissions.check_skill_grant(auth, {"name": name})
    assert message is not None and "联系管理员" in message

    events = asyncio.run(audit.recent(50))
    actions = [e["action"] for e in events]
    assert "skill_grant" in actions
    assert "skill_grant_revoke" in actions


def test_grant_unknown_skill_rejected(client: TestClient) -> None:
    sess = login_as(client, "E8001", ["org_admin"])
    r = post(
        client,
        "/admin/accounts/E1001/grants",
        {"_csrf": sess["csrf"], "action": "grant", "skill": "nope_skill"},
    )
    assert r.status_code == 302
    assert "err=" in r.headers["location"]


def test_grant_unknown_account_rejected(client: TestClient) -> None:
    sess = login_as(client, "E8001", ["org_admin"])
    name = next(m["name"] for m in skill_store.list_meta() if m["rw"] == "write")
    r = post(
        client,
        "/admin/accounts/E9999/grants",
        {"_csrf": sess["csrf"], "action": "grant", "skill": name},
    )
    assert r.status_code == 302
    assert "err=" in r.headers["location"]


# ---- CSRF 防护 ----


def test_csrf_blocked_state_unchanged(client: TestClient) -> None:
    login_as(client, "E8001", ["org_admin"])
    r = post(client, "/admin/accounts/E1001/twin", {"_csrf": "wrong-token", "value": "false"})
    assert r.status_code == 302
    assert "err=" in r.headers["location"]
    assert accounts_store.is_twin_enabled("E1001")
