"""P3.1 MES / U8 接入测试（PLAN P3.1，PRD 7.1/8.2）：

流水线集成：两个只读技能（mes_production_report / u8_gl_summary）路由 →
抽取分流（工单号/凭证号直达明细，否则列表/余额表）→ MCP 工具编排 →
渲染。工具调用以 unittest.mock 拦截（服务端 mock 见 services/mcp_mes
与 services/mcp_u8 各自测试）。

路由避让口径：凭证号查询消息带「U8」且避开 erp_voucher_summary 的
短词（凭证/记账/借贷/账务），验证最长关键词路由不误路由。
"""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from agent_core import audit
from agent_core.pipeline.graph import build_graph

AUTH: dict[str, Any] = {
    "user_id": "E1001",
    "dept": "事业部A/销售科",
    "roles": ["employee"],
    "perm_ver": 17,
}

# 与 mcp_mes mock 对齐的工单/报工样本（断言渲染文案）
_WORK_ORDERS: list[dict[str, Any]] = [
    {
        "work_order": "MO20260902002",
        "sku": "SKU-A",
        "sku_name": "商品 A",
        "plan_qty": 300,
        "completed_qty": 260,
        "good_qty": 252,
        "ng_qty": 8,
        "status": "running",
        "workstation": "总装二线",
    },
    {
        "work_order": "MO20260901003",
        "sku": "SKU-B",
        "sku_name": "商品 B",
        "plan_qty": 200,
        "completed_qty": 200,
        "good_qty": 196,
        "ng_qty": 4,
        "status": "done",
        "workstation": "总装一线",
    },
]

_SKU_A_ORDERS: list[dict[str, Any]] = [
    r for r in _WORK_ORDERS if r["sku"] == "SKU-A"
]

_PRODUCTION_REPORTS: list[dict[str, Any]] = [
    {
        "report_no": "PR20260920001",
        "work_order": "MO20260902002",
        "operator": "王强",
        "report_time": "2026-09-20 16:30",
        "good_qty": 80,
        "ng_qty": 4,
        "work_hours": 8.0,
    },
    {
        "report_no": "PR20260921001",
        "work_order": "MO20260902002",
        "operator": "李敏",
        "report_time": "2026-09-21 16:30",
        "good_qty": 72,
        "ng_qty": 2,
        "work_hours": 8.0,
    },
]

# 与 mcp_u8 mock 对齐的余额表/凭证明细样本（服务端已重算合计与平衡）
_GL_BALANCE: dict[str, Any] = {
    "period": "2026-09",
    "rows": [
        {
            "subject": "银行存款",
            "direction": "debit",
            "opening": 800000.0,
            "debit": 1200000.0,
            "credit": 1500000.0,
            "closing": 500000.0,
        },
        {
            "subject": "应收账款",
            "direction": "debit",
            "opening": 350000.0,
            "debit": 600000.0,
            "credit": 300000.0,
            "closing": 650000.0,
        },
    ],
    "debit_total": 1800000.0,
    "credit_total": 1800000.0,
}

_VOUCHER_DETAIL: dict[str, Any] = {
    "voucher_no": "记-2026090128",
    "voucher_date": "2026-09-05",
    "summary": "收到客户货款（XX 贸易）",
    "entries": [
        {"subject": "银行存款", "direction": "debit", "amount": 300000.0},
        {"subject": "应收账款", "direction": "credit", "amount": 300000.0},
    ],
    "debit_total": 300000.0,
    "credit_total": 300000.0,
    "balanced": True,
}


@pytest.fixture(autouse=True)
def _clean_audit() -> Any:
    asyncio.run(audit.clear())
    yield
    asyncio.run(audit.clear())


async def _run(message: str, session: str) -> dict[str, Any]:
    graph = build_graph().compile()
    return await graph.ainvoke(
        {
            "user_id": AUTH["user_id"],
            "session_id": session,
            "message": message,
            "auth": AUTH,
        }
    )


