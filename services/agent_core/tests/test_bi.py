"""BI 查询（bi_query，PRD 5.2 场景 3）测试：

- rules.is_bi_followup：追加维度追问判定
- 流水线集成：KPI 查询 → bi_result 卡；追问继承口径；权限报错渲染
"""

from typing import Any
from unittest.mock import patch

from agent_core.mcp_client import McpToolError
from agent_core.pipeline import rules, slots
from agent_core.pipeline.graph import build_graph
from agent_core.skills.registry import get_skill

_USER = "u001"
_SESSION = "s-bi"


def _bi_result(query: str, split: bool = False) -> dict[str, Any]:
    """bi__execute_query 工具桩响应（对齐 mcp_bi/tools/bi_query.py 返回结构）。"""
    return {
        "query": query,
        "metric": "销售额",
        "kpi": {
            "label": "销售额",
            "value": 1234567.89,
            "unit": "元",
            "period": "2026-09",
            "region": "华东" if "华东" in query else "华东、华南",
        },
        "mom_pct": 12.3,
        "trend": [
            {"period": "2026-07", "value": 1000000.0},
            {"period": "2026-08", "value": 1100000.0},
            {"period": "2026-09", "value": 1234567.89},
        ],
        "dimension": "产品线" if split else None,
        "series": [{"label": "智能门禁", "value": 800000.0}] if split else None,
        "chart_hint": "bar",
        "permission_note": "数据已按权限过滤（可见区域：华东、华南）",
        "cached": False,
    }


def _mock_call_bi_tool(name: str, arguments: dict[str, Any]) -> Any:
    assert name == "bi__execute_query"
    query = arguments["query"]
    if "华北" in query:
        raise McpToolError("无权限查询 华北 区域数据（权限范围：华东、华南）")
    if not any(k in query for k in ("销售", "业绩", "营收")):
        raise McpToolError("暂不支持该指标查询（MVP 支持销售额相关查询，如：本月华东区销售额）")
    return _bi_result(query, split="拆分" in query)


def _events_of(final_state: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    return [e for e in final_state.get("events", []) if e.get("type") == event_type]


async def _invoke(message: str) -> dict[str, Any]:
    graph = build_graph().compile()
    return await graph.ainvoke({"user_id": _USER, "session_id": _SESSION, "message": message})


async def test_is_bi_followup() -> None:
    prev = _bi_result("本月华东区销售额多少")
    assert rules.is_bi_followup("按产品类别拆分一下", prev) is True
    assert rules.is_bi_followup("按区域分布呢", prev) is True
    assert rules.is_bi_followup("上月销售额多少", prev) is False  # 新指标查询
    assert rules.is_bi_followup("按产品拆分", None) is False  # 无上轮结果
    assert rules.is_bi_followup("按产品拆分", ["AP-1"]) is False  # 上轮为审批列表


async def test_kpi_query_renders_bi_card() -> None:
    """KPI 查询：直接执行 + 结果入槽位缓存 + final 附 bi_result 卡。"""
    with patch("agent_core.pipeline.graph.call_bi_tool", side_effect=_mock_call_bi_tool):
        final = await _invoke("本月华东区销售额多少")
    assert final["tool_result"]["kpi"]["value"] == 1234567.89
    cached = await slots.get_query_result(_USER, _SESSION, "bi")
    assert cached is not None and "kpi" in cached
    finals = _events_of(final, "final")
    assert len(finals) == 1
    assert finals[0]["text"].startswith("华东 2026-09 销售额：1,234,567.89 元")
    assert "环比+12.3%" in finals[0]["text"]
    card = finals[0]["cards"][0]
    assert card["type"] == "bi_result"
    assert card["kpi"]["period"] == "2026-09"


async def test_followup_inherits_scope() -> None:
    """追加维度追问：「按产品线拆分一下」继承上轮口径（query 拼接）。"""
    calls: list[dict[str, Any]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append(arguments)
        return _mock_call_bi_tool(name, arguments)

    with patch("agent_core.pipeline.graph.call_bi_tool", side_effect=_spy):
        await _invoke("本月华东区销售额多少")
        final = await _invoke("按产品类别拆分一下")
    assert len(calls) == 2
    # 追问继承：上轮原文 + 本条追问拼接，BI 侧解析出同一口径 + 拆分维度
    assert calls[1]["query"] == "本月华东区销售额多少，按产品类别拆分一下"
    card = _events_of(final, "final")[0]["cards"][0]
    assert card["dimension"] == "产品线"
    assert card["series"] == [{"label": "智能门禁", "value": 800000.0}]


async def test_permission_error_rendered() -> None:
    """权限外区域：McpToolError → 终态错误文案（不抛异常中断会话）。"""
    with patch("agent_core.pipeline.graph.call_bi_tool", side_effect=_mock_call_bi_tool):
        final = await _invoke("华北区销售额多少")
    assert final["tool_result"] == {"error": "无权限查询 华北 区域数据（权限范围：华东、华南）"}
    finals = _events_of(final, "final")
    assert finals[0]["text"] == ("BI 查询失败：无权限查询 华北 区域数据（权限范围：华东、华南）")
    assert finals[0]["cards"] == []


async def test_metric_not_supported() -> None:
    """非销售指标：BI 侧校验拒绝（只读范围内 MVP 仅支持销售额）。"""
    with patch("agent_core.pipeline.graph.call_bi_tool", side_effect=_mock_call_bi_tool):
        final = await _invoke("本月利润指标多少")
    assert "BI 查询失败" in _events_of(final, "final")[0]["text"]


async def test_bi_result_does_not_pollute_approval_slot() -> None:
    """混合会话隔离（E2E 联调回归）：BI 查询缓存不污染行内审批定位。

    回归背景：BI 结果与待办列表曾共用同一槽位键，同会话先查 BI 再说
    「同意第 2 条」会把 dict 当 list 定位 → KeyError('1') → 服务异常。
    """
    with patch("agent_core.pipeline.graph.call_bi_tool", side_effect=_mock_call_bi_tool):
        await _invoke("本月华东区销售额多少")
    assert await slots.get_query_result(_USER, _SESSION, "bi") is not None
    # approval 槽位独立为空 → '同意第 2 条' 应走补问而非异常
    assert await slots.get_query_result(_USER, _SESSION, "approval") is None
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=lambda n, a: []):
        final = await _invoke("同意第 2 条")
    skill = get_skill("oa_todo_approve")
    assert skill is not None
    assert final.get("confirm_token") is None
    assert final["final"]["text"] == skill["ask_messages"]["approval_target"]
