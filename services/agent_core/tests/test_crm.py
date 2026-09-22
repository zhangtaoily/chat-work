"""CRM/ERP/WMS 业务系统接入（PLAN P2.1，PRD 6.1）测试：

- CRM 订单录入：两轮收集（客户匹配→默认值链→联系人补问）→ HITL → 恢复提交
- 大额订单：合计 ≥ 5000 元审批流加销售总监；幂等重放返回 idempotent_reuse
- 多候选客户：列选项补问 → 全名点名唯一命中锁定 → 多联系人点名
- CRM 订单校验：明细缺失/数量无效/单价缺失/联系人不在册（纵深防御）
- 客户 360：唯一命中渲染档案；多候选列选项补问
- 跟单 / ERP 库缺凭证 / WMS 预警：只读工具编排与文本渲染
"""

import copy
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from agent_core.pipeline.graph import _validate_crm_order, build_graph, build_resume_graph
from agent_core.skills.registry import get_skill

_USER = "u-sales-001"


# ---- CRM mock 桩数据（与 mcp_crm/adapters/crm_client.py 口径一致）----

_HUACHEN = {
    "customer_id": "C-2001",
    "name": "华辰机械制造有限公司",
    "contacts": ["钱七", "孙八", "周九"],
    "default_address": "无锡市新吴区 XX 工业园 3 栋",
    "payment_term": "net30",
    "last_prices": {"SKU-C": 88.0},
    "credit_level": "B",
}

_XX_CUSTOMERS = [
    {"customer_id": "C-1001", "name": "XX 贸易（上海）有限公司"},
    {"customer_id": "C-1002", "name": "XX 实业（苏州）有限公司"},
    {"customer_id": "C-1003", "name": "XX 进出口（深圳）有限公司"},
]

_XX_360 = {
    "C-1002": {
        "customer_id": "C-1002",
        "name": "XX 实业（苏州）有限公司",
        "contacts": ["李四", "王五"],
        "default_address": "苏州工业园区 XX 路 6 号",
        "payment_term": "net60",
        "last_prices": {"SKU-A": 48.5},
        "credit_level": "B",
    },
}

_TRACK = {
    "order_no": "SO20260820007",
    "customer_name": "XX 实业（苏州）有限公司",
    "status": "delayed",
    "total_amount": 4400.0,
    "delivery_date": "2026-09-05",
    "progress": [
        {"node": "审批", "state": "done", "note": "2026-08-20 通过"},
        {"node": "仓库备货", "state": "doing", "note": "SKU-C 缺货，采购在途"},
    ],
    "exceptions": ["库存缺货：SKU-C 在途采购预计 09-25 到货，交期将延后"],
}

_SUBMIT_BASE = {
    "doc_no": "SO20260918001",
    "customer_name": "华辰机械制造有限公司",
    "status": "submitted",
    "message": "销售订单已录入 CRM 并提交审批",
}


def _crm_tool_stub(ledger: dict[str, bool]):
    """CRM 工具桩：搜索/360/提交（幂等记账）/跟单。"""

    def _stub(name: str, arguments: dict[str, Any]) -> Any:
        if name == "crm__search_customers":
            kw = arguments["keyword"]
            if "华辰" in kw:
                return [{"customer_id": _HUACHEN["customer_id"], "name": _HUACHEN["name"]}]
            if "实业" in kw:
                return [{"customer_id": "C-1002", "name": _XX_CUSTOMERS[1]["name"]}]
            if "XX" in kw:
                return copy.deepcopy(_XX_CUSTOMERS)
            return []
        if name == "crm__get_customer_360":
            cid = arguments["customer_id"]
            if cid == _HUACHEN["customer_id"]:
                return copy.deepcopy(_HUACHEN)
            if cid in _XX_360:
                return copy.deepcopy(_XX_360[cid])
            raise ValueError(f"客户不存在：{cid}")
        if name == "crm__submit_sales_order":
            key = arguments["idempotency_key"]
            if key in ledger:
                return {
                    **_SUBMIT_BASE,
                    "idempotent_reuse": True,
                    "message": "重复请求，返回首次提交结果",
                }
            ledger[key] = True
            total = sum(
                Decimal(str(it["qty"])) * Decimal(str(it["price"]))
                for it in arguments["items"]
            )
            # 大额订单审批流自动加销售总监节点（PRD 6.1.1）
            flow = ["直属主管", "销售总监"] if total >= 5000 else ["直属主管"]
            return {**_SUBMIT_BASE, "total_amount": float(total), "approval_flow": flow}
        if name == "crm__query_order_progress":
            return copy.deepcopy(_TRACK)
        raise AssertionError(f"未预期的 CRM 工具调用：{name}")

    return _stub


