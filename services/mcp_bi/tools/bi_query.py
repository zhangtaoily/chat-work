"""BI 查询工具：bi__execute_query（只读）。

契约源：packages/protocol/tools/bi__execute_query.json（_meta.rw: read）。

实现（MVP mock，ARCHITECTURE 4.2.2，生产替换 BI OpenAPI）：
- 自然语言 → 查询语义规则解析（指标/期间/区域/拆分维度）
- 数据源：进程内确定性 mock 销售数据（区域 × 产品线 × 月份）
- 权限过滤：区域白名单，权限外区域数据不可查询（PRD 2.3 数据权限）
- 热点缓存：相同查询 5 分钟内复用（PRD R6 Token 成本风险缓解）
"""

import hashlib
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

# mock 数据维度（生产经 BI OpenAPI 元数据接口获取）
_REGIONS = ("华东", "华南", "华北", "华西")
_PRODUCTS = ("智能门禁", "协同办公", "数据服务")
_MONTHS = ("2026-07", "2026-08", "2026-09")
_CURRENT_MONTH = _MONTHS[-1]
# 权限范围（服务账号绑定科室数据权限，mock：销售科可见华东/华南）
_VISIBLE_REGIONS = ("华东", "华南")

# 期间别名 → 月份集合
_PERIOD_Q3 = _MONTHS
_PERIOD_PREV = _MONTHS[-2]
_PERIOD_LABELS = {"Q3": "2026Q3", "三季度": "2026Q3", "第三季度": "2026Q3"}

_GROUP_ALIASES = {
    "product": ("产品", "产品线", "品类"),
    "region": ("区域", "大区", "地区"),
    "month": ("月份", "月度", "趋势"),
}

_CACHE_TTL_SECONDS = 300  # 热点查询缓存 5 分钟（PRD R6）
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _amount(region: str, product: str, month: str) -> float:
    """确定性伪随机销售额（元）：同一维度组合恒定，重启不漂移。"""
    digest = hashlib.md5(f"{region}|{product}|{month}".encode()).digest()
    base = 600_000 + digest[0] * 12_000 + digest[1] * 45
    growth = 1.0 + 0.12 * _MONTHS.index(month)  # 月度自然增长趋势
    return round(base * growth, 2)


def _sum(region: str | None, product: str | None, months: tuple[str, ...]) -> float:
    """按口径聚合（None = 该维度全量，区域仅权限内可见）。"""
    regions = (region,) if region else _VISIBLE_REGIONS
    products = (product,) if product else _PRODUCTS
    return round(sum(_amount(r, p, m) for r in regions for p in products for m in months), 2)


def _parse_period(query: str) -> tuple[str, tuple[str, ...]]:
    """解析期间 → (展示标签, 月份集合)。缺省本月。"""
    if "上月" in query:
        return _PERIOD_PREV, (_PERIOD_PREV,)
    for alias, label in _PERIOD_LABELS.items():
        if alias in query:
            return label, _PERIOD_Q3
    for m in _MONTHS:  # 显式月份（如 2026-08）
        if m in query:
            return m, (m,)
    return _CURRENT_MONTH, (_CURRENT_MONTH,)


def _parse_region(query: str) -> str | None:
    """解析区域（None = 权限范围内全量）。"""
    return next((r for r in _REGIONS if r in query), None)


def _parse_group(query: str, dimensions: list[str] | None) -> str | None:
    """解析拆分维度：dimensions 参数优先，其次自然语言「按 X」。

    拆分口径默认继承（拆分词命中但未指明维度时由上层追问，不猜——PRD 5.2）。
    """
    if dimensions:
        for dim in dimensions:
            if dim in _GROUP_ALIASES:
                return dim
    if "拆分" in query or "分布" in query or "对比" in query or "按" in query:
        for group, aliases in _GROUP_ALIASES.items():
            if any(a in query for a in aliases):
                return group
    return None