async def test_pipeline_mes_work_orders() -> None:
    """「工单进度」：无单号 → 工单进度列表（MES_STATUS 中文渲染）。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return _WORK_ORDERS

    with patch("agent_core.pipeline.graph.call_mes_tool", side_effect=_spy):
        final = await _run("看下工单进度", "s-mes-wo")

    text = final["final"]["text"]
    assert "MO20260902002 商品 A" in text and "生产中" in text
    assert "MO20260901003" in text and "已完工" in text
    assert final["final"]["cards"] == [{"type": "work_orders", "rows": _WORK_ORDERS}]
    assert calls == [("mes__query_work_orders", {})]
    assert await audit.recent(action="permission_denied") == []


async def test_pipeline_mes_work_orders_sku_filter() -> None:
    """「SKU-A 工单进度」：SKU 可选过滤透传工具参数。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return _SKU_A_ORDERS

    with patch("agent_core.pipeline.graph.call_mes_tool", side_effect=_spy):
        final = await _run("看下 SKU-A 的工单进度", "s-mes-wo-sku")

    text = final["final"]["text"]
    assert "MO20260902002" in text
    assert "MO20260901003" not in text  # SKU-B 工单被过滤
    assert calls == [("mes__query_work_orders", {"sku": "SKU-A"})]


async def test_pipeline_mes_production_reports_by_order() -> None:
    """「MO 号报工记录」：工单号直达报工明细（操作工/工时渲染）。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return _PRODUCTION_REPORTS

    with patch("agent_core.pipeline.graph.call_mes_tool", side_effect=_spy):
        final = await _run("MO20260902002 的报工记录", "s-mes-report")

    text = final["final"]["text"]
    assert "王强" in text and "李敏" in text and "PR20260920001" in text
    assert final["final"]["cards"] == [
        {"type": "production_reports", "rows": _PRODUCTION_REPORTS}
    ]
    assert final["draft"]["work_order"]["value"] == "MO20260902002"
    assert calls == [
        ("mes__query_production_reports", {"work_order": "MO20260902002"})
    ]


async def test_pipeline_u8_gl_balance_default_period() -> None:
    """「本月总账」：科目余额表，期间缺省当月（computed 槽位）。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return _GL_BALANCE

    with patch("agent_core.pipeline.graph.call_u8_tool", side_effect=_spy):
        final = await _run("看下本月总账", "s-u8-gl")

    text = final["final"]["text"]
    assert "银行存款" in text and "U8 只读" in text
    assert "借 1,200,000.00" in text and "期末 500,000.00" in text
    assert final["final"]["cards"][0]["type"] == "u8_gl_balance"
    assert final["draft"]["period"]["source"] == "computed"
    assert calls == [
        ("u8__query_gl_balance", {"period": final["draft"]["period"]["value"]})
    ]


async def test_pipeline_u8_gl_balance_subject_and_period() -> None:
    """「2026-08 应收科目余额」：期间 + 科目口语关键词过滤。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return _GL_BALANCE

    with patch("agent_core.pipeline.graph.call_u8_tool", side_effect=_spy):
        final = await _run("看下 2026-08 应收的科目余额", "s-u8-gl-subject")

    text = final["final"]["text"]
    assert "应收账款" in text
    assert calls == [
        ("u8__query_gl_balance", {"period": "2026-08", "subject": "应收"})
    ]


async def test_pipeline_u8_voucher_detail() -> None:
    """「U8 凭证号明细」：凭证号直达凭证明细 + 借贷平衡渲染。

    消息带「U8」并避开 erp_voucher_summary 的短词（凭证/记账/借贷/账务），
    验证最长关键词路由归 u8_gl_summary。
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return _VOUCHER_DETAIL

    with patch("agent_core.pipeline.graph.call_u8_tool", side_effect=_spy):
        final = await _run("U8 查一下记-2026090128的明细", "s-u8-voucher")

    text = final["final"]["text"]
    assert "记-2026090128" in text and "收到客户货款" in text
    assert "借贷平衡" in text and "300,000.00" in text
    assert final["final"]["cards"][0]["type"] == "u8_voucher_detail"
    assert final["draft"]["voucher_no"]["value"] == "记-2026090128"
    assert calls == [("u8__query_voucher_detail", {"voucher_no": "记-2026090128"})]
