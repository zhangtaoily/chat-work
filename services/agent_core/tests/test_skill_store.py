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

from agent_core import audit, persist
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
    """写入类技能：submit → in_review（SEC-RV 单号自增）→ 双人复核通过 → published。"""
    await store.register(name="bulk_write", title="批量写入", rw="write", by="E1001")
    meta = await store.submit("bulk_write", by="E2002", note="首次提交")
    assert meta["status"] == "in_review"
    assert meta["review_id"] == "SEC-RV-0001"
    meta = await store.review("bulk_write", approve=True, by="SEC001", note="通过")
    assert meta["status"] == "in_review"  # 双人复核：第一核后仍 in_review
    assert [a["by"] for a in meta["approvals"]] == ["SEC001"]
    with pytest.raises(ValueError, match="两名不同安全评审员"):
        await store.review("bulk_write", approve=True, by="SEC001")  # 同核拒绝
    meta = await store.review("bulk_write", approve=True, by="SEC002", note="复核通过")
    assert meta["status"] == "published"
    assert meta["reviewed_by"] == "SEC002"
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
    await store.review("bulk_write", approve=True, by="SEC002")  # 双人复核后发布
    await store.deprecate("bulk_write", by="SEC001")
    actions = {e["action"] for e in await audit.recent(limit=100)}
    assert {"skill_register", "skill_submit", "skill_review", "skill_deprecate"} <= actions
    events = await audit.recent(limit=100)
    assert any(e["result"] == "first_approved" for e in events)  # 第一复核留痕
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
    # 销售科专属（CRM 客户 360/订单跟单，P1-2.4 归类）：仅本科室可见
    assert dept_items["count"] == 2
    assert {i["name"] for i in dept_items["items"]} == {"crm_customer_360", "crm_order_track"}
    wh_tok = make_token(rsa_key, dept="事业部B/仓库物流")
    dept_items = client.get("/skills", params={"category": "dept"}, headers=auth(wh_tok)).json()
    assert dept_items["count"] == 1  # 本科室 dept 技能可见
    assert [i["name"] for i in dept_items["items"]] == ["wh_stock_overview"]
    searched = client.get("/skills", params={"q": "库存"}, headers=auth(tok)).json()
    assert any(i["name"] == "erp_inventory_query" for i in searched["items"])
    detail = client.get("/skills/oa_leave_request", headers=auth(tok)).json()
    assert detail["status"] == "published"
    assert detail["definition"]["rw"] == "write"
    assert client.get("/skills/unknown_skill", headers=auth(tok)).status_code == 404


def test_marketplace_visibility_by_dept(client: TestClient, rsa_key: Any) -> None:
    """部门可见性隔离（PRD 3.4）：普通视角仅 official（通用）+ 本科室 dept。"""
    sales = make_token(rsa_key)  # 事业部A/销售科（无专属技能）
    names = {i["name"] for i in client.get("/skills", headers=auth(sales)).json()["items"]}
    assert "oa_leave_request" in names  # official 通用可见
    assert "erp_inventory_query" in names
    assert not ({"wh_stock_overview", "prod_material_check", "hr_roster"} & names)  # 他科室专属
    wh = make_token(rsa_key, dept="事业部B/仓库物流")
    names = {i["name"] for i in client.get("/skills", headers=auth(wh)).json()["items"]}
    assert "wh_stock_overview" in names  # 本科室 dept 可见
    assert "prod_material_check" not in names  # 他科室 dept 不可见
    assert "oa_leave_request" in names  # official 照常


def test_skill_detail_hidden_cross_dept(client: TestClient, rsa_key: Any) -> None:
    """详情直访防越权：不可见技能 404（不向未授权者泄露存在性）。"""
    sales = make_token(rsa_key)
    assert client.get("/skills/prod_material_check", headers=auth(sales)).status_code == 404
    wh = make_token(rsa_key, dept="事业部B/仓库物流")
    r = client.get("/skills/wh_stock_overview", headers=auth(wh))
    assert r.status_code == 200
    assert r.json()["category"] == "dept"