def _events_of(final_state: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    return [e for e in final_state.get("events", []) if e.get("type") == event_type]


# ---- CRM 销售订单录入（PRD 6.1.1）----


async def test_order_two_rounds_hitl_submit() -> None:
    """两轮收集：客户唯一命中 + 默认值链 → 联系人补问 → HITL → 恢复提交。"""
    session = "s-crm-2round"
    ledger: dict[str, bool] = {}
    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub(ledger)):
        graph = build_graph().compile()
        first = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "给华辰机械下销售订单 SKU-C x10"}
        )
    draft = first["draft"]
    # 客户唯一命中锁定；默认值链：单价取最近成交价 / 地址 / 付款方式默认主数据
    assert draft["customer_id"]["value"] == "C-2001"
    assert draft["items"]["value"] == [{"sku": "SKU-C", "qty": 10.0, "price": 88.0}]
    assert draft["payment_term"]["value"] == "net30"
    assert draft["delivery_date"]["source"] == "default"
    # 多联系人必问：补问文案列在册联系人
    assert first["final"]["text"] == (
        "请问订单联系人是谁？（需为该客户在册联系人）在册联系人：钱七、孙八、周九"
    )
    assert first.get("confirm_token") is None

    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub(ledger)):
        second = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "联系人钱七"}
        )
    # 裸回复点名联系人，不再被当作改选客户；齐备 → HITL 确认卡
    assert second["draft"]["contact"]["value"] == "钱七"
    confirms = _events_of(second, "confirm_card")
    assert len(confirms) == 1
    assert confirms[0]["payload"]["fields"]["客户"] == "华辰机械制造有限公司"
    assert confirms[0]["payload"]["fields"]["合计金额"] == "880.00 元"
    assert confirms[0]["payload"]["fields"]["商品明细"] == "SKU-C × 10 @ 88 = 880.00 元"

    state: dict[str, Any] = dict(second)
    state["confirmed"] = True
    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub(ledger)):
        final = await build_resume_graph().compile().ainvoke(state)
    assert "SO20260918001" in final["final"]["text"]
    assert "880.00" in final["final"]["text"]
    # 880 元 < 5000：审批流仅直属主管
    assert "直属主管" in final["final"]["text"]
    assert "销售总监" not in final["final"]["text"]


async def test_order_large_amount_director_and_idempotent_replay() -> None:
    """大额 ≥ 5000 元：审批流加销售总监；同幂等键重放 → idempotent_reuse。"""
    session = "s-crm-large"
    ledger: dict[str, bool] = {}
    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub(ledger)):
        graph = build_graph().compile()
        first = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "给华辰机械下销售订单 SKU-C x100"}
        )
    assert first.get("confirm_token") is None
    assert "在册联系人" in first["final"]["text"]
    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub(ledger)):
        second = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "联系人钱七"}
        )
    assert second.get("confirm_token") is not None
    state: dict[str, Any] = dict(second)
    state["confirmed"] = True
    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub(ledger)):
        resume = build_resume_graph().compile()
        final = await resume.ainvoke(state)
    # 88 × 100 = 8800 ≥ 5000：销售总监加签
    assert "审批流：直属主管 → 销售总监" in final["final"]["text"]

    # 同一状态重复恢复（同幂等键）：CRM 侧幂等命中，不重复提交
    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub(ledger)):
        replay = await resume.ainvoke(state)
    assert "幂等命中，未重复提交" in replay["final"]["text"]


