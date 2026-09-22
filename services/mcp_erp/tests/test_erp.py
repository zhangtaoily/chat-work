"""mcp-erp 单测：mock 适配器直测（库存/采购在途/凭证摘要借贷平衡）。

覆盖 PRD 6.1 ERP 优先场景：
- 库存查询（含缺料预警过滤，与 CRM 跟单「SKU-C 缺货」呼应）
- 采购订单查询（含在途，ETA 与 CRM 异常口径一致）
- 财务凭证摘要（按月，借贷平衡会计恒等式）
"""

import asyncio
from typing import Any

import pytest

from adapters.erp_client import MockErpAdapter, get_adapter
from tools.voucher import _PERIOD_PATTERN


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """每个测试独立：mock 模式 + 适配器单例重置。"""
    monkeypatch.setenv("ERP_MODE", "mock")
    from adapters import erp_client

    erp_client._adapter = None
    yield
    erp_client._adapter = None


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------- 库存查询 ----------

def test_inventory_by_sku() -> None:
    """按 SKU 精确查询：SKU-C 跨 2 仓（总仓 + 苏州仓）。"""
    rows = _run(get_adapter().query_inventory("SKU-C", None, False, 20))
    assert {r["warehouse"] for r in rows} == {"总仓", "苏州仓"}
    assert all(r["sku_name"] == "商品 C" for r in rows)


def test_inventory_below_safety_alerts() -> None:
    """缺料预警口径：仅返回低于安全库存的行（SKU-C 总仓 5<50、苏州仓 0<30）。"""
    rows = _run(get_adapter().query_inventory(None, None, True, 20))
    assert {(r["sku"], r["warehouse"]) for r in rows} == {
        ("SKU-C", "总仓"),
        ("SKU-C", "苏州仓"),
    }
    assert all(r["below_safety"] for r in rows)


def test_inventory_in_transit_visible() -> None:
    """采购在途量可见：SKU-C 总仓 in_transit=500（补货判断依据）。"""
    rows = _run(get_adapter().query_inventory("SKU-C", "总仓", False, 20))
    assert rows[0]["inbound_transit"] == 500


def test_inventory_keyword_warehouse() -> None:
    """按仓库名模糊：总仓 3 行（SKU-A/B/C）。"""
    rows = _run(get_adapter().query_inventory(None, "总仓", False, 20))
    assert len(rows) == 3


# ---------- 采购订单 ----------

def test_purchase_orders_in_transit_for_sku() -> None:
    """SKU-C 在途采购单：PO20260910003，500 件，ETA 09-25（与 CRM 异常口径一致）。"""
    rows = _run(get_adapter().query_purchase_orders("SKU-C", "in_transit", 20))
    assert len(rows) == 1
    po = rows[0]
    assert po["po_no"] == "PO20260910003"
    assert po["qty"] == 500
    assert po["eta"] == "2026-09-25"


def test_purchase_orders_by_status() -> None:
    """按状态过滤：received 命中 PO20260905011。"""
    rows = _run(get_adapter().query_purchase_orders(None, "received", 20))
    assert [r["po_no"] for r in rows] == ["PO20260905011"]


def test_purchase_orders_all() -> None:
    rows = _run(get_adapter().query_purchase_orders(None, None, 20))
    assert len(rows) == 2


# ---------- 财务凭证摘要 ----------

def test_voucher_summary_balanced_2026_09() -> None:
    """会计恒等式：借方合计 = 贷方合计 = 2,800,000；凭证 128 张。"""
    summary = _run(get_adapter().query_voucher_summary("2026-09"))
    assert summary["voucher_count"] == 128
    assert summary["balanced"] is True
    assert summary["debit_total"] == summary["credit_total"] == 2_800_000.0
    subjects = {s["subject"] for s in summary["by_subject"]}
    assert "主营业务收入" in subjects


def test_voucher_summary_2026_08() -> None:
    summary = _run(get_adapter().query_voucher_summary("2026-08"))
    assert summary["voucher_count"] == 96
    assert summary["balanced"] is True
    assert summary["debit_total"] == 1_100_000.0


def test_voucher_summary_unknown_period() -> None:
    with pytest.raises(ValueError, match="无 2025-01 期间凭证摘要"):
        _run(get_adapter().query_voucher_summary("2025-01"))


def test_voucher_period_pattern() -> None:
    """期间格式校验：YYYY-MM 且月份 01-12。"""
    assert _PERIOD_PATTERN.match("2026-09") is not None
    assert _PERIOD_PATTERN.match("2026-13") is None
    assert _PERIOD_PATTERN.match("202609") is None
    assert _PERIOD_PATTERN.match("2026-9") is None


def test_mock_adapter_type_exported() -> None:
    """MockErpAdapter 可直接实例化（供上游集成测试复用口径）。"""
    adapter = MockErpAdapter()
    assert asyncio.run(adapter.query_inventory(None, None, True, 20))
