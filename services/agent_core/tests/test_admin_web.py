"""Web 管理后台测试（PLAN P2.7，PRD 5.6）。

会话门禁：未登录全部页面/表单 302 /admin/login（回跳目标保留）；
/admin/ 登录后根跳转 reviews；闲置 30 分钟会话过期即弃（滑动续期）。
OIDC 网页登录：/admin/login 302 IdP（state + PKCE S256）→ /callback
兑换建会话（_exchange_code 打桩）；无管理角色 403 拒绝；open redirect 钳制。
角色矩阵（PRD 5.6.1）：五页面逐页 403；CSRF：伪造 token 提交被拒且状态不变。
工作台全流程：技能双人复核（首核 in_review / 同核拒绝 / 异核 published）、
知识 D2 双人复核、自动化 pause/resume、功能开关切换（scheduler 实接线）、
MCP 注册 + 探测（httpx 打桩）、审计筛选 + CSV 水印导出 + audit_export
自审计、登出销毁会话、敏感页 no-store（PRD 5.6.4）。
"""

import asyncio
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from agent_core import audit, automation, syscfg
from agent_core.api import admin as admin_mod
from agent_core.api import auth as auth_mod
from agent_core.api.auth import AuthContext
from agent_core.api.main import app
from agent_core.automation import store as auto_store
from agent_core.knowledge import store as knowledge_store
from agent_core.skills import store as skill_store

ISSUER = auth_mod.SSO_ISSUER
AUDIENCE = auth_mod.SSO_AUDIENCE


def make_token(rsa_key: Any, **overrides: Any) -> str:
    """按 PRD 8.5.4 claims 结构签发测试 token（与 test_skill_store 同口径）。"""
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
    """测试隔离：会话/流程态清空 + 四域存储重建 + 调度器关闭 + 审计清空。"""
    auth_mod.set_sso_required(False)
    admin_mod._sessions.clear()
    admin_mod._flows.clear()
    syscfg.reset()
    skill_store.reset()
    knowledge_store.reset()
    auto_store.stop_scheduler()
    automation.reset()
    asyncio.run(audit.clear())
    yield
    auto_store.stop_scheduler()
    asyncio.run(audit.clear())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def ctx_of(user: str, roles: list[str]) -> AuthContext:
    """直构造管理身份（不走 JWKS，AuthContext 为 frozen dataclass）。"""
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
    """建会话并注入 cookie（绕过 OIDC 跳转，专注页面/表单行为断言）。"""
    sess = admin_mod.create_session(ctx_of(user, roles))
    client.cookies.set(admin_mod.SESSION_COOKIE, sess["sid"])
    return sess


def go(client: TestClient, url: str, **kw: Any) -> Any:
    kw.setdefault("follow_redirects", False)
    return client.get(url, **kw)


def post(client: TestClient, url: str, data: dict[str, str], **kw: Any) -> Any:
    kw.setdefault("follow_redirects", False)
    return client.post(url, data=data, **kw)


# ---- 会话门禁（未登录一律 302 登录页，PRD 5.6.4）----


@pytest.mark.parametrize(
    "page",
    [
        "/admin/",
        "/admin/reviews",
        "/admin/knowledge",
        "/admin/automation",
        "/admin/syscfg",
        "/admin/audit",
        "/admin/audit/export",
    ],
)
def test_unauthenticated_redirects_to_login(client: TestClient, page: str) -> None:
    r = go(client, page)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/admin/login")


def test_unauthenticated_post_redirects(client: TestClient) -> None:
    r = post(client, "/admin/reviews/any/decision", {"action": "approve"})
    assert r.status_code == 302
    assert r.headers["location"].startswith("/admin/login")


def test_index_redirects_to_reviews(client: TestClient) -> None:
    login_as(client, "sa1", ["system_admin"])
    r = go(client, "/admin/")
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/reviews"


