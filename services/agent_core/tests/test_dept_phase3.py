"""科室扩展测试·第三批（PLAN P3.2，PRD 7.2）：

剩余五科室只读技能（人力资源/企管/法务/技术/产品）：
- 本科室放行：双工具编排（或进程内 audit.recent 直调）+ 渲染
- 跨科室拒绝：permission 节点前置拦截——拒绝落审计、不触达工具
- 技能市场联动：registry 新增技能自动上架（store._ensure 内置基线）
"""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from agent_core import audit
from agent_core.pipeline.graph import build_graph
from agent_core.skills import registry, store

# 种子用户与 infra/keycloak/realm-export.json E2006-E2010 对齐
AUTH_HR: dict[str, Any] = {
    "user_id": "E2006",
    "dept": "事业部A/人力资源科",
    "roles": ["employee"],
    "perm_ver": 17,
}
AUTH_MGMT: dict[str, Any] = {
    "user_id": "E2007",
    "dept": "事业部A/企管科",
    "roles": ["employee"],
    "perm_ver": 17,
}
AUTH_LEGAL: dict[str, Any] = {
    "user_id": "E2008",
    "dept": "事业部A/法务科",
    "roles": ["employee"],
    "perm_ver": 17,
}
AUTH_TECH: dict[str, Any] = {
    "user_id": "E2009",
    "dept": "事业部A/技术科",
    "roles": ["employee"],
    "perm_ver": 17,
}
AUTH_PRODUCT: dict[str, Any] = {
    "user_id": "E2010",
    "dept": "事业部A/产品科",
    "roles": ["employee"],
    "perm_ver": 17,
}
# 跨科室访问者（销售科）
AUTH_SALES: dict[str, Any] = {
    "user_id": "E1001",
    "dept": "事业部A/销售科",
    "roles": ["employee"],
    "perm_ver": 17,
}
# 跨科室访问者（信息科，与 test_dept.py 同源）
AUTH_IT: dict[str, Any] = {
    "user_id": "E2001",
    "dept": "事业部A/信息科",
    "roles": ["employee"],
    "perm_ver": 17,
}

_ACTIVITY_ROWS: list[dict[str, Any]] = [
    {
        "user_id": "E2006",
        "action_type": "leave",
        "summary": "提交请假申请（年假 1 天）",
        "occurred_at": "2026-09-22T09:30:00",
    },
    {
        "user_id": "E2006",
        "action_type": "approval",
        "summary": "审批通过请假单 QJ20260921001",
        "occurred_at": "2026-09-21T14:00:00",
    },
]

_BI_RESULT: dict[str, Any] = {
    "query": "本月销售额",
    "kpi": {"label": "销售额", "value": 120000.0, "period": "2026-09", "region": "集团"},
    "mom_pct": 5.2,
}

_BI_PRODUCT_RESULT: dict[str, Any] = {
    "query": "本月销售额",
    "kpi": {"label": "销售额", "value": 1500000.0, "period": "2026-09", "region": "集团"},
    "dimension": "产品线",
    "series": [
        {"label": "智能门禁", "value": 600000.0},
        {"label": "协同办公", "value": 500000.0},
        {"label": "数据服务", "value": 400000.0},
    ],
    "mom_pct": None,
}

_VOUCHER_RESULT: dict[str, Any] = {
    "period": "2026-09",
    "voucher_count": 42,
    "debit_total": 88000.0,
    "credit_total": 88000.0,
    "balanced": True,
    "by_subject": [],
}

_INVENTORY_ROWS: list[dict[str, Any]] = [
    {
        "sku": "SKU-C",
        "sku_name": "备件 C",
        "warehouse": "总仓",
        "available": 5,
        "on_hand": 6,
        "inbound_transit": 500,
        "safety_stock": 50,
        "below_safety": True,
    }
]

_ALERT_ROWS: list[dict[str, Any]] = [
    {
        "alert_type": "expiry",
        "sku": "SKU-B",
        "warehouse": "总仓",
        "detail": "批次 B20260130 共 80 件，效期至 2026-10-15（30 天内到期）",
        "suggested_action": "优先出库临期批次（FEFO）",
    }
]

_CUSTOMER_360: dict[str, Any] = {
    "customer_id": "C-2001",
    "name": "华辰机械制造有限公司",
    "contacts": ["钱七", "孙八", "周九"],
    "credit_level": "B",
    "orders": [],
}