async def test_order_multi_customer_candidates_then_pick() -> None:
    """多候选客户：列选项补问 → 全名点名唯一命中 → 多联系人点名 → HITL。"""
    session = "s-crm-candidates"
    ledger: dict[str, bool] = {}
    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub(ledger)):
        graph = build_graph().compile()
        first = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "给XX公司下销售订单 SKU-A x10"}
        )
    # 3 家候选列选项融入补问
    assert first["final"]["text"] == (
        "「XX公司」匹配到 3 家客户，请确认是哪家：\n"
        "- XX 贸易（上海）有限公司（C-1001）\n"
        "- XX 实业（苏州）有限公司（C-1002）\n"
        "- XX 进出口（深圳）有限公司（C-1003）"
    )

    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub(ledger)):
        second = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "XX 实业（苏州）有限公司"}
        )
    draft = second["draft"]
    assert draft["customer_id"]["value"] == "C-1002"
    # 默认值链：SKU-A 取最近成交价 48.5；多联系人列候选
    assert draft["items"]["value"] == [{"sku": "SKU-A", "qty": 10.0, "price": 48.5}]
    assert second["final"]["text"] == (
        "请问订单联系人是谁？（需为该客户在册联系人）在册联系人：李四、王五"
    )

    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub(ledger)):
        third = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "李四"}
        )
    assert third["draft"]["contact"]["value"] == "李四"
    assert third.get("confirm_token") is not None
    assert len(_events_of(third, "confirm_card")) == 1


def test_validate_crm_order_rules() -> None:
    """订单校验：明细为空 / 数量无效 / 单价缺失 / 联系人不在册（纵深防御）。"""
    skill = get_skill("crm_sales_order_entry")
    base = {
        "customer_id": {"value": "C-2001"},
        "contact": {"value": "钱七"},
        "items": {"value": [{"sku": "SKU-C", "qty": 10.0, "price": 88.0}]},
        "_crm_contacts": {"value": ["钱七", "孙八", "周九"]},
    }
    assert _validate_crm_order(copy.deepcopy(base), skill)["passed"] is True

    empty = {**base, "items": {"value": []}}
    v = _validate_crm_order(empty, skill)
    assert v["passed"] is False and "商品明细不能为空" in v["errors"]

    bad_qty = {**base, "items": {"value": [{"sku": "SKU-C", "qty": 0, "price": 88.0}]}}
    v = _validate_crm_order(bad_qty, skill)
    assert v["passed"] is False and any("数量无效" in e for e in v["errors"])

    no_price = {**base, "items": {"value": [{"sku": "SKU-A", "qty": 10.0, "price": None}]}}
    v = _validate_crm_order(no_price, skill)
    assert v["passed"] is False and any("请提供单价" in e for e in v["errors"])

    bad_contact = {**base, "contact": {"value": "路人甲"}}
    v = _validate_crm_order(bad_contact, skill)
    assert v["passed"] is False and "路人甲" in v["errors"][0]


async def test_order_customer_not_found_asks_again() -> None:
    """客户未命中：提示核对名称，不进 HITL。"""
    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub({})):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": "s-crm-notfound", "message": "给不存在客户下销售订单 SKU-C x10"}
        )
    # 桩搜索不命中（关键词不含 华辰/实业/XX）→ 未找到 → 核对提示
    assert "未找到客户" in final["final"]["text"] or "请核对" in final["final"]["text"]
    assert final.get("confirm_token") is None


# ---- 客户 360 / 跟单（PRD 6.1.2）----


async def test_customer_360_unique_hit() -> None:
    """客户 360：唯一命中渲染档案摘要（信用等级/联系人/最近成交价）。"""
    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub({})):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": "s-crm-360", "message": "查一下华辰机械的客户360"}
        )
    text = final["final"]["text"]
    assert "华辰机械制造有限公司（C-2001）" in text
    assert "信用等级：B" in text
    assert "钱七、孙八、周九" in text
    assert "SKU-C 88 元" in text


async def test_customer_360_multi_candidates() -> None:
    """客户 360 多候选：列选项补问，不渲染档案。"""
    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub({})):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": "s-crm-360-multi", "message": "查一下XX公司的客户360"}
        )
    text = final["final"]["text"]
    assert "匹配到 3 家客户" in text
    assert "C-1002" in text


async def test_order_track_progress_with_exception() -> None:
    """跟单进度：状态 + 节点推进 + 异常透出。"""
    with patch("agent_core.pipeline.graph.call_crm_tool", side_effect=_crm_tool_stub({})):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": "s-crm-track", "message": "查一下 SO20260820007 的跟单进度"}
        )
    text = final["final"]["text"]
    assert "SO20260820007" in text and "延期" in text
    assert "仓库备货" in text
    assert "交期将延后" in text


# ---- ERP 只读（PRD 6.1.3）----


_ERP_INVENTORY = [
    {
        "sku": "SKU-C",
        "sku_name": "商品 C",
        "warehouse": "总仓",
        "on_hand": 5,
        "available": 5,
        "inbound_transit": 500,
        "safety_stock": 50,
        "below_safety": True,
    }
]

