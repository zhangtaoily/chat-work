"""科室扩展测试（PLAN P2.2，PRD 2.3/6.2）：

- permissions.check_dept_scope：数据级·组织维「本科室」门禁
  （auth.dept 尾段与技能 dept_scope 匹配才放行；跨科室拒绝；auth 缺省放行）
- 流水线集成：五科室只读技能（信息/生产/计划/品管/仓库）双工具编排 + 渲染；
  跨科室访问被 permission 节点前置拦截——拒绝落审计、不触达工具
"""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from agent_core import audit
from agent_core.pipeline import permissions
from agent_core.pipeline.graph import build_graph

# 种子用户与 infra/keycloak/realm-export.json E2001-E2005 对齐
AUTH_IT: dict[str, Any] = {
    "user_id": "E2001",
    "dept": "事业部A/信息科",
    "roles": ["employee"],
    "perm_ver": 17,
}
AUTH_PROD: dict[str, Any] = {
    "user_id": "E2002",
    "dept": "事业部A/生产科",
    "roles": ["employee"],
    "perm_ver": 17,
}
AUTH_PLAN: dict[str, Any] = {
    "user_id": "E2003",
    "dept": "事业部A/计划科",
    "roles": ["employee"],
    "perm_ver": 17,
}
AUTH_QA: dict[str, Any] = {
    "user_id": "E2004",
    "dept": "事业部A/品管科",
    "roles": ["employee"],
    "perm_ver": 17,
}
AUTH_WH: dict[str, Any] = {
    "user_id": "E2005",
    "dept": "事业部A/仓库物流",
    "roles": ["employee"],
    "perm_ver": 17,
}
# 跨科室访问者（销售科，与 test_permissions 同源）
AUTH_SALES: dict[str, Any] = {
    "user_id": "E1001",
    "dept": "事业部A/销售科",
    "roles": ["employee"],
    "perm_ver": 17,
}

_INVENTORY_ROWS: list[dict[str, Any]] = [
    {
        "sku": "SKU-C",
        "sku_name": "商品 C",
        "warehouse": "总仓",
        "available": 5,
        "on_hand": 6,
        "inbound_transit": 500,
        "safety_stock": 50,
        "below_safety": True,
    }
]

_OUTBOUND_ORDERS: list[dict[str, Any]] = [
    {
        "doc_no": "OUT20260901002",
        "order_type": "outbound",
        "sku": "SKU-A",
        "qty": 200,
        "partner": "XX 贸易（上海）有限公司",
        "ref_no": "SO20260901001",
        "doc_date": "2026-09-01",
        "status": "pending",
    }
]

_INBOUND_ORDERS: list[dict[str, Any]] = [
    {
        "doc_no": "IN20260905012",
        "order_type": "inbound",
        "sku": "SKU-B",
        "qty": 200,
        "partner": "华南五金贸易有限公司",
        "ref_no": "PO20260905011",
        "doc_date": "2026-09-05",
        "status": "done",
    }
]

_EXPIRY_ALERTS: list[dict[str, Any]] = [
    {
        "alert_type": "expiry",
        "sku": "SKU-B",
        "warehouse": "总仓",
        "detail": "批次 B20260130 共 80 件，效期至 2026-10-15（30 天内到期）",
        "suggested_action": "优先出库临期批次（FEFO）",
    }
]

_IN_TRANSIT_POS: list[dict[str, Any]] = [
    {
        "po_no": "PO20260905011",
        "sku": "SKU-B",
        "qty": 500,
        "supplier": "华南五金贸易有限公司",
        "status": "in_transit",
        "eta": "2026-09-25",
        "amount": 25000.0,
    }
]

_BI_RESULT: dict[str, Any] = {
    "query": "本月销售额",
    "kpi": {"label": "销售额", "value": 120000.0, "period": "2026-09", "region": "集团"},
    "mom_pct": 5.2,
}

_VOUCHER_RESULT: dict[str, Any] = {
    "period": "2026-09",
    "voucher_count": 42,
    "debit_total": 88000.0,
    "credit_total": 88000.0,
    "balanced": True,
    "by_subject": [],
}


@pytest.fixture(autouse=True)
def _clean_audit() -> Any:
    asyncio.run(audit.clear())
    yield
    asyncio.run(audit.clear())


# ---- 单元：check_dept_scope（数据级·组织维）----


def test_dept_scope_none_auth_passes() -> None:
    """auth 缺省（本地冒烟无 token）放行。"""
    skill = {"dept_scope": "生产科"}
    assert permissions.check_dept_scope(None, skill) is None


def test_dept_scope_skill_without_scope_passes() -> None:
    """无 dept_scope 元数据的技能不受组织维约束。"""
    assert permissions.check_dept_scope(AUTH_SALES, {}) is None


def test_dept_scope_same_dept_tail_passes() -> None:
    """本科室放行：dept 尾段匹配（事业部前缀不影响）。"""
    skill = {"dept_scope": "生产科"}
    assert permissions.check_dept_scope(AUTH_PROD, skill) is None


def test_dept_scope_cross_dept_denied() -> None:
    """跨科室拒绝：文案含目标科室与当前科室，不暴露权限实现细节。"""
    message = permissions.check_dept_scope(AUTH_SALES, {"dept_scope": "生产科"})
    assert message is not None
    assert "生产科" in message
    assert "销售科" in message


def test_dept_scope_missing_dept_denied() -> None:
    """auth 有值但无 dept（未设置）→ 拒绝并提示。"""
    message = permissions.check_dept_scope({"user_id": "E1001"}, {"dept_scope": "生产科"})
    assert message is not None
    assert "未设置" in message