@pytest.fixture(autouse=True)
def _clean_audit() -> Any:
    asyncio.run(audit.clear())
    yield
    asyncio.run(audit.clear())


async def _run(message: str, auth: dict[str, Any], session: str) -> dict[str, Any]:
    graph = build_graph().compile()
    return await graph.ainvoke(
        {
            "user_id": auth["user_id"],
            "session_id": session,
            "message": message,
            "auth": auth,
        }
    )


# ---- 技能市场联动：新技能自动上架（store._ensure 内置基线）----


def test_phase3_skills_published() -> None:
    """P3.2 五技能：dept_scope 元数据齐备且在技能市场自动 published。"""
    for name in ("hr_roster", "mgmt_overview", "audit_trace", "tech_inventory", "product_sales"):
        skill = registry.SKILLS[name]
        assert skill["dept_scope"], name
        assert skill["rw"] == "read" and skill["mode"] == "ask", name
        assert store.is_published(name), name


# ---- 人力资源科：人力速览（OA 动态）----


async def test_hr_roster_same_dept() -> None:
    """人力资源科「员工动态」：本人近 7 天 OA 操作记录。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return _ACTIVITY_ROWS

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_spy):
        final = await _run("看下员工动态", AUTH_HR, "s-p3-hr")

    text = final["final"]["text"]
    assert "近 7 天 OA 员工动态" in text
    assert "提交请假申请" in text
    assert final["tool_result"] == _ACTIVITY_ROWS
    # 用户标识恒取会话 user_id（不采信入参）
    assert ("oa__query_activity_log", {"user_id": "E2006", "days": 7}) in calls
    assert await audit.recent(action="permission_denied") == []


async def test_hr_roster_cross_dept_denied() -> None:
    """销售科问员工动态 → 组织维拦截：不触达工具、落 permission_denied 审计。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return []

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_spy):
        final = await _run("看下员工动态", AUTH_SALES, "s-p3-hr-deny")

    assert "人力资源科" in final["final"]["text"]
    assert final["tool_result"] is None
    assert calls == []

    denied = await audit.recent(action="permission_denied")
    assert len(denied) == 1
    assert denied[0]["tool"] == "oa__query_activity_log"  # read_tools[0]
    assert denied[0]["user_id"] == "E1001"


# ---- 企管科：管理速览（BI + 凭证平衡）----


