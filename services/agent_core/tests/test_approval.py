"""行内审批（oa__approve，PRD 5.2 场景 2）测试：

- rules.parse_approval_action：动作词/序号/单号解析
- slots.query_result：最近查询结果存取
- 流水线集成：读路径缓存列表 → 写路径 HITL 挂起 → 恢复图执行 → 结果渲染
"""

from typing import Any
from unittest.mock import patch

from agent_core.pipeline import rules, slots
from agent_core.pipeline.graph import build_graph, build_resume_graph
from agent_core.skills.registry import get_skill

_USER = "u001"
_SESSION = "s-approval"
_SESSION_NO_QUERY = "s-no-query"  # 独立会话：避免共享 _SESSION 的查询缓存污染
_SESSION_RANGE = "s-out-of-range"
_SESSION_THIS = "s-this-no-last"  # 独立会话：无 approval_last 槽位的"这条"补问场景

_ITEMS: list[dict[str, Any]] = [
    {
        "approval_id": "AP-2026-0001",
        "doc_type": "leave",
        "doc_no": "LV-2026-0912",
        "title": "张三的年假申请（2 天）",
        "applicant": "张三",
        "submitted_at": "2026-09-12T10:00:00",
        "status": "pending",
    },
    {
        "approval_id": "AP-2026-0002",
        "doc_type": "leave",
        "doc_no": "LV-2026-0913",
        "title": "李四的调休申请（1 天）",
        "applicant": "李四",
        "submitted_at": "2026-09-13T14:30:00",
        "status": "pending",
    },
    {
        "approval_id": "AP-2026-0003",
        "doc_type": "leave",
        "doc_no": "LV-2026-0914",
        "title": "王五的病假申请（0.5 天）",
        "applicant": "王五",
        "submitted_at": "2026-09-14T09:00:00",
        "status": "pending",
    },
]


def _mock_call_oa_tool(name: str, arguments: dict[str, Any]) -> Any:
    """MCP 工具桩：读路径返回待办列表，写路径返回审批结果。"""
    if name == "oa__query_pending_approvals":
        return [dict(it) for it in _ITEMS]
    if name == "oa__approve":
        return {
            "approval_id": arguments["approval_id"],
            "doc_no": "LV-2026-0913",
            "status": "approved" if arguments["action"] == "approve" else "rejected",
            "message": "已同意" if arguments["action"] == "approve" else "已驳回",
        }
    raise AssertionError(f"未预期的工具调用：{name}")


def _events_of(final_state: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    return [e for e in final_state.get("events", []) if e.get("type") == event_type]


async def test_parse_approval_action() -> None:
    assert rules.parse_approval_action("同意第 2 条") == ("approve", "index:2")
    assert rules.parse_approval_action("驳回第1条") == ("reject", "index:1")
    assert rules.parse_approval_action("批准第 3 条") == ("approve", "index:3")
    assert rules.parse_approval_action("不同意第 3 条") == ("reject", "index:3")
    assert rules.parse_approval_action("同意 AP-2026-0001") == (
        "approve",
        "id:AP-2026-0001",
    )
    assert rules.parse_approval_action("同意 ap-2026-0002") == (
        "approve",
        "id:AP-2026-0002",
    )
    assert rules.parse_approval_action("同意") == ("approve", "")
    assert rules.parse_approval_action("这条驳回，原因是预算超了") == ("reject", "last")
    assert rules.parse_approval_action("这一条同意") == ("approve", "last")
    assert rules.parse_approval_action("查一下我的待办审批") is None
    assert rules.parse_approval_action("我下周三想请一天年假") is None


async def test_extract_approval_comment() -> None:
    """审批附言提取（PRD 5.2 场景 2 驳回原因）。"""
    assert rules.extract_approval_comment("这条驳回，原因是预算超了") == "预算超了"
    assert rules.extract_approval_comment("驳回 AP-2026-0003 原因是预算超了") == "预算超了"
    assert rules.extract_approval_comment("驳回第 2 条：理由是材料不全") == "材料不全"
    assert rules.extract_approval_comment("这条驳回，原因：部门预算不足。") == "部门预算不足"
    assert rules.extract_approval_comment("驳回第 2 条") is None


async def test_query_result_roundtrip() -> None:
    await slots.set_query_result(_USER, _SESSION, _ITEMS, "approval")
    assert await slots.get_query_result(_USER, _SESSION, "approval") == _ITEMS
    assert await slots.get_query_result(_USER, "other", "approval") is None


async def test_read_path_caches_items() -> None:
    """查待办：直接执行 + 列表入槽位缓存 + final 附 todo_list 卡。"""
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": _SESSION,
                "message": "查一下我的待办审批",
            }
        )
    assert final["tool_result"] == _ITEMS
    assert await slots.get_query_result(_USER, _SESSION, "approval") == _ITEMS
    finals = _events_of(final, "final")
    assert len(finals) == 1
    assert finals[0]["cards"] == [{"type": "todo_list", "items": _ITEMS}]


