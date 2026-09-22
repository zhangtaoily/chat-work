"""mcp-crm 单测：mock 适配器直测（订单链路 / 审批流 / 幂等 / 客户模糊匹配）。

覆盖 PRD 6.1.1 核心规则：
- 客户名称模糊匹配列候选；客户 360 返回默认值链数据
- 大额订单（合计 ≥ ¥5,000）审批流自动加销售总监节点
- 金额服务端重算（数量×单价，2 位小数）
- 写入幂等：同键重复提交返回原单据编号
- 联系人必须在册；交货日期无默认值且不得早于今天
"""

import asyncio
from typing import Any

import pytest

import db
import idempotency
from adapters.crm_client import MockCrmAdapter, get_adapter
from tools.orders import submit_sales_order


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """每个测试独立：mock 模式 + SQLite 内存幂等表 + 全局单例重置（避免跨 loop）。"""
    monkeypatch.setenv("CRM_MODE", "mock")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from adapters import crm_client

    db._engine = None
    db._session_factory = None
    idempotency._engine_ready = False
    crm_client._adapter = None
    yield
    db._engine = None
    db._session_factory = None
    idempotency._engine_ready = False
    crm_client._adapter = None


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------- 客户：模糊匹配 / 360 ----------

def test_search_customers_fuzzy_hits() -> None:
    """关键词 'XX' 模糊命中 3 条候选（对齐 PRD 6.1.1 多候选场景）。"""
    adapter = get_adapter()
    hits = _run(adapter.search_customers("XX"))
    assert len(hits) == 3
    assert all("XX" in h["name"] for h in hits)
    assert {h["customer_id"] for h in hits} == {"C-1001", "C-1002", "C-1003"}


def test_customer_360_returns_defaults() -> None:
    """客户 360 返回联系人/付款条件/最近成交价（订单草稿默认值链数据源）。"""
    adapter = get_adapter()
    profile = _run(adapter.get_customer_360("C-1002"))
    assert profile["name"] == "XX 实业（苏州）有限公司"
    assert profile["contacts"] == ["李四", "王五"]  # 多联系人 → Agent 必问
    assert profile["payment_term"] == "net60"
    assert profile["last_prices"]["SKU-A"] == 48.5


def test_customer_360_not_found() -> None:
    adapter = get_adapter()
    with pytest.raises(ValueError, match="客户不存在"):
        _run(adapter.get_customer_360("C-9999"))


# ---------- 销售订单：审批流 / 金额重算 / 幂等 ----------

def _order_items(sku: str = "SKU-A", qty: int = 20, price: float = 50.0) -> list[dict[str, Any]]:
    return [{"sku": sku, "qty": qty, "price": price}]


def _submit_kwargs(total_small: bool = True) -> dict[str, Any]:
    return {
        "user_id": "E001",
        "order_type": "standard",
        "customer_id": "C-1001",
        "contact": "张三",
        "address": "上海市浦东新区 XX 路 88 号",
        "items": _order_items(qty=20 if total_small else 200),
        "delivery_date": "2026-12-31",
        "payment_term": "net30",
        "idempotency_key": "E001_s1_intent_v1",
    }


def test_submit_small_order_single_approver() -> None:
    """小额订单（1,000 元）：审批流仅直属主管；金额服务端重算。"""
    result = _run(submit_sales_order(**_submit_kwargs(total_small=True)))
    assert result["status"] == "submitted"
    assert result["doc_no"].startswith("SO")
    assert result["total_amount"] == 1000.0
    assert result["approval_flow"] == ["直属主管"]


def test_submit_large_order_adds_director() -> None:
    """大额订单（10,000 元 ≥ ¥5,000）：审批流加销售总监节点（PRD 6.1.1）。"""
    result = _run(submit_sales_order(**_submit_kwargs(total_small=False)))
    assert result["total_amount"] == 10000.0
    assert result["approval_flow"] == ["直属主管", "销售总监"]


