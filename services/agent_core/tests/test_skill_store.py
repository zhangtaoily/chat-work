"""技能市场测试（PLAN P2.3，PRD 3.4/3.5/5.6）。

单元层（store 状态机）：内置基线、只读自动发布、写入类 SEC-RV-* 评审流、
驳回重提、下架后路由联动（match_skill 降级闲聊）、非法迁移 ValueError、
使用统计累加、审计留痕（params_digest 截断 JSON 字符串口径）。
API 层（TestClient + make_token）：恒需认证 401、角色门禁
（security_reviewer 评审 / dept_manager 下架 / include_unpublished 视图）、
dept_scope 安装跨科室 403、未上架不可安装、注册→提交→评审→安装全流程。
"""

import time
from typing import Any

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from agent_core import audit
from agent_core.api import auth as auth_mod
from agent_core.api.main import app
from agent_core.skills import store
from agent_core.skills.registry import match_skill

ISSUER = auth_mod.SSO_ISSUER
AUDIENCE = auth_mod.SSO_AUDIENCE


def make_token(rsa_key: Any, **overrides: Any) -> str:
    """按 PRD 8.5.4 claims 结构签发测试 token（与 test_api_auth 同口径）。"""
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


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_market() -> None:
    """测试隔离：市场状态重建内置基线 + 关闭强制鉴权。"""
    auth_mod.set_sso_required(False)
    store.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---- store 状态机（单元）----


def test_builtin_baseline() -> None:
    """内置技能基线：官方/科室自动归类，全部已上架（PRD 3.4 分类）。"""
    official = store.detail("oa_leave_request")
    assert official is not None
    assert official["status"] == "published"
    assert official["category"] == "official"
    dept = store.detail("wh_stock_overview")
    assert dept is not None
    assert dept["category"] == "dept"
    assert dept["dept_scope"] == "仓库物流"
    assert store.is_published("oa_leave_request")


@pytest.mark.asyncio
async def test_register_and_read_auto_publish() -> None:
    """只读技能提交即发布（PRD 3.4：无需审核仅限只读工具）。"""
    await store.register(
        name="stock_lookup",
        title="库存速查",
        rw="read",
        category="dept",
        dept_scope="仓库物流",
        by="E1001",
    )
    assert store.status("stock_lookup") == "draft"
    meta = await store.submit("stock_lookup", by="E2002")
    assert meta["status"] == "published"
    assert meta["review_id"] is None
    assert meta["review_note"] == "只读技能自动发布"
    assert meta["published_at"] is not None


@pytest.mark.asyncio
async def test_write_review_flow_seq() -> None:
    """写入类技能：submit → in_review（SEC-RV 单号自增）→ approve → published。"""
    await store.register(name="bulk_write", title="批量写入", rw="write", by="E1001")
    meta = await store.submit("bulk_write", by="E2002", note="首次提交")
    assert meta["status"] == "in_review"
    assert meta["review_id"] == "SEC-RV-0001"
    meta = await store.review("bulk_write", approve=True, by="SEC001", note="通过")
    assert meta["status"] == "published"
    assert meta["reviewed_by"] == "SEC001"
    # 第二个写入技能评审单号自增
    await store.register(name="bulk_write2", title="批量写入二", rw="write", by="E1001")
    meta = await store.submit("bulk_write2", by="E2002")
    assert meta["review_id"] == "SEC-RV-0002"


@pytest.mark.asyncio
async def test_reject_then_resubmit() -> None:
    """驳回后可重提（rejected ──submit──> in_review，换发新评审单号）。"""
    await store.register(name="bulk_write", title="批量写入", rw="write", by="E1001")
    await store.submit("bulk_write", by="E2002")
    meta = await store.review("bulk_write", approve=False, by="SEC001", note="缺少数据脱敏")
    assert meta["status"] == "rejected"
    meta = await store.submit("bulk_write", by="E2002")
    assert meta["status"] == "in_review"
    assert meta["review_id"] == "SEC-RV-0002"


@pytest.mark.asyncio
async def test_deprecate_disables_routing() -> None:
    """下架联动路由：deprecated 技能 match_skill 不再命中（降级闲聊兜底）。"""
    assert match_skill("帮我查出库单") is not None
    await store.deprecate("wms_stock_alert", by="SEC001", note="临时整改")
    assert store.status("wms_stock_alert") == "deprecated"
    assert not store.is_published("wms_stock_alert")
    assert match_skill("帮我查出库单") is None