def test_idle_session_expired_requires_relogin(client: TestClient) -> None:
    sess = login_as(client, "sa1", ["system_admin"])
    # 回拨 last_seen 越过 30 分钟闲置线：过期即弃，需重新登录
    admin_mod._sessions[sess["sid"]]["last_seen"] -= admin_mod.IDLE_TTL_SECONDS + 1
    r = go(client, "/admin/reviews")
    assert r.status_code == 302
    assert r.headers["location"].startswith("/admin/login")
    assert sess["sid"] not in admin_mod._sessions


# ---- 角色矩阵（PRD 5.6.1：页面级细分门禁）----


@pytest.mark.parametrize(
    ("page", "ok_roles", "denied_role"),
    [
        ("/admin/reviews", ["security_reviewer"], "knowledge_manager"),
        ("/admin/knowledge", ["knowledge_manager"], "auditor"),
        ("/admin/automation", ["system_admin"], "knowledge_manager"),
        ("/admin/syscfg", ["system_admin"], "security_reviewer"),
        ("/admin/audit", ["auditor"], "security_reviewer"),
    ],
)
def test_role_matrix_gate(client: TestClient, page: str, ok_roles: list[str], denied_role: str) -> None:
    login_as(client, "ok1", ok_roles)
    assert go(client, page).status_code == 200
    client.cookies.clear()
    login_as(client, "no1", [denied_role])
    r = go(client, page)
    assert r.status_code == 403
    assert "需要角色" in r.text


def test_admin_pages_no_store(client: TestClient) -> None:
    """敏感页不落浏览器缓存（PRD 5.6.4：/admin/* 统一 no-store）。"""
    login_as(client, "sa1", ["system_admin"])
    r = go(client, "/admin/syscfg")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.headers.get("pragma") == "no-cache"


# ---- CSRF（会话绑定 token，伪造提交被拒且状态不变）----


def test_csrf_rejected_on_review_decision(client: TestClient) -> None:
    asyncio.run(skill_store.register(name="bulk_write", title="批量写入", rw="write", by="E1001"))
    asyncio.run(skill_store.submit("bulk_write", by="E2002"))
    login_as(client, "SEC001", ["security_reviewer"])
    r = post(client, "/admin/reviews/bulk_write/decision", {"action": "approve", "_csrf": "forged"})
    assert r.status_code == 302
    assert "CSRF" in unquote(r.headers["location"])
    assert skill_store.status("bulk_write") == "in_review"  # 伪造提交不生效


# ---- 工作台 1：技能评审（双人复核经 Web 表单全流程，PRD 5.6.4）----


def test_skill_dual_review_flow_via_web(client: TestClient) -> None:
    asyncio.run(skill_store.register(name="bulk_write", title="批量写入", rw="write", by="E1001"))
    asyncio.run(skill_store.submit("bulk_write", by="E2002"))
    s1 = login_as(client, "SEC001", ["security_reviewer"])
    assert go(client, "/admin/reviews").status_code == 200
    # 首核：保持 in_review，提示待第二评审人复核
    r = post(client, "/admin/reviews/bulk_write/decision", {"action": "approve", "_csrf": s1["csrf"]})
    assert "第一复核通过" in unquote(r.headers["location"])
    assert skill_store.status("bulk_write") == "in_review"
    # 同核再批：两名不同安全评审员拒绝
    r = post(client, "/admin/reviews/bulk_write/decision", {"action": "approve", "_csrf": s1["csrf"]})
    assert "两名不同安全评审员" in unquote(r.headers["location"])
    assert skill_store.status("bulk_write") == "in_review"
    # 第二评审人异核通过 → published
    s2 = login_as(client, "SEC002", ["security_reviewer"])
    r = post(
        client,
        "/admin/reviews/bulk_write/decision",
        {"action": "approve", "_csrf": s2["csrf"], "note": "复核通过"},
    )
    assert "双人复核通过" in unquote(r.headers["location"])
    assert skill_store.status("bulk_write") == "published"


# ---- 工作台 2：知识库审核（D2 双人复核，PRD 5.6.4/9.5）----