def test_marketplace_reviewer_sees_all(client: TestClient, rsa_key: Any) -> None:
    """评审豁免：security_reviewer 的 include_unpublished 视角全量可见
    （评审工作台数据源——评审员须能见他科室提交的在评技能）。"""
    sec = make_token(rsa_key, roles=["security_reviewer"], dept="事业部A/信息科")
    body = client.get(
        "/skills", params={"include_unpublished": "true"}, headers=auth(sec)
    ).json()
    names = {i["name"] for i in body["items"]}
    assert {"wh_stock_overview", "prod_material_check"} <= names  # 他科室 dept 技能仍在
    # 普通视角（include_unpublished=false）对评审员同样按部门过滤
    normal = client.get("/skills", headers=auth(sec)).json()
    assert all(i["dept_scope"] in (None, "信息科") for i in normal["items"])


def test_personal_skill_owner_only(client: TestClient, rsa_key: Any) -> None:
    """personal 技能仅本人可见可装：他人列表不可见、install 403。"""
    owner = make_token(rsa_key)  # E1001
    assert (
        client.post(
            "/skills/manage",
            json={"name": "my_helper", "title": "私人助手", "rw": "read", "category": "personal"},
            headers=auth(owner),
        ).status_code
        == 200
    )
    assert client.post("/skills/my_helper/submit", json={}, headers=auth(owner)).status_code == 200
    other = make_token(rsa_key, sub="E2002", dept="事业部B/仓库物流")
    names = {i["name"] for i in client.get("/skills", headers=auth(other)).json()["items"]}
    assert "my_helper" not in names
    r = client.post("/skills/my_helper/install", headers=auth(other))
    assert r.status_code == 403
    assert "仅本人" in r.json()["detail"]
    assert "my_helper" in {i["name"] for i in client.get("/skills", headers=auth(owner)).json()["items"]}
    assert client.post("/skills/my_helper/install", headers=auth(owner)).status_code == 200


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
    assert r.json()["status"] == "in_review"  # 双人复核：第一核后仍 in_review
    # 同一评审人重复通过 → 400（防自批自核）
    assert (
        client.post("/skills/write_skill/review", json={"approve": True}, headers=auth(sec)).status_code
        == 400
    )
    sec2 = make_token(rsa_key, sub="SEC002", roles=["security_reviewer"])
    r = client.post(
        "/skills/write_skill/review",
        json={"approve": True, "note": "复核通过"},
        headers=auth(sec2),
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
    """注册 → 提交（SEC-RV-*）→ 双人复核 → 安装 → 我的技能全流程。"""
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
    # security_reviewer 双人复核通过后安装（PRD 5.6.4）
    sec = make_token(rsa_key, roles=["security_reviewer"])
    r = client.post("/skills/sales_broadcast/review", json={"approve": True}, headers=auth(sec))
    assert r.status_code == 200
    sec2 = make_token(rsa_key, sub="SEC002", roles=["security_reviewer"])
    assert (
        client.post("/skills/sales_broadcast/review", json={"approve": True}, headers=auth(sec2)).status_code
        == 200
    )
    assert client.post("/skills/sales_broadcast/install", headers=auth(emp)).status_code == 200
    mine = client.get("/skills/mine", headers=auth(emp)).json()
    assert [i["name"] for i in mine["items"]] == ["sales_broadcast"]


# ---- 归类管理（P1-2.4 部门可见性：管理后台通用↔专属切换） ----


async def test_set_scope_toggles_visibility() -> None:
    """归类切换即时生效：通用→专属后他科室不可见、本科室可见；audit 落账。"""
    await store.set_scope("erp_po_sync", category="dept", dept_scope="计划科", by="E8001")
    meta = store.detail("erp_po_sync")
    assert meta["category"] == "dept" and meta["dept_scope"] == "计划科"
    assert not store.is_visible_to(meta, user_id="E1001", dept="事业部A/销售科")
    assert store.is_visible_to(meta, user_id="E2003", dept="事业部A/计划科")
    actions = [i["action"] for i in await audit.recent(10)]
    assert "skill_scope" in actions
    # 切回通用：全员可见恢复
    await store.set_scope("erp_po_sync", category="official", by="E8001")
    meta = store.detail("erp_po_sync")
    assert meta["category"] == "official" and meta["dept_scope"] is None
    assert store.is_visible_to(meta, user_id="E1001", dept="事业部A/销售科")
    assert meta["scope_overridden"] is True  # 显式覆盖标记（重启豁免基线同步）


async def test_set_scope_validation() -> None:
    """归类校验：专属缺科室/非法分类/未知技能/个人技能均拒绝。"""
    with pytest.raises(ValueError, match="归属科室"):
        await store.set_scope("erp_po_sync", category="dept", dept_scope="  ", by="E8001")
    with pytest.raises(ValueError, match="official"):
        await store.set_scope("erp_po_sync", category="team", by="E8001")
    with pytest.raises(ValueError, match="未注册"):
        await store.set_scope("no_such_skill", category="official", by="E8001")
    await store.register(
        name="my_tool", title="私人工具", rw="read", category="personal", by="E1001"
    )
    with pytest.raises(ValueError, match="个人"):
        await store.set_scope("my_tool", category="official", by="E8001")


async def test_rest_market_respects_scope_override(
    client: TestClient, rsa_key: Any
) -> None:
    """REST 全链路随归类生效：他科室列表不可见、详情 404、安装 403。"""
    await store.set_scope("erp_po_sync", category="dept", dept_scope="计划科", by="E8001")
    sales = make_token(rsa_key)
    names = {i["name"] for i in client.get("/skills", headers=auth(sales)).json()["items"]}
    assert "erp_po_sync" not in names
    assert client.get("/skills/erp_po_sync", headers=auth(sales)).status_code == 404
    assert client.post("/skills/erp_po_sync/install", headers=auth(sales)).status_code == 403
    plan = make_token(rsa_key, dept="事业部A/计划科")
    names = {i["name"] for i in client.get("/skills", headers=auth(plan)).json()["items"]}
    assert "erp_po_sync" in names


def _login_as_admin(client: TestClient, user: str, roles: list[str]) -> dict[str, Any]:
    """建管理会话并注入 cookie（口径同 test_models_registry.login_as）。"""
    from uuid import uuid4

    from agent_core.api import admin as admin_mod
    from agent_core.api.auth import AuthContext

    ctx = AuthContext(
        user_id=user,
        idp="ad",
        idp_sub=f"{user}@corp.com",
        dept="信息科",
        roles=list(roles),
        perm_ver=17,
        jti=uuid4().hex,
        session_id=f"web-{user}",
    )
    sess = admin_mod.create_session(ctx)
    client.cookies.set(admin_mod.SESSION_COOKIE, sess["sid"])
    return sess


def test_skill_scope_admin_page_gates(client: TestClient) -> None:
    """归类管理页门禁：未登录 302 / auditor 403 / org_admin 可切换 / system_admin 只读 / CSRF。"""
    # 未登录
    r = client.get("/admin/skills", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/admin/login")
    # auditor 403
    _login_as_admin(client, "aud1", ["auditor"])
    assert client.get("/admin/skills").status_code == 403
    client.cookies.clear()
    # system_admin 只读：可见列表但无切换表单
    _login_as_admin(client, "sa1", ["system_admin"])
    r = client.get("/admin/skills")
    assert r.status_code == 200 and "u8_gl_summary" in r.text
    assert "切换归类" not in r.text
    r = client.post(
        "/admin/skills/erp_po_sync/scope",
        data={"category": "official", "_csrf": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    client.cookies.clear()
    # org_admin：切换 + CSRF 拦截
    sess = _login_as_admin(client, "oa1", ["org_admin"])
    r = client.get("/admin/skills")
    assert r.status_code == 200 and "切换归类" in r.text
    r = client.post(
        "/admin/skills/erp_po_sync/scope",
        data={"category": "dept", "dept_scope": "计划科", "_csrf": "forged"},
        follow_redirects=False,
    )
    assert r.status_code == 302 and "CSRF" in r.headers["location"]
    r = client.post(
        "/admin/skills/erp_po_sync/scope",
        data={"category": "dept", "dept_scope": "计划科", "_csrf": sess["csrf"]},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert store.detail("erp_po_sync")["dept_scope"] == "计划科"
    # 专属缺科室 → 表单回跳报错
    r = client.post(
        "/admin/skills/erp_po_sync/scope",
        data={"category": "dept", "dept_scope": "", "_csrf": sess["csrf"]},
        follow_redirects=False,
    )
    assert "归属科室" in r.headers["location"] or "err=" in r.headers["location"]


# ---- 快照恢复与基线同步（registry SSOT：旧快照不吞代码基线） ----


async def test_restore_syncs_builtin_baseline() -> None:
    """旧快照不吞代码基线：restore 后归类以最新 registry 为准；
    生命周期状态（status/统计/评审单号）保留，新增内置技能补入，动态技能不动。"""
    stale = {
        "meta": {
            "u8_gl_summary": {  # 旧快照形态：归类修正前财务技能是「通用」
                "name": "u8_gl_summary",
                "title": "U8 总账余额表",
                "version": "1.0.0",
                "rw": "read",
                "category": "official",
                "dept_scope": None,
                "status": "deprecated",
                "approvals": [],
                "stats": {"calls": 5, "success": 3, "failed": 2},
            },
            "dyn_skill": {  # 动态注册技能（非内置）：以快照为准
                "name": "dyn_skill",
                "title": "动态技能",
                "version": "1.0.0",
                "rw": "read",
                "category": "dept",
                "dept_scope": "销售科",
                "status": "published",
                "approvals": [],
                "stats": {"calls": 0, "success": 0, "failed": 0},
            },
        },
        "seq": 3,
    }
    await persist.write_json("skill_store:snapshot", stale)  # conftest 已隔离到 tmp_path
    store._meta.clear()  # 模拟服务重启：进程内为空、从快照启动
    await store.restore()
    m = store.detail("u8_gl_summary")
    assert m is not None
    assert (m["category"], m["dept_scope"]) == ("dept", "财务科")  # 基线纠正旧快照
    assert m["status"] == "deprecated"  # 生命周期状态保留（曾被下架）
    assert m["stats"]["calls"] == 5  # 使用统计保留
    assert store.is_published("oa_leave_request")  # 快照缺失的内置技能按基线补入
    dyn = store.detail("dyn_skill")
    assert dyn is not None and dyn["dept_scope"] == "销售科"
    await store.register(name="w_after", title="重启后提单", rw="write", by="E1")
    meta = await store.submit("w_after", by="E1")
    assert meta["review_id"] == "SEC-RV-0004"  # 评审单号 seq 从快照恢复


async def test_restore_respects_scope_override() -> None:
    """管理员 set_scope 显式覆盖豁免基线同步：重启后保留管理员的归类。"""
    snap = {
        "meta": {
            "crm_customer_360": {  # 管理员曾把基线「销售科专属」切成通用
                "name": "crm_customer_360",
                "title": "CRM 客户 360 视图",
                "version": "1.0.0",
                "rw": "read",
                "category": "official",
                "dept_scope": None,
                "status": "published",
                "approvals": [],
                "scope_overridden": True,
                "stats": {"calls": 0, "success": 0, "failed": 0},
            }
        },
        "seq": 0,
    }
    await persist.write_json("skill_store:snapshot", snap)
    store._meta.clear()
    await store.restore()
    m = store.detail("crm_customer_360")
    assert m is not None
    assert (m["category"], m["dept_scope"]) == ("official", None)  # 管理员覆盖优先
    assert m["scope_overridden"] is True