async def test_inline_approval_hitl_suspend() -> None:
    """行内审批写路径：定位第 2 条 → HITL 挂起（confirm_card，未执行写入）。"""
    await slots.set_query_result(_USER, _SESSION, _ITEMS, "approval")
    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return _mock_call_oa_tool(name, arguments)

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_spy):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": _SESSION, "message": "同意第 2 条"}
        )

    draft = final["draft"]
    assert draft["approval_id"]["value"] == "AP-2026-0002"
    assert draft["action"]["value"] == "approve"

    # HITL 挂起：confirm_card 已发，写入未执行
    assert final["confirm_token"] is not None
    confirms = _events_of(final, "confirm_card")
    assert len(confirms) == 1
    assert confirms[0]["payload"]["fields"]["approval_title"] == "李四的调休申请（1 天）"
    assert confirms[0]["payload"]["fields"]["action"] == "approve"
    assert calls == []  # 写入被挂起
    assert final["tool_result"] is None

    # 幂等键含目标条目+动作（不同待办互不冲突）
    arguments = final["tool_call"]["arguments"]
    assert arguments["approval_id"] == "AP-2026-0002"
    assert arguments["action"] == "approve"
    assert arguments["idempotency_key"].startswith(f"{_USER}_{_SESSION}_")


async def test_inline_approval_resume_executes() -> None:
    """恢复图：确认后 execute→format 完成写入并渲染结果。"""
    await slots.set_query_result(_USER, _SESSION, _ITEMS, "approval")
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        suspended = await graph.ainvoke(
            {"user_id": _USER, "session_id": _SESSION, "message": "同意第 2 条"}
        )

    state: dict[str, Any] = dict(suspended)
    state["confirmed"] = True
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        resume = build_resume_graph().compile()
        final = await resume.ainvoke(state)

    assert final["tool_result"]["status"] == "approved"
    assert final["tool_result"]["approval_id"] == "AP-2026-0002"
    assert "LV-2026-0913" in final["final"]["text"]


async def test_inline_approval_reject_by_id() -> None:
    """单号直达：'驳回 AP-2026-0003' 走写路径。"""
    await slots.set_query_result(_USER, _SESSION, _ITEMS, "approval")
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": _SESSION,
                "message": "驳回 AP-2026-0003",
            }
        )
    assert final["draft"]["approval_id"]["value"] == "AP-2026-0003"
    assert final["draft"]["action"]["value"] == "reject"
    assert final["confirm_token"] is not None


async def test_inline_approval_without_query_asks_back() -> None:
    """未查过列表即说'同意第 2 条'：定位失败 → 补问（不进入 HITL、不触发查询）。"""
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": _SESSION_NO_QUERY, "message": "同意第 2 条"}
        )
    skill = get_skill("oa_todo_approve")
    assert skill is not None
    final_state = final["final"]
    assert final_state is not None
    assert final_state["text"] == skill["ask_messages"]["approval_target"]
    assert final.get("confirm_token") is None
    assert await slots.get_query_result(_USER, _SESSION_NO_QUERY, "approval") is None


async def test_inline_approval_index_out_of_range_asks_back() -> None:
    """序号越界：'同意第 9 条' → 补问。"""
    await slots.set_query_result(_USER, _SESSION_RANGE, _ITEMS, "approval")
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": _SESSION_RANGE, "message": "同意第 9 条"}
        )
    final_state = final["final"]
    assert final_state is not None
    skill = get_skill("oa_todo_approve")
    assert skill is not None
    assert final_state["text"] == skill["ask_messages"]["approval_target"]
    assert final.get("confirm_token") is None


async def test_inline_reject_this_with_comment() -> None:
    """PRD 5.2 场景 2 验收句：'这条驳回，原因是预算超了'。

    上轮定位第 1 条 → "这条"承接 → comment 入 draft 与确认卡 → 恢复执行
    → 工具入参透传 comment → 结果文案含驳回原因。
    """
    await slots.set_query_result(_USER, _SESSION, _ITEMS, "approval")
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        first = await graph.ainvoke(
            {"user_id": _USER, "session_id": _SESSION, "message": "同意第 1 条"}
        )
    assert first["draft"]["approval_id"]["value"] == "AP-2026-0001"

    calls: list[tuple[str, dict[str, Any]]] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, arguments))
        return _mock_call_oa_tool(name, arguments)

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_spy):
        graph = build_graph().compile()
        suspended = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": _SESSION,
                "message": "这条驳回，原因是预算超了",
            }
        )

    draft = suspended["draft"]
    assert draft["approval_id"]["value"] == "AP-2026-0001"
    assert draft["action"]["value"] == "reject"
    assert draft["comment"]["value"] == "预算超了"
    assert suspended["confirm_token"] is not None
    confirms = _events_of(suspended, "confirm_card")
    assert confirms[0]["payload"]["fields"]["comment"] == "预算超了"
    assert calls == []  # 写入被挂起

    state: dict[str, Any] = dict(suspended)
    state["confirmed"] = True
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_spy):
        resume = build_resume_graph().compile()
        final = await resume.ainvoke(state)

    assert len(calls) == 1
    name, arguments = calls[0]
    assert name == "oa__approve"
    assert arguments["approval_id"] == "AP-2026-0001"
    assert arguments["action"] == "reject"
    assert arguments["comment"] == "预算超了"
    assert arguments["idempotency_key"].startswith(f"{_USER}_{_SESSION}_")
    assert final["tool_result"]["status"] == "rejected"
    assert "驳回原因「预算超了」" in final["final"]["text"]


async def test_inline_this_without_last_asks_back() -> None:
    """'这条驳回' 但上轮未定位过条目 → 补问（无 approval_last 槽位）。"""
    await slots.set_query_result(_USER, _SESSION_THIS, _ITEMS, "approval")
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": _SESSION_THIS, "message": "这条驳回"}
        )
    final_state = final["final"]
    assert final_state is not None
    skill = get_skill("oa_todo_approve")
    assert skill is not None
    assert final_state["text"] == skill["ask_messages"]["approval_target"]
    assert final.get("confirm_token") is None
