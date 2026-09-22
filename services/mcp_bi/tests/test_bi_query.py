"""bi__execute_query 工具单测（不经 MCP 传输，直接调实现函数）：

- 规则解析：期间（本月/上月/Q3）/ 区域 / 拆分维度
- 聚合：KPI + 趋势 + 拆分序列一致性
- 权限：权限外区域拒绝（PRD 2.3）
- 热点缓存：相同查询 5 分钟内复用（PRD R6）
"""

import asyncio

import pytest

from tools.bi_query import _parse_group, _parse_period, _parse_region, execute_query


def test_parse_period() -> None:
    assert _parse_period("本月华东区销售额")[0] == "2026-09"
    assert _parse_period("上月销售额")[0] == "2026-08"
    assert _parse_period("Q3 各区域销售额")[0] == "2026Q3"
    assert _parse_period("2026-08 销售额")[0] == "2026-08"
    assert _parse_period("销售额多少")[0] == "2026-09"  # 缺省本月


def test_parse_region_and_group() -> None:
    assert _parse_region("华东区销售额") == "华东"
    assert _parse_region("销售额") is None
    assert _parse_group("按产品线拆分销售额", None) == "product"
    assert _parse_group("按区域分布", None) == "region"
    assert _parse_group("销售额", ["month"]) == "month"  # 参数优先
    assert _parse_group("本月销售额", None) is None


def test_kpi_and_series_consistency() -> None:
    """拆分序列合计 = KPI（同口径聚合一致性）。"""
    result = asyncio.run(execute_query("本月华东区销售额按产品线拆分"))
    total = result["kpi"]["value"]
    assert total > 0
    assert result["dimension"] == "产品线"
    assert sum(s["value"] for s in result["series"]) == pytest.approx(total)
    assert result["cached"] is False


def test_permission_error() -> None:
    """权限外区域（华北/华西不可见）拒绝查询。"""
    with pytest.raises(ValueError, match="无权限"):
        asyncio.run(execute_query("本月华北区销售额"))


def test_hot_cache_reuse() -> None:
    """热点缓存：相同查询 5 分钟内复用，cached 标记翻转（响应为副本，不污染缓存体）。"""
    first = asyncio.run(execute_query("本月华南区销售额"))
    second = asyncio.run(execute_query("本月华南区销售额"))
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["kpi"] == first["kpi"]
    assert first["cached"] is False  # 原缓存体标记不被翻转
