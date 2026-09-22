"""mcp-wms 单测：mock 适配器直测（出入库单查询/日期窗口/库存预警）。

覆盖 PRD 6.1 WMS 优先场景：
- 入库/出库单查询（含关联单据：采购单/销售订单）
- 库存预警（缺料 low_stock / 效期 expiry，与 CRM/ERP mock 数据呼应）
"""

import asyncio
from typing import Any

import pytest

from adapters.wms_client import MockWmsAdapter, get_adapter
from tools.stock_orders import query_stock_orders


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """每个测试独立：mock 模式 + 适配器单例重置。"""
    monkeypatch.setenv("WMS_MODE", "mock")
    import adapters.wms_client as wms_client

    wms_client._adapter = None
    yield
    wms_client._adapter = None


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------- 出入库单查询 ----------

def test_query_inbound_orders() -> None:
    """入库单：IN20260905012 关联采购单 PO20260905011（与 ERP 呼应）。"""
    rows = _run(get_adapter().query_stock_orders("inbound", None, 365, 20))
    assert len(rows) == 2
    by_no = {r["doc_no"]: r for r in rows}
    assert by_no["IN20260905012"]["ref_no"] == "PO20260905011"
    assert by_no["IN20260905012"]["qty"] == 200
    assert by_no["IN20260905012"]["status"] == "done"


def test_query_outbound_orders_blocked() -> None:
    """出库单：OUT20260820008 因 SKU-C 缺货阻塞（与 CRM 跟单异常呼应）。"""
    rows = _run(get_adapter().query_stock_orders("outbound", None, 365, 20))
    assert len(rows) == 2
    by_no = {r["doc_no"]: r for r in rows}
    assert by_no["OUT20260820008"]["status"] == "blocked"
    assert by_no["OUT20260820008"]["ref_no"] == "SO20260820007"
    assert by_no["OUT20260901002"]["status"] == "pending"


def test_query_orders_date_window() -> None:
    """日期窗口：days=1 时历史单全部排除（窗口边界不依赖运行日期）。"""
    rows = _run(get_adapter().query_stock_orders("inbound", None, 1, 20))
    assert rows == []


def test_query_orders_keyword_partner() -> None:
    """关键词命中往来方/单号/品名。"""
    rows = _run(get_adapter().query_stock_orders("outbound", "XX 实业", 365, 20))
    assert [r["doc_no"] for r in rows] == ["OUT20260820008"]
    rows = _run(get_adapter().query_stock_orders("outbound", "商品 A", 365, 20))
    assert [r["doc_no"] for r in rows] == ["OUT20260901002"]


def test_query_orders_invalid_type() -> None:
    """工具层枚举校验：order_type 必须为 inbound/outbound。"""
    with pytest.raises(ValueError, match="无效单据类型"):
        _run(query_stock_orders("transfer", None, 30, 20))


# ---------- 库存预警 ----------

def test_alerts_all() -> None:
    """全量预警：2 条缺料 + 1 条效期。"""
    rows = _run(get_adapter().query_stock_alerts(None))
    types = [r["alert_type"] for r in rows]
    assert types.count("low_stock") == 2
    assert types.count("expiry") == 1


def test_alerts_by_sku_low_stock() -> None:
    """SKU-C 缺料预警：总仓 + 苏州仓，含在途补货建议（ETA 09-25）。"""
    rows = _run(get_adapter().query_stock_alerts("SKU-C"))
    assert len(rows) == 2
    assert all(r["alert_type"] == "low_stock" for r in rows)
    assert "2026-09-25" in rows[0]["detail"]


def test_alerts_by_sku_empty() -> None:
    """SKU-A 无预警。"""
    rows = _run(get_adapter().query_stock_alerts("SKU-A"))
    assert rows == []


def test_mock_adapter_type_exported() -> None:
    """MockWmsAdapter 可直接实例化（供上游集成测试复用口径）。"""
    adapter = MockWmsAdapter()
    assert asyncio.run(adapter.query_stock_alerts(None))