_ERP_POS = [
    {
        "po_no": "PO20260910003",
        "sku": "SKU-C",
        "qty": 500,
        "supplier": "华源供应",
        "status": "in_transit",
        "eta": "2026-09-25",
        "amount": 44000.0,
    }
]

_ERP_VOUCHER = {
    "period": "2026-09",
    "voucher_count": 12,
    "debit_total": 128000.0,
    "credit_total": 128000.0,
    "balanced": True,
    "by_subject": [{"subject": "应收账款", "debit": 100000.0, "credit": 0.0}],
}


def _erp_tool_stub(name: str, arguments: dict[str, Any]) -> Any:
    if name == "erp__query_inventory":
        rows = copy.deepcopy(_ERP_INVENTORY)
        if not arguments.get("below_safety"):
            return rows
        return [r for r in rows if r["below_safety"]]
    if name == "erp__query_purchase_orders":
        pos = copy.deepcopy(_ERP_POS)
        status = arguments.get("status")
        return [p for p in pos if status is None or p["status"] == status]
    if name == "erp__query_voucher_summary":
        return copy.deepcopy(_ERP_VOUCHER)
    raise AssertionError(f"未预期的 ERP 工具调用：{name}")


async def test_erp_inventory_below_safety_filter() -> None:
    """库存查询：缺料告急 → 仅看低于安全库存行，缺料标记透出。"""
    with patch("agent_core.pipeline.graph.call_erp_tool", side_effect=_erp_tool_stub):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": "s-erp-inv", "message": "查一下 SKU-C 的库存，缺料告急"}
        )
    text = final["final"]["text"]
    assert "SKU-C 商品 C @总仓" in text
    assert "可用 5" in text and "在途 500" in text
    assert "低于安全库存" in text


async def test_erp_po_in_transit() -> None:
    """采购单：在途过滤 + ETA 透出。"""
    with patch("agent_core.pipeline.graph.call_erp_tool", side_effect=_erp_tool_stub):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": "s-erp-po", "message": "SKU-C 的在途采购单有哪些"}
        )
    text = final["final"]["text"]
    assert "PO20260910003" in text and "在途" in text
    assert "ETA 2026-09-25" in text


async def test_erp_voucher_summary_balanced() -> None:
    """凭证摘要：期间/张数/借贷平衡 + 科目明细。"""
    with patch("agent_core.pipeline.graph.call_erp_tool", side_effect=_erp_tool_stub):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": "s-erp-voucher", "message": "查一下 2026-09 的凭证摘要"}
        )
    text = final["final"]["text"]
    assert "期间 2026-09：凭证 12 张" in text
    assert "借贷平衡" in text
    assert "应收账款" in text


# ---- WMS 只读（PRD 6.1.4）----


_WMS_PAYLOAD = {
    "alerts": [
        {
            "alert_type": "low_stock",
            "sku": "SKU-C",
            "warehouse": "总仓",
            "detail": "现存 5 低于安全库存 50",
            "suggested_action": "建议加快 PO20260910003 入库",
        }
    ],
    "orders": [
        {
            "doc_no": "OUT20260820008",
            "order_type": "outbound",
            "sku": "SKU-C",
            "qty": 50,
            "partner": "XX 实业（苏州）有限公司",
            "status": "blocked",
            "ref_no": "SO20260820007",
        }
    ],
}


def _wms_tool_stub(name: str, arguments: dict[str, Any]) -> Any:
    if name == "wms__query_stock_alerts":
        sku = arguments.get("sku")
        return [a for a in copy.deepcopy(_WMS_PAYLOAD["alerts"]) if sku is None or a["sku"] == sku]
    if name == "wms__query_stock_orders":
        return copy.deepcopy(_WMS_PAYLOAD["orders"])
    raise AssertionError(f"未预期的 WMS 工具调用：{name}")


async def test_wms_stock_alert_combined() -> None:
    """WMS：预警 + 出库单合并查询（缺料跟单全景）。"""
    with patch("agent_core.pipeline.graph.call_wms_tool", side_effect=_wms_tool_stub):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": "s-wms", "message": "SKU-C 的出库单和库存预警"}
        )
    text = final["final"]["text"]
    assert "库存预警 1 条" in text
    assert "低库存" in text and "PO20260910003" in text
    assert "OUT20260820008" in text and "阻塞" in text
    assert "关联 SO20260820007" in text
