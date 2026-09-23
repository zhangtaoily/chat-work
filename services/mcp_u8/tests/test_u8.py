"""mcp-u8 单测：mock 适配器直测（科目余额/凭证明细借贷平衡）。

覆盖 P3.1 U8 场景（PLAN P3.1，PRD 7.1）：
- 科目余额表查询（期间/科目关键词过滤；与 ERP 凭证摘要同口径呼应）
- 凭证明细查询（借贷分录 + 平衡校验会计恒等式；未知凭证报错）
"""

import asyncio
from typing import Any

import pytest

from adapters.u8_client import MockU8Adapter, get_adapter
from tools.gl import _PERIOD_PATTERN


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """每个测试独立：mock 模式 + 适配器单例重置。"""
    monkeypatch.setenv("U8_MODE", "mock")
    from adapters import u8_client

    u8_client._adapter = None
    yield
    u8_client._adapter = None


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------- 科目余额表 ----------

def test_gl_balance_2026_09() -> None:
    """2026-09 余额表：6 个科目，本期借/贷合计（与 ERP 凭证摘要同口径）。"""
    data = _run(get_adapter().query_gl_balance("2026-09", None))
    assert data["period"] == "2026-09"
    assert len(data["rows"]) == 6
    # 与 mcp-erp voucher_summary 2026-09 呼应：银行存款本期借 120 万
    bank = next(r for r in data["rows"] if r["subject"] == "银行存款")
    assert bank["debit"] == 1_200_000.0
    assert bank["closing"] == 500_000.0


def test_gl_balance_subject_filter() -> None:
    """科目关键词过滤：「应收」命中应收账款 1 行。"""
    data = _run(get_adapter().query_gl_balance("2026-09", "应收"))
    assert [r["subject"] for r in data["rows"]] == ["应收账款"]
    assert data["debit_total"] == 600_000.0


def test_gl_balance_2026_08() -> None:
    data = _run(get_adapter().query_gl_balance("2026-08", None))
    assert len(data["rows"]) == 3
    assert data["debit_total"] == 1_100_000.0
    assert data["credit_total"] == 1_100_000.0


def test_gl_balance_unknown_period() -> None:
    with pytest.raises(ValueError, match="无 2025-01 期间科目余额"):
        _run(get_adapter().query_gl_balance("2025-01", None))


def test_gl_period_pattern() -> None:
    """期间格式校验：YYYY-MM 且月份 01-12。"""
    assert _PERIOD_PATTERN.match("2026-09") is not None
    assert _PERIOD_PATTERN.match("2026-13") is None
    assert _PERIOD_PATTERN.match("202609") is None


# ---------- 凭证明细 ----------

def test_voucher_detail_balanced() -> None:
    """凭证明细借贷平衡：记-2026090128 借 30 万 = 贷 30 万。"""
    detail = _run(get_adapter().query_voucher_detail("记-2026090128"))
    assert detail["balanced"] is True
    assert detail["debit_total"] == detail["credit_total"] == 300_000.0
    assert {e["direction"] for e in detail["entries"]} == {"debit", "credit"}


def test_voucher_detail_136() -> None:
    detail = _run(get_adapter().query_voucher_detail("记-2026090136"))
    assert detail["summary"] == "采购原材料入账（华东机械）"
    assert detail["balanced"] is True


def test_voucher_detail_unknown() -> None:
    with pytest.raises(ValueError, match="凭证 记-2099010001 不存在"):
        _run(get_adapter().query_voucher_detail("记-2099010001"))


def test_mock_adapter_type_exported() -> None:
    """MockU8Adapter 可直接实例化（供上游集成测试复用口径）。"""
    adapter = MockU8Adapter()
    assert asyncio.run(adapter.query_gl_balance("2026-09", None))
