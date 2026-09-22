"""周报生成（weekly_report，PRD 5.2 场景 4）测试：

- rules.build_weekly_markdown：操作记录聚合 Markdown（概要/明细/下周计划）
- 流水线集成：意图命中 → oa__query_activity_log → weekly_report 卡
- 工具报错：McpToolError → 终态错误文案（不抛异常中断会话）
"""

from typing import Any
from unittest.mock import patch

from agent_core.mcp_client import McpToolError
from agent_core.pipeline import rules
from agent_core.pipeline.graph import build_graph
from agent_core.skills.registry import match_skill

_USER = "u001"
_SESSION = "s-weekly"


def _activity_items() -> list[dict[str, Any]]:
    """oa__query_activity_log 工具桩响应（对齐 mcp_oa mock 结构）。"""
    return [
        {
            "user_id": _USER,
            "action_type": "submit_leave",
            "summary": "提交年假申请（2 天，事由：家中有事）",
            "occurred_at": "2026-09-17T10:30:00",
        },
        {
            "user_id": _USER,
            "action_type": "approve",
            "summary": "审批通过李四的调休申请（LV-2026-0913）",
            "occurred_at": "2026-09-16T11:30:00",
        },
        {
            "user_id": _USER,
            "action_type": "query_bi",
            "summary": "查询华东区 2026-09 销售额",
            "occurred_at": "2026-09-15T12:30:00",
        },
    ]


def _mock_call_oa_tool(name: str, arguments: dict[str, Any]) -> Any:
    assert name == "oa__query_activity_log"
    assert arguments["user_id"] == _USER
    assert 1 <= arguments["days"] <= 31
    return _activity_items()


def _events_of(final_state: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    return [e for e in final_state.get("events", []) if e.get("type") == event_type]


async def _invoke(message: str) -> dict[str, Any]:
    graph = build_graph().compile()
    return await graph.ainvoke({"user_id": _USER, "session_id": _SESSION, "message": message})


def test_match_weekly_intent() -> None:
    """意图命中：「帮我生成本周工作周报」路由到 weekly_report 技能。"""
    skill = match_skill("帮我生成本周工作周报")
    assert skill is not None and skill["name"] == "weekly_report"


def test_build_weekly_markdown_with_items() -> None:
    """有记录：标题周期 + 概要计数 + 明细表 + 下周计划占位。"""
    md = rules.build_weekly_markdown(_activity_items())
    assert md.startswith("# 个人工作周报（2026-09-15 ~ 2026-09-17）")
    assert "- 请假提交：1 项" in md
    assert "- 审批处理：1 项" in md
    assert "- 数据查询：1 项" in md
    assert "| 2026-09-17 | 请假提交 | 提交年假申请（2 天，事由：家中有事） |" in md
    assert "## 下周计划" in md


def test_build_weekly_markdown_empty() -> None:
    """空记录：骨架占位（周期回退最近 7 天），不抛异常。"""
    md = rules.build_weekly_markdown([])
    assert md.startswith("# 个人工作周报（")
    assert "本周暂无 OA 操作记录" in md
    assert "## 下周计划" in md


async def test_weekly_pipeline_renders_report_card() -> None:
    """流水线集成：命中技能 → 查日志 → Markdown 草稿卡。"""
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        final = await _invoke("帮我生成本周工作周报")
    assert final["tool_result"] == _activity_items()
    finals = _events_of(final, "final")
    assert len(finals) == 1
    assert finals[0]["text"].startswith("已生成本周工作周报草稿（3 条操作记录）")
    card = finals[0]["cards"][0]
    assert card["type"] == "weekly_report"
    assert card["item_count"] == 3
    assert card["markdown"].startswith("# 个人工作周报（2026-09-15 ~ 2026-09-17）")


async def test_weekly_error_rendered() -> None:
    """工具报错：McpToolError → 终态错误文案（不抛异常中断会话）。"""

    def _fail(name: str, arguments: dict[str, Any]) -> Any:
        raise McpToolError("操作日志服务暂不可用")

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_fail):
        final = await _invoke("帮我生成本周工作周报")
    assert final["tool_result"] == {"error": "操作日志服务暂不可用"}
    finals = _events_of(final, "final")
    assert finals[0]["text"] == "周报生成失败：操作日志服务暂不可用"
    assert finals[0]["cards"] == []