async def test_mgmt_overview_same_dept() -> None:
    """企管科「经营概览」：BI 当月 KPI + 凭证平衡双指标。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return {"bi__execute_query": _BI_RESULT}.get(name, _VOUCHER_RESULT)

    with (
        patch("agent_core.pipeline.graph.call_bi_tool", side_effect=_spy),
        patch("agent_core.pipeline.graph.call_erp_tool", side_effect=_spy),
    ):
        final = await _run("看下经营概览", AUTH_MGMT, "s-p3-mgmt")

    text = final["final"]["text"]
    assert "BI：集团 2026-09 销售额：120,000.00 元" in text
    assert "凭证 2026-09：42 张" in text and "借贷平衡" in text
    assert ("bi__execute_query", {"query": "本月销售额"}) in calls
    assert await audit.recent(action="permission_denied") == []


# ---- 法务科：审计流水（进程内 audit.recent 直调）----


async def test_audit_trace_same_dept_own_rows_only() -> None:
    """法务科「审计流水」：仅本人留痕（D3 敏感），他人记录不可见。"""
    await audit.record(
        "tool_call",
        user_id="E2008",
        session_id="s-p3-legal-seed",
        tool="oa__submit_leave_request",
        params={"leave_type": "annual"},
        result="ok",
    )
    await audit.record(
        "tool_call",
        user_id="E9999",
        session_id="s-p3-legal-other",
        tool="crm__submit_sales_order",
        params={"customer_id": "C-1001"},
        result="ok",
    )

    final = await _run("查下我的审计流水", AUTH_LEGAL, "s-p3-legal")

    text = final["final"]["text"]
    assert "操作留痕" in text
    assert "oa__submit_leave_request" in text
    # 他人记录不可见（仅本人范围）
    assert "crm__submit_sales_order" not in text
    rows = final["tool_result"]["rows"]
    assert all(r["user_id"] == "E2008" for r in rows)
    assert await audit.recent(action="permission_denied") == []


async def test_audit_trace_cross_dept_denied() -> None:
    """信息科问审计流水 → 拒绝；read_tools 为空时审计 tool 落 None 不报错。"""
    final = await _run("查下我的审计流水", AUTH_IT, "s-p3-legal-deny")

    assert "法务科" in final["final"]["text"]
    assert final["tool_result"] is None

    denied = await audit.recent(action="permission_denied")
    assert len(denied) == 1
    assert denied[0]["tool"] is None  # read_tools 空 → None（不崩溃）
    assert denied[0]["user_id"] == "E2001"


# ---- 技术科：技术备件巡检（库存 + 预警）----


async def test_tech_inventory_same_dept() -> None:
    """技术科「备件巡检」：备件库存水位 + 待处置预警。"""
    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        return {"erp__query_inventory": _INVENTORY_ROWS}.get(name, _ALERT_ROWS)

    with (
        patch("agent_core.pipeline.graph.call_erp_tool", side_effect=_spy),
        patch("agent_core.pipeline.graph.call_wms_tool", side_effect=_spy),
    ):
        final = await _run("跑一次备件巡检", AUTH_TECH, "s-p3-tech")

    text = final["final"]["text"]
    assert "在库 SKU 1 个，低于安全库存 1 个" in text
    assert "待处置预警" in text
    assert final["tool_result"]["inventory"] == _INVENTORY_ROWS
    assert final["tool_result"]["alerts"] == _ALERT_ROWS
    assert await audit.recent(action="permission_denied") == []


async def test_tech_inventory_cross_dept_denied() -> None:
    """销售科问备件 → 组织维拦截。"""
    with patch("agent_core.pipeline.graph.call_erp_tool", side_effect=lambda n, a: []):
        final = await _run("跑一次备件巡检", AUTH_SALES, "s-p3-tech-deny")

    assert "技术科" in final["final"]["text"]
    assert final["tool_result"] is None
    assert len(await audit.recent(action="permission_denied")) == 1


# ---- 产品科：产品销售看板（BI 拆分 + 可选客户 360）----


async def test_product_sales_same_dept() -> None:
    """产品科「产品看板」：BI 产品线拆分序列。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return _BI_PRODUCT_RESULT

    with patch("agent_core.pipeline.graph.call_bi_tool", side_effect=_spy):
        final = await _run("看下产品销售看板", AUTH_PRODUCT, "s-p3-prod")

    text = final["final"]["text"]
    assert "2026-09产品线销售额" in text
    assert "智能门禁：600,000.00 元" in text
    assert "数据服务：400,000.00 元" in text
    # 拆分口径固定按产品线
    assert ("bi__execute_query", {"query": "本月销售额", "dimensions": ["product"]}) in calls
    assert await audit.recent(action="permission_denied") == []


async def test_product_sales_with_customer_360() -> None:
    """产品看板 + 显式客户句式 → 附客户 360 摘要（单命中直达）。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        if name == "crm__search_customers":
            return [{"customer_id": "C-2001", "name": "华辰机械制造有限公司"}]
        if name == "crm__get_customer_360":
            return _CUSTOMER_360
        return _BI_PRODUCT_RESULT

    with (
        patch("agent_core.pipeline.graph.call_bi_tool", side_effect=_spy),
        patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_spy),
    ):
        final = await _run("看下产品看板 查华辰机械的资料", AUTH_PRODUCT, "s-p3-prod-crm")

    text = final["final"]["text"]
    assert "产品线销售额" in text
    assert "头部客户：华辰机械制造有限公司" in text
    assert ("crm__search_customers", {"keyword": "华辰机械"}) in calls
    assert ("crm__get_customer_360", {"customer_id": "C-2001"}) in calls


async def test_product_sales_cross_dept_denied() -> None:
    """销售科问产品看板 → 组织维拦截（产品科视图不跨科室开放）。"""
    with patch("agent_core.pipeline.graph.call_bi_tool", side_effect=lambda n, a: {}):
        final = await _run("看下产品销售看板", AUTH_SALES, "s-p3-prod-deny")

    assert "产品科" in final["final"]["text"]
    assert final["tool_result"] is None
    assert len(await audit.recent(action="permission_denied")) == 1
