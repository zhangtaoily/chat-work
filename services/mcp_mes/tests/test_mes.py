"""mcp-mes 单测：mock 适配器直测（工单进度/报工明细）。

覆盖 P3.1 MES 场景（PLAN P3.1，PRD 7.1）：
- 工单进度查询（SKU/状态过滤；SKU-C 等料未开工与 ERP 缺料呼应）
- 报工明细查询（按工单；未知工单报错）
"""

import asyncio
from typing import Any

import pytest

from adapters.mes_client import MockMesAdapter, get_adapter


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """每个测试独立：mock 模式 + 适配器单例重置。"""
    monkeypatch.setenv("MES_MODE", "mock")
    from adapters import mes_client

    mes_client._adapter = None
    yield
    mes_client._adapter = None


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------- 工单进度 ----------

def test_work_orders_all() -> None:
    """全量工单：4 张（含 SKU-C 等料未开工）。"""
    rows = _run(get_adapter().query_work_orders(None, None, 20))
    assert len(rows) == 4
    assert {r["status"] for r in rows} == {"pending", "running", "done", "closed"}


def test_work_orders_by_sku() -> None:
    """按 SKU 过滤：SKU-A 2 张（running + closed）。"""
    rows = _run(get_adapter().query_work_orders("SKU-A", None, 20))
    assert {r["work_order"] for r in rows} == {"MO20260902002", "MO20260801004"}


def test_work_orders_running() -> None:
    """状态过滤：running 命中 MO20260902002（完工 260/300，良品 252）。"""
    rows = _run(get_adapter().query_work_orders(None, "running", 20))
    assert len(rows) == 1
    wo = rows[0]
    assert wo["work_order"] == "MO20260902002"
    assert wo["completed_qty"] == 260
    assert wo["good_qty"] == 252


def test_work_orders_sku_c_pending_waiting_material() -> None:
    """SKU-C 等料工单：未开工、完工 0（与 ERP 缺料/在途 PO20260910003 呼应）。"""
    rows = _run(get_adapter().query_work_orders("SKU-C", None, 20))
    assert len(rows) == 1
    wo = rows[0]
    assert wo["status"] == "pending"
    assert wo["completed_qty"] == 0
    assert wo["plan_start"] == "2026-09-26"  # 晚于在途 ETA 09-25


def test_work_orders_limit() -> None:
    rows = _run(get_adapter().query_work_orders(None, None, 2))
    assert len(rows) == 2


# ---------- 报工明细 ----------

def test_production_reports_by_work_order() -> None:
    """按工单查报工流水：MO20260902002 两条（王强 80/4 + 李敏 72/2）。"""
    rows = _run(get_adapter().query_production_reports("MO20260902002", 20))
    assert len(rows) == 2
    assert [r["operator"] for r in rows] == ["王强", "李敏"]
    assert sum(r["good_qty"] for r in rows) == 152
    assert all(r["work_hours"] == 8.0 for r in rows)


def test_production_reports_unknown_order() -> None:
    with pytest.raises(ValueError, match="MO20990101001 无报工记录"):
        _run(get_adapter().query_production_reports("MO20990101001", 20))


def test_mock_adapter_type_exported() -> None:
    """MockMesAdapter 可直接实例化（供上游集成测试复用口径）。"""
    adapter = MockMesAdapter()
    assert asyncio.run(adapter.query_work_orders(None, None, 20))