@pytest.mark.asyncio
async def test_invalid_transitions() -> None:
    """非法迁移/参数：重复注册、rw/category 非法、draft 直接评审、未注册提交。"""
    with pytest.raises(ValueError):
        await store.submit("no_such_skill", by="E1001")
    await store.register(name="demo", title="演示技能", rw="write", by="E1001")
    with pytest.raises(ValueError):
        await store.register(name="demo", title="重复注册", rw="read", by="E1001")
    with pytest.raises(ValueError):
        await store.register(name="demo_bad_rw", title="非法rw", rw="delete", by="E1001")
    with pytest.raises(ValueError):
        await store.register(
            name="demo_bad_cat", title="非法分类", rw="read", category="team", by="E1001"
        )
    with pytest.raises(ValueError):
        await store.review("demo", approve=True, by="SEC001")  # draft 不在评审中
    assert store.is_published("demo") is False


def test_record_usage_stats() -> None:
    """使用统计：仅触达 MCP 的调用计数；未注册技能静默忽略。"""
    store.record_usage("oa_leave_request", ok=True)
    store.record_usage("oa_leave_request", ok=True)
    store.record_usage("oa_leave_request", ok=False)
    meta = store.detail("oa_leave_request")
    assert meta is not None
    assert meta["stats"] == {"calls": 3, "success": 2, "failed": 1}
    store.record_usage("no_such_skill", ok=True)


@pytest.mark.asyncio
async def test_audit_trail_digest() -> None:
    """生命周期动作落审计（PRD 5.6.4）：params_digest 为截断 JSON 字符串口径。"""
    await store.register(name="bulk_write", title="批量写入", rw="write", by="E1001")
    await store.submit("bulk_write", by="E2002")
    await store.review("bulk_write", approve=True, by="SEC001")
    await store.deprecate("bulk_write", by="SEC001")
    actions = {e["action"] for e in await audit.recent(limit=100)}
    assert {"skill_register", "skill_submit", "skill_review", "skill_deprecate"} <= actions
    sub = next(e for e in await audit.recent(limit=100) if e["action"] == "skill_submit")
    assert "bulk_write" in sub["params_digest"]
    assert "SEC-RV-0001" in sub["params_digest"]


# ---- API 门禁与角色（TestClient）----


def test_api_market_requires_auth() -> None:
    """恒需认证：无 token → 401（口径对齐 /audit，本地冒烟也不放行）。"""
    auth_mod.set_sso_required(True)
    c = TestClient(app)
    assert c.get("/skills").status_code == 401
    assert c.get("/skills/oa_leave_request").status_code == 401
    assert c.get("/skills/mine").status_code == 401


def test_api_marketplace_and_detail(client: TestClient, rsa_key: Any) -> None:
    """广场：仅 published + 分类/关键词过滤；详情合并运行时定义。"""
    tok = make_token(rsa_key)
    body = client.get("/skills", headers=auth(tok)).json()
    assert body["count"] >= 15  # 内置基线全量上架
    assert all(i["status"] == "published" for i in body["items"])
    dept_items = client.get("/skills", params={"category": "dept"}, headers=auth(tok)).json()
    assert dept_items["count"] == 5  # P2.2 五科室技能
    assert all(i["category"] == "dept" for i in dept_items["items"])
    searched = client.get("/skills", params={"q": "库存"}, headers=auth(tok)).json()
    assert any(i["name"] == "erp_inventory_query" for i in searched["items"])
    detail = client.get("/skills/oa_leave_request", headers=auth(tok)).json()
    assert detail["status"] == "published"
    assert detail["definition"]["rw"] == "write"
    assert client.get("/skills/unknown_skill", headers=auth(tok)).status_code == 404


def test_api_include_unpublished_gate(client: TestClient, rsa_key: Any) -> None:
    """未上架技能仅管理员可见（include_unpublished，评审工作台数据源）。"""
    emp = make_token(rsa_key)
    r = client.get("/skills", params={"include_unpublished": "true"}, headers=auth(emp))
    assert r.status_code == 403
    sec = make_token(rsa_key, roles=["security_reviewer"])
    r = client.get("/skills", params={"include_unpublished": "true"}, headers=auth(sec))
    assert r.status_code == 200


