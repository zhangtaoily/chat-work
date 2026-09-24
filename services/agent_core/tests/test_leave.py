"""请假审批录入（oa_leave_request，PRD 5.2 场景 1-1）测试：

- 分段收集：多轮补问合并草稿 + 年假余额查询入 draft + 补问文案融入余额
- 0.5 天粒度：改期/改半天后时长自动重算（computed 覆盖旧值）
- 校验：开始时间早于当前时间拦截、全周末区间提示
- 病假 ≥ 2 天：extract 医疗证明提示 + 成功文案补交提醒
- HITL：确认卡挂起 → 恢复图提交 → 审批流说明（≥ 3 天加签部门总监）
"""

from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

from agent_core.pipeline.graph import build_graph, build_resume_graph

_USER = "u001"


def _next_workdays(count: int = 2, min_ahead: int = 7) -> list[date]:
    """从 min_ahead 天后起取 count 个连续工作日（未来日期，避开「早于当前时间」校验）。"""
    d = date.today() + timedelta(days=min_ahead)  # noqa: DTZ011 - 测试用本地今日即可
    while d.weekday() >= 5:
        d += timedelta(days=1)
    days = [d]
    cur = d
    while len(days) < count:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days.append(cur)
    return days


_BALANCES = [{"leave_type": "annual", "total_days": 5, "remaining_days": 5}]

_SUBMIT_RESULT = {
    "doc_no": "LV-2027-1001",
    "approval_id": "AP-2027-1001",
    "status": "pending",
    "message": "请假申请已提交",
}


def _mock_call_oa_tool(name: str, arguments: dict[str, Any]) -> Any:
    """MCP 工具桩：余额查询返回年假 5 天；提交返回单据号。"""
    if name == "oa__query_leave_balance":
        return [dict(b) for b in _BALANCES]
    if name == "oa__submit_leave_request":
        return dict(_SUBMIT_RESULT)
    raise AssertionError(f"未预期的工具调用：{name}")


def _events_of(final_state: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    return [e for e in final_state.get("events", []) if e.get("type") == event_type]


async def test_two_round_collection_with_balance() -> None:
    """分段收集：第 1 轮缺事由 → 补问文案融入余额；第 2 轮补齐 → HITL 确认卡。"""
    start, end = _next_workdays(2)
    session = "s-leave-2round"
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        first = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": session,
                "message": f"我要请年假 {start.isoformat()} 到 {end.isoformat()}",
            }
        )
    draft = first["draft"]
    assert draft["leave_type"]["value"] == "annual"
    assert draft["duration_days"] == {"value": 2.0, "source": "computed"}
    assert draft["balance_days"] == {"value": 5, "source": "computed"}
    # 补问文案融入余额展示（PRD 5.2 示例：余额 + 扣减预览）
    assert first["final"]["text"] == (
        "查到你的年假余额为 5 天（本次申请 2 天，扣后剩 3 天）。\n请问请假事由是什么？"
    )
    assert first.get("confirm_token") is None

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        second = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "事由：家中有事"}
        )
    assert second["draft"]["reason"]["value"] == "家中有事"
    confirms = _events_of(second, "confirm_card")
    assert len(confirms) == 1
    assert confirms[0]["payload"]["fields"]["duration_days"] == 2.0


async def test_reason_followup_plain_reply() -> None:
    """补答整句视为事由：收集轮中无关键词回复（不含"事由/原因/因为"）识别为事由。"""
    start, end = _next_workdays(2)
    session = "s-leave-plain-reason"
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        first = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": session,
                "message": f"请年假 {start.isoformat()} 到 {end.isoformat()}",
            }
        )
    assert first["final"]["text"].endswith("请问请假事由是什么？")

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        second = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "家里有事需要回去处理"}
        )
    assert second["draft"]["reason"]["value"] == "家里有事需要回去处理"
    assert len(_events_of(second, "confirm_card")) == 1


async def test_reschedule_recomputes_duration() -> None:
    """改期重算：结束时间改成当天中午 → 时长自动重算为 1.5 并覆盖旧值。"""
    start, end = _next_workdays(2)
    session = "s-leave-reschedule"
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        first = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": session,
                "message": f"请年假 {start.isoformat()} 到 {end.isoformat()}，事由：家中有事",
            }
        )
    assert first["draft"]["duration_days"]["value"] == 2.0
    assert first["confirm_token"] is not None

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        second = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": session,
                "message": f"结束时间改成 {end.isoformat()} 12:00",
            }
        )
    draft = second["draft"]
    assert draft["end_time"]["value"] == f"{end.isoformat()}T12:00:00"
    assert draft["duration_days"] == {"value": 1.5, "source": "computed"}
    # 改期后字段仍齐备 → 直接再次进入 HITL
    assert second["confirm_token"] is not None


async def test_start_time_in_past_rejected() -> None:
    """开始时间早于当前时间：validate 提前拦截，不进 HITL。"""
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": "s-leave-past",
                "message": "请年假 2026-01-05 到 2026-01-06，事由：测试",
            }
        )
    validation = final["validation"]
    assert validation["passed"] is False
    assert any("开始时间早于当前时间" in e for e in validation["errors"])
    assert final.get("confirm_token") is None


async def test_weekend_only_range_rejected() -> None:
    """全周末区间（2027-03-13 周六 ~ 03-14 周日）：时长 0 → 提示调整时间。"""
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": "s-leave-weekend",
                "message": "请年假 2027-03-13 到 2027-03-14，事由：测试",
            }
        )
    validation = final["validation"]
    assert validation["passed"] is False
    assert any("不含工作日" in e for e in validation["errors"])
    assert final.get("confirm_token") is None