def _mom_pct(value: float, months: tuple[str, ...]) -> float | None:
    """环比：单月查询时对上月环比；多期汇总不计算。"""
    if len(months) != 1 or months[0] == _MONTHS[0]:
        return None
    prev_month = _MONTHS[_MONTHS.index(months[0]) - 1]
    prev = _sum(None, None, (prev_month,))
    if prev == 0:
        return None
    return round((value - prev) / prev * 100, 1)


def _parse_query(query: str, dimensions: list[str] | None) -> dict[str, Any]:
    """自然语言 → 查询语义（指标校验 + 期间/区域/拆分维度）。"""
    if not any(k in query for k in ("销售", "业绩", "营收")):
        raise ValueError("暂不支持该指标查询（MVP 支持销售额相关查询，如：本月华东区销售额）")
    period_label, months = _parse_period(query)
    region = _parse_region(query)
    if region and region not in _VISIBLE_REGIONS:
        raise ValueError(f"无权限查询 {region} 区域数据（权限范围：{'、'.join(_VISIBLE_REGIONS)}）")
    group = _parse_group(query, dimensions)
    return {
        "period_label": period_label,
        "months": months,
        "region": region,
        "group": group,
    }


def _build_result(query: str, parsed: dict[str, Any]) -> dict[str, Any]:
    """执行聚合并构造响应（KPI + 趋势 + 拆分序列）。"""
    months: tuple[str, ...] = parsed["months"]
    region: str | None = parsed["region"]
    value = _sum(region, None, months)
    region_label = region or "、".join(_VISIBLE_REGIONS)
    dimension_labels = {"product": "产品线", "region": "区域", "month": "月份"}

    series: list[dict[str, Any]] | None = None
    group: str | None = parsed["group"]
    if group == "product":
        series = [{"label": p, "value": _sum(region, p, months)} for p in _PRODUCTS]
    elif group == "region":
        series = [{"label": r, "value": _sum(r, None, months)} for r in _VISIBLE_REGIONS]
    elif group == "month":
        series = [{"label": m, "value": _sum(region, None, (m,))} for m in _MONTHS]

    return {
        "query": query,
        "metric": "销售额",
        "kpi": {
            "label": "销售额",
            "value": value,
            "unit": "元",
            "period": parsed["period_label"],
            "region": region_label,
        },
        "mom_pct": _mom_pct(value, months),
        "trend": [{"period": m, "value": _sum(region, None, (m,))} for m in _MONTHS],
        "dimension": dimension_labels.get(group or "", None),
        "series": series,
        "chart_hint": "bar",
        "permission_note": f"数据已按权限过滤（可见区域：{'、'.join(_VISIBLE_REGIONS)}）",
        "cached": False,
    }


async def execute_query(query: str, dimensions: list[str] | None = None) -> dict[str, Any]:
    """bi__execute_query 工具实现（缓存层 + 解析执行）。"""
    cache_key = hashlib.md5((query + "|" + ",".join(sorted(dimensions or []))).encode()).hexdigest()
    hit = _CACHE.get(cache_key)
    if hit is not None and hit[0] > time.time():
        result = dict(hit[1])
        result["cached"] = True
        return result
    result = _build_result(query, _parse_query(query, dimensions))
    _CACHE[cache_key] = (time.time() + _CACHE_TTL_SECONDS, result)
    return result


def register(mcp: FastMCP) -> None:
    """注册 BI 查询工具（契约：packages/protocol/tools/bi__execute_query.json）。"""

    @mcp.tool()
    async def bi__execute_query(query: str, dimensions: list[str] | None = None) -> dict[str, Any]:
        """自然语言 BI 数据查询（只读：仅 SELECT 语义，禁止任何写操作）。

        Args:
            query: 自然语言查询问题（如：本月华东区销售额 / 按产品线拆分上月销售额）
            dimensions: 可选，限定分析维度（product/region/month）
        """
        return await execute_query(query, dimensions)