def test_api_install_dept_scope(client: TestClient, rsa_key: Any) -> None:
    """一键安装权限校验：科室技能 dept 尾段匹配（对齐 check_dept_scope）。"""
    cross = make_token(rsa_key, dept="事业部A/销售科")
    r = client.post("/skills/wh_stock_overview/install", headers=auth(cross))
    assert r.status_code == 403
    assert "仓库物流" in r.json()["detail"]
    same = make_token(rsa_key, dept="事业部B/仓库物流")
    assert client.post("/skills/wh_stock_overview/install", headers=auth(same)).status_code == 200
    mine = client.get("/skills/mine", headers=auth(same)).json()
    assert [i["name"] for i in mine["items"]] == ["wh_stock_overview"]
    assert client.delete("/skills/wh_stock_overview/install", headers=auth(same)).status_code == 200
    assert client.get("/skills/mine", headers=auth(same)).json()["count"] == 0


def test_api_install_requires_published(client: TestClient, rsa_key: Any) -> None:
    """未上架技能不可安装（draft 状态 400）。"""
    tok = make_token(rsa_key)
    r = client.post(
        "/skills/manage",
        json={"name": "draft_skill", "title": "草稿技能", "rw": "read"},
        headers=auth(tok),
    )
    assert r.status_code == 200
    assert client.post("/skills/draft_skill/install", headers=auth(tok)).status_code == 400


def test_api_review_role_gate(client: TestClient, rsa_key: Any) -> None:
    """评审门禁：security_reviewer 专属（PRD 5.6.1 安全评审员职责）。"""
    emp = make_token(rsa_key)
    r = client.post(
        "/skills/manage",
        json={"name": "write_skill", "title": "写入技能", "rw": "write"},
        headers=auth(emp),
    )
    assert r.status_code == 200
    assert client.post("/skills/write_skill/submit", json={}, headers=auth(emp)).status_code == 200
    r = client.post("/skills/write_skill/review", json={"approve": True}, headers=auth(emp))
    assert r.status_code == 403
    sec = make_token(rsa_key, roles=["security_reviewer"])
    r = client.post(
        "/skills/write_skill/review",
        json={"approve": True, "note": "数据面无越权"},
        headers=auth(sec),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "published"


def test_api_deprecate_role_gate(client: TestClient, rsa_key: Any) -> None:
    """下架门禁：dept_manager/security_reviewer；下架后广场不可见、重复下架 400。"""
    emp = make_token(rsa_key)
    r = client.post("/skills/wms_stock_alert/deprecate", json={}, headers=auth(emp))
    assert r.status_code == 403
    mgr = make_token(rsa_key, roles=["dept_manager"])
    r = client.post(
        "/skills/wms_stock_alert/deprecate", json={"note": "临时整改"}, headers=auth(mgr)
    )
    assert r.status_code == 200
    assert r.json()["status"] == "deprecated"
    items = client.get("/skills", headers=auth(emp)).json()["items"]
    assert all(i["name"] != "wms_stock_alert" for i in items)
    r = client.post("/skills/wms_stock_alert/deprecate", json={}, headers=auth(mgr))
    assert r.status_code == 400


def test_api_lifecycle_full_flow(client: TestClient, rsa_key: Any) -> None:
    """注册 → 提交（SEC-RV-*）→ 评审 → 安装 → 我的技能全流程。"""
    emp = make_token(rsa_key)
    r = client.post(
        "/skills/manage",
        json={
            "name": "sales_broadcast",
            "title": "销售群发通知",
            "rw": "write",
            "category": "dept",
        },
        headers=auth(emp),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "draft"
    # 重复注册 400
    r = client.post(
        "/skills/manage",
        json={"name": "sales_broadcast", "title": "销售群发通知", "rw": "write"},
        headers=auth(emp),
    )
    assert r.status_code == 400
    # 提交 → in_review + 评审单
    r = client.post("/skills/sales_broadcast/submit", json={"note": "首次上架"}, headers=auth(emp))
    assert r.status_code == 200
    assert r.json()["review_id"] == "SEC-RV-0001"
    # security_reviewer 评审通过后安装
    sec = make_token(rsa_key, roles=["security_reviewer"])
    r = client.post("/skills/sales_broadcast/review", json={"approve": True}, headers=auth(sec))
    assert r.status_code == 200
    assert client.post("/skills/sales_broadcast/install", headers=auth(emp)).status_code == 200
    mine = client.get("/skills/mine", headers=auth(emp)).json()
    assert [i["name"] for i in mine["items"]] == ["sales_broadcast"]