async def test_sick_leave_medical_proof() -> None:
    """病假 ≥ 2 天：extract 提示医疗证明（不查余额）；提交成功文案附补交提醒。"""
    start, end = _next_workdays(2)
    session = "s-leave-sick"
    calls: list[str] = []

    def _spy(name: str, arguments: dict[str, Any]) -> Any:
        calls.append(name)
        return _mock_call_oa_tool(name, arguments)

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_spy):
        graph = build_graph().compile()
        first = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": session,
                "message": f"请2天病假 {start.isoformat()} 到 {end.isoformat()}，事由：生病",
            }
        )
    stages = [e["message"] for e in _events_of(first, "stage_progress")]
    assert any("医疗证明" in m for m in stages)
    assert "oa__query_leave_balance" not in calls  # 病假不查余额
    assert first["confirm_token"] is not None

    state: dict[str, Any] = dict(first)
    state["confirmed"] = True
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_spy):
        resume = build_resume_graph().compile()
        final = await resume.ainvoke(state)
    assert "审批流：直属主管 → HR 备案" in final["final"]["text"]  # 2 天 < 3 天
    assert "请补交医疗证明至 HR" in final["final"]["text"]


async def test_annual_leave_3days_director_flow() -> None:
    """≥ 3 天加签部门总监；幂等键随 tool_call 传递；成功文案含审批流说明。"""
    days = _next_workdays(3)
    start, end = days[0], days[-1]
    session = "s-leave-3days"
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        first = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": session,
                "message": f"请3天年假 {start.isoformat()} 到 {end.isoformat()}，事由：家中有事",
            }
        )
    assert first["confirm_token"] is not None
    arguments = first["tool_call"]["arguments"]
    assert arguments["duration_days"] == 3.0
    assert arguments["idempotency_key"].startswith(f"{_USER}_{session}_")

    state: dict[str, Any] = dict(first)
    state["confirmed"] = True
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        resume = build_resume_graph().compile()
        final = await resume.ainvoke(state)
    assert "直属主管 → 部门总监 → HR 备案" in final["final"]["text"]
    assert "LV-2027-1001" in final["final"]["text"]
    assert final["final"]["cards"][0]["doc_no"] == "LV-2027-1001"


async def test_balance_note_without_duration() -> None:
    """首轮只说「请年假」：余额展示无扣减预览，补问开始时间。"""
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        final = await graph.ainvoke(
            {"user_id": _USER, "session_id": "s-leave-ask-start", "message": "请年假"}
        )
    assert final["draft"]["balance_days"]["value"] == 5
    assert final["final"]["text"] == (
        "查到你的年假余额为 5 天。\n请问开始时间？（口语日期即可，如 10月5号 或 2026-10-05）"
    )
    assert final.get("confirm_token") is None


async def test_colloquial_single_day_leave() -> None:
    """口语单日请假：「我10月5号请假，家里有事」→ 自动推理起止（冬令时整天）、
    时长、事由，仅补问假种；一轮补答后进确认与工具入参。"""
    session = "s-leave-colloquial"
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool), patch(
        "agent_core.pipeline.rules._today", return_value=date(2026, 9, 24)
    ):
        graph = build_graph().compile()
        first = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "我10月5号请假，家里有事"}
        )
    draft = first["draft"]
    # 单日推理（冬令时 8:30-16:30）+ 无引导词事由兜底
    assert draft["start_time"]["value"] == "2026-10-05T08:30:00"
    assert draft["end_time"]["value"] == "2026-10-05T16:30:00"
    assert draft["duration_days"] == {"value": 1.0, "source": "computed"}
    assert draft["reason"]["value"] == "家里有事"
    assert "leave_type" not in draft
    assert first.get("confirm_token") is None
    assert "请问要请哪种假" in first["final"]["text"]

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        second = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "调休"}
        )
    # 调休（comp）条件必填 tx_reason → 追问时长来源
    assert second.get("confirm_token") is None
    assert "调休时长来源" in second["final"]["text"]

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        third = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "加班"}
        )
    assert third["confirm_token"] is not None
    args = third["tool_call"]["arguments"]
    assert args["start_time"] == "2026-10-05T08:30:00"
    assert args["duration_days"] == 1.0
    assert args["tx_reason"] == 0  # 裸选项词「加班」补答映射


async def test_comp_leave_tx_reason_from_message() -> None:
    """调休时长来源对话指定：「来源是加班」→ 草稿/确认卡/工具入参透传 tx_reason。"""
    start, end = _next_workdays(2)
    session = "s-leave-tx-reason"
    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        graph = build_graph().compile()
        first = await graph.ainvoke(
            {
                "user_id": _USER,
                "session_id": session,
                "message": f"我要调休 {start.isoformat()} 到 {end.isoformat()}，来源是加班",
            }
        )
    assert first["draft"]["tx_reason"] == {"value": 0, "source": "ask"}
    assert first.get("confirm_token") is None  # 缺事由 → 补问轮

    with patch("agent_core.pipeline.graph.call_oa_tool", side_effect=_mock_call_oa_tool):
        second = await graph.ainvoke(
            {"user_id": _USER, "session_id": session, "message": "事由：项目上线调休"}
        )
    # 确认卡：tx_reason 原值 + 中文标签
    confirms = _events_of(second, "confirm_card")
    assert len(confirms) == 1
    assert confirms[0]["payload"]["fields"]["tx_reason"] == 0
    assert confirms[0]["payload"]["fields"]["tx_reason_label"] == "加班"
    # 工具入参透传（契约 oa__submit_leave_request.json tx_reason）
    assert second["tool_call"]["arguments"]["tx_reason"] == 0