def test_knowledge_d2_dual_review_via_web(client: TestClient) -> None:
    doc = asyncio.run(
        knowledge_store.upload(
            title="库存处理规范",
            content="# 规范\n先进先出，异常库存当日上报。",
            space="dept",
            dept_scope="信息科",
            classification="D2",
            uploaded_by="E9001",
        )
    )
    asyncio.run(knowledge_store.submit(doc["doc_id"], by="E9001"))
    s1 = login_as(client, "E9002", ["knowledge_manager"])
    r = post(client, f"/admin/knowledge/{doc['doc_id']}/decision", {"action": "approve", "_csrf": s1["csrf"]})
    assert "第一复核通过" in unquote(r.headers["location"])
    assert knowledge_store.detail(doc["doc_id"])["status"] == "pending_review"
    s2 = login_as(client, "E9003", ["knowledge_manager"])
    r = post(client, f"/admin/knowledge/{doc['doc_id']}/decision", {"action": "approve", "_csrf": s2["csrf"]})
    assert "已发布" in unquote(r.headers["location"])
    assert knowledge_store.detail(doc["doc_id"])["status"] == "published"


# ---- 工作台 3：自动化治理（管理员可暂停/恢复任意成员任务）----


def test_automation_pause_resume_via_web(client: TestClient) -> None:
    task = asyncio.run(
        automation.create(
            name="每日库存播报",
            skill="erp_inventory_query",
            params={"sku": {"value": "SKU-001", "source": "extract"}},
            schedule={"type": "daily", "time": "09:00"},
            owner="E1001",
            owner_auth={"user_id": "E1001", "dept": "事业部A/生产科", "roles": ["employee"]},
        )
    )
    s = login_as(client, "sa1", ["system_admin"])
    r = post(client, f"/admin/automation/{task['id']}/status", {"action": "pause", "_csrf": s["csrf"]})
    assert "已暂停" in unquote(r.headers["location"])
    assert automation.list_tasks()[0]["status"] == "paused"
    r = post(client, f"/admin/automation/{task['id']}/status", {"action": "resume", "_csrf": s["csrf"]})
    assert "已恢复" in unquote(r.headers["location"])
    assert automation.list_tasks()[0]["status"] == "active"


# ---- 工作台 4：系统配置（开关实接线 + MCP 注册/探测打桩）----