def test_submit_idempotent_replay() -> None:
    """同幂等键重复提交：命中返回原单据编号，不重复下单。"""
    kwargs = _submit_kwargs(total_small=True)
    first = _run(submit_sales_order(**kwargs))
    adapter: MockCrmAdapter = get_adapter()
    orders_after_first = len(adapter._orders)  # 预置历史单 + 本次 1 单
    second = _run(submit_sales_order(**kwargs))
    assert second["doc_no"] == first["doc_no"]
    assert second["idempotent_reuse"] is True
    assert len(adapter._orders) == orders_after_first  # 只落了一单


def test_submit_rounding_two_decimals() -> None:
    """金额四舍五入 2 位小数（19.999 元 × 3 = 59.997 → 60.00 由服务端重算）。"""
    kwargs = _submit_kwargs(total_small=True)
    kwargs["items"] = [{"sku": "SKU-A", "qty": 3, "price": 19.999}]
    result = _run(submit_sales_order(**kwargs))
    assert result["total_amount"] == 60.0


def test_submit_contact_must_be_registered() -> None:
    """联系人不在册：报错并列候选（多联系人客户必问）。"""
    kwargs = _submit_kwargs(total_small=True)
    kwargs.update(customer_id="C-1002", contact="张三")
    with pytest.raises(ValueError, match="候选：李四、王五"):
        _run(submit_sales_order(**kwargs))


def test_submit_rejects_past_delivery_date() -> None:
    """交货日期不得早于今天（PRD：无默认值必问，服务端兜底校验）。"""
    kwargs = _submit_kwargs(total_small=True)
    kwargs["delivery_date"] = "2020-01-01"
    with pytest.raises(ValueError, match="不得早于今天"):
        _run(submit_sales_order(**kwargs))


def test_submit_rejects_bad_delivery_format() -> None:
    kwargs = _submit_kwargs(total_small=True)
    kwargs["delivery_date"] = "09/01"
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _run(submit_sales_order(**kwargs))


def test_submit_rejects_empty_items() -> None:
    kwargs = _submit_kwargs(total_small=True)
    kwargs["items"] = []
    with pytest.raises(ValueError, match="明细不能为空"):
        _run(submit_sales_order(**kwargs))


def test_submit_rejects_non_positive_qty() -> None:
    kwargs = _submit_kwargs(total_small=True)
    kwargs["items"] = [{"sku": "SKU-A", "qty": 0, "price": 50.0}]
    with pytest.raises(ValueError, match="数量必须大于 0"):
        _run(submit_sales_order(**kwargs))


def test_submit_rejects_unknown_order_type() -> None:
    kwargs = _submit_kwargs(total_small=True)
    kwargs["order_type"] = "vip"
    with pytest.raises(ValueError, match="无效单据类型"):
        _run(submit_sales_order(**kwargs))


def test_submit_rejects_unknown_payment_term() -> None:
    kwargs = _submit_kwargs(total_small=True)
    kwargs["payment_term"] = "net90"
    with pytest.raises(ValueError, match="无效付款方式"):
        _run(submit_sales_order(**kwargs))


def test_submit_rejects_missing_idempotency_key() -> None:
    kwargs = _submit_kwargs(total_small=True)
    kwargs["idempotency_key"] = ""
    with pytest.raises(ValueError, match="幂等键缺失"):
        _run(submit_sales_order(**kwargs))


# ---------- 跟单：进度与异常 ----------

def test_query_order_progress_with_exception() -> None:
    """预置延期订单：状态 delayed，异常可见（跟单场景）。"""
    adapter = get_adapter()
    progress = _run(adapter.query_order_progress("SO20260820007"))
    assert progress["status"] == "delayed"
    assert progress["exceptions"]
    assert progress["delivery_date"] == "2026-09-05"


def test_query_order_progress_not_found() -> None:
    adapter = get_adapter()
    with pytest.raises(ValueError, match="订单不存在"):
        _run(adapter.query_order_progress("SO20990101001"))