# ---- 流水线集成：五科室只读技能 ----


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


async def test_pipeline_prod_material_check_same_dept() -> None:
    """生产科「生产缺料」：路由到科室技能，缺料清单 + 出库单合并返回。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return {"erp__query_inventory": _INVENTORY_ROWS}.get(name, _OUTBOUND_ORDERS)

    with (
        patch("agent_core.pipeline.graph.call_erp_tool", side_effect=_spy),
        patch("agent_core.pipeline.graph.call_wms_tool", side_effect=_spy),
    ):
        final = await _run("帮我看下生产缺料情况", AUTH_PROD, "s-dept-prod")

    text = final["final"]["text"]
    assert "缺料清单" in text and "SKU-C" in text
    assert "近期出库单" in text
    assert final["tool_result"]["inventory"] == _INVENTORY_ROWS
    assert final["tool_result"]["orders"] == _OUTBOUND_ORDERS
    # 缺料口径固定 below_safety=True
    assert ("erp__query_inventory", {"below_safety": True}) in calls
    assert await audit.recent(action="permission_denied") == []


async def test_pipeline_qa_batch_trace_same_dept() -> None:
    """品管科「效期预警」：最长关键词路由到科室技能（非 wms_stock_alert）。"""
    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        return {"wms__query_stock_alerts": _EXPIRY_ALERTS}.get(name, _INBOUND_ORDERS)

    with (
        patch("agent_core.pipeline.graph.call_wms_tool", side_effect=_spy),
        patch("agent_core.pipeline.graph.call_erp_tool", side_effect=_spy),
    ):
        final = await _run("查下效期预警", AUTH_QA, "s-dept-qa")

    text = final["final"]["text"]
    assert "效期/库存预警" in text and "批次 B20260130" in text
    assert "近期入库单" in text
    assert final["tool_result"]["alerts"] == _EXPIRY_ALERTS
    assert await audit.recent(action="permission_denied") == []


async def test_pipeline_qa_batch_trace_cross_dept_denied() -> None:
    """销售科问效期 → 组织维前置拦截：不触达工具、落 permission_denied 审计。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return []

    with (
        patch("agent_core.pipeline.graph.call_wms_tool", side_effect=_spy),
        patch("agent_core.pipeline.graph.call_erp_tool", side_effect=_spy),
    ):
        final = await _run("查下效期预警", AUTH_SALES, "s-dept-qa-deny")

    assert final["final"]["text"] == (
        "暂无「品管科」的数据权限（当前科室：事业部A/销售科），请联系管理员开通。"
    )
    assert final["tool_result"] is None
    assert calls == []  # 请求未发往 MCP

    denied = await audit.recent(action="permission_denied")
    assert len(denied) == 1
    assert denied[0]["tool"] == "wms__query_stock_alerts"  # read_tools[0]
    assert "qa_batch_trace" in denied[0]["params_digest"]
    assert denied[0]["user_id"] == "E1001"
    assert "品管科" in denied[0]["detail"]


async def test_pipeline_plan_inbound_view_with_sku() -> None:
    """计划科「到货计划 SKU-B」：在途采购单 + 指定 SKU 库存。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return {"erp__query_purchase_orders": _IN_TRANSIT_POS}.get(
            name, _INVENTORY_ROWS
        )

    with patch("agent_core.pipeline.graph.call_erp_tool", side_effect=_spy):
        final = await _run("看下到货计划 SKU-B", AUTH_PLAN, "s-dept-plan")

    assert "在途采购单" in final["final"]["text"]
    assert "PO20260905011" in final["final"]["text"]
    assert ("erp__query_purchase_orders", {"status": "in_transit", "sku": "SKU-B"}) in calls
    assert final["tool_result"]["pos"] == _IN_TRANSIT_POS


async def test_pipeline_wh_stock_overview() -> None:
    """仓库物流科「仓库概览」：库存水位汇总 + 待处置预警。"""
    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        return {"erp__query_inventory": _INVENTORY_ROWS}.get(name, _EXPIRY_ALERTS)

    with (
        patch("agent_core.pipeline.graph.call_erp_tool", side_effect=_spy),
        patch("agent_core.pipeline.graph.call_wms_tool", side_effect=_spy),
    ):
        final = await _run("看下仓库概览", AUTH_WH, "s-dept-wh")

    text = final["final"]["text"]
    assert "在库 SKU 1 个，低于安全库存 1 个" in text
    assert "待处置预警" in text
    assert final["tool_result"]["alerts"] == _EXPIRY_ALERTS


async def test_pipeline_it_data_check_period_default() -> None:
    """信息科「数据巡检」：BI 快照 + 凭证平衡核查（期间缺省当月）。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return {"bi__execute_query": _BI_RESULT}.get(name, _VOUCHER_RESULT)

    with (
        patch("agent_core.pipeline.graph.call_bi_tool", side_effect=_spy),
        patch("agent_core.pipeline.graph.call_erp_tool", side_effect=_spy),
    ):
        final = await _run("跑一次数据巡检", AUTH_IT, "s-dept-it")

    text = final["final"]["text"]
    assert "BI：集团 2026-09 销售额：120,000.00 元，环比+5.2%" in text
    assert "凭证 2026-09：42 张" in text and "借贷平衡" in text
    assert ("bi__execute_query", {"query": "本月销售额"}) in calls
    assert ("erp__query_voucher_summary", {"period": final["draft"]["period"]["value"]}) in calls