def test_syscfg_toggle_off_on(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 调度器线程绑定请求级 loop，单测内打桩 start_scheduler（实接线已在冒烟验证）
    monkeypatch.setattr(auto_store, "start_scheduler", lambda: None)
    key = "automation.scheduler_enabled"
    s = login_as(client, "sa1", ["system_admin"])
    r = post(client, "/admin/syscfg/toggle", {"key": key, "value": "false", "_csrf": s["csrf"]})
    assert "功能开关" in unquote(r.headers["location"])
    assert syscfg.get_toggle(key) is False
    post(client, "/admin/syscfg/toggle", {"key": key, "value": "true", "_csrf": s["csrf"]})
    assert syscfg.get_toggle(key) is True
    # 非系统管理员提交：403 门禁拦下，状态不被改动
    login_as(client, "sec1", ["security_reviewer"])
    r = post(client, "/admin/syscfg/toggle", {"key": key, "value": "false", "_csrf": "x"})
    assert r.status_code == 403
    assert syscfg.get_toggle(key) is True


def test_syscfg_mcp_register_and_probe(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    s = login_as(client, "sa1", ["system_admin"])
    r = post(
        client,
        "/admin/syscfg/mcp",
        {"name": "testsvc", "url": "http://probe.test", "_csrf": s["csrf"]},
    )
    assert "已注册" in unquote(r.headers["location"])
    assert any(m["name"] == "testsvc" for m in syscfg.list_mcp())

    # 探测打桩：httpx.AsyncClient 恒返回 200（不触网）
    class _FakeResp:
        status_code = 200

    class _FakeClient:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, timeout: Any = None) -> Any:
            return _FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    r = post(client, "/admin/syscfg/mcp/probe", {"_csrf": s["csrf"]})
    assert "已探测" in unquote(r.headers["location"])
    assert "probed=" in r.headers["location"]
    # 回跳带探测结果渲染页面
    r = go(client, r.headers["location"])
    assert r.status_code == 200


# ---- 工作台 5：审计（筛选 + CSV 水印导出 + 导出自审计，PRD 5.6.4）----


def test_audit_filter_and_export_watermark(client: TestClient) -> None:
    asyncio.run(audit.record("k_hello", tool="t", user_id="E1001", detail="你好世界"))
    asyncio.run(audit.record("k_other", tool="t", user_id="E2002", detail="他人条目"))
    login_as(client, "aud1", ["auditor"])
    r = go(client, "/admin/audit?user_id=E1001")
    assert r.status_code == 200
    assert "你好世界" in r.text
    assert "他人条目" not in r.text
    # 导出 CSV：头两行水印（标题 + 操作人/时间/条数），文件名带操作人
    r = go(client, "/admin/audit/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "_aud1.csv" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    assert r.text.startswith("# Chat-Work 审计导出")
    assert "操作人: aud1" in r.text
    # "审计者也被审计"：导出动作自身落 audit_export 审计
    events = asyncio.run(audit.recent(50, user_id="aud1"))
    assert any(e["action"] == "audit_export" for e in events)


# ---- OIDC 网页登录（复用同 realm 授权码 + PKCE S256，PRD 5.6.3）----


def test_oidc_login_flow(client: TestClient, rsa_key: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1) /admin/login 302 到 IdP 授权页（state 流程态 + PKCE S256）
    r = go(client, "/admin/login?next=/admin/reviews")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(f"{ISSUER}/protocol/openid-connect/auth")
    q = parse_qs(urlparse(loc).query)
    assert q["code_challenge_method"] == ["S256"]
    state = q["state"][0]
    assert state in admin_mod._flows
    # 2) 兑换打桩 → 回调验签建会话并回跳 next
    token = make_token(rsa_key, sub="sa9", roles=["system_admin"])

    async def fake_exchange(code: str, verifier: str) -> dict[str, Any]:
        return {"access_token": token}

    monkeypatch.setattr(admin_mod, "_exchange_code", fake_exchange)
    r = go(client, f"/admin/callback?code=abc&state={state}")
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/reviews"
    assert "admin_session=" in r.headers["set-cookie"]
    assert any(v["user_id"] == "sa9" for v in admin_mod._sessions.values())


def test_callback_rejects_non_admin_role(client: TestClient, rsa_key: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    r = go(client, "/admin/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    token = make_token(rsa_key, sub="E1001", roles=["employee"])

    async def fake_exchange(code: str, verifier: str) -> dict[str, Any]:
        return {"access_token": token}

    monkeypatch.setattr(admin_mod, "_exchange_code", fake_exchange)
    r = go(client, f"/admin/callback?code=abc&state={state}")
    assert r.status_code == 403
    assert "无管理角色" in r.text
    assert admin_mod._sessions == {}  # 未建任何会话


# ---- 登出 + open redirect 钳制 ----


def test_logout_destroys_session(client: TestClient) -> None:
    s = login_as(client, "sa1", ["system_admin"])
    assert s["sid"] in admin_mod._sessions
    r = post(client, "/admin/logout", {"_csrf": s["csrf"]})
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/login"
    assert s["sid"] not in admin_mod._sessions
    r = go(client, "/admin/reviews")
    assert r.status_code == 302
    assert r.headers["location"].startswith("/admin/login")


def test_login_open_redirect_guard(client: TestClient) -> None:
    r = go(client, "/admin/login?next=https://evil.example")
    assert r.status_code == 302
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert admin_mod._flows[state]["next"] == "/"  # 外域回跳被钳制为站内根
