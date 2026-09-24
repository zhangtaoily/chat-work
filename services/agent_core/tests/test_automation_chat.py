"""自动化任务 chat 入口测试（PRD 3.6.2：对话创建定时提醒）。

规则层：中文时刻解析（今天/明天/后天 + 上午/下午/晚上 + N点[N分/半]）
与提醒正文抽取（「提醒我 X」引导词剥离）。
图层：用户原句端到端（对话直建 once 任务，执行体 send_reminder）、
缺时刻补问续收（pending slot 两轮收集）、调度器到点执行提醒入信箱。
"""

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest

from agent_core import audit, automation
from agent_core.automation import store as auto_store
from agent_core.pipeline import rules
from agent_core.pipeline.graph import build_graph
from agent_core.skills import store as skill_store
from agent_core.skills.registry import match_skill

_USER = "E2001"


@pytest.fixture(autouse=True)
def _reset_automation() -> Iterator[None]:
    """测试隔离：任务域清空 + 调度器关闭 + 市场基线重建 + 审计清空。"""
    auto_store.stop_scheduler()
    automation.reset()
    skill_store.reset()
    asyncio.run(audit.clear())
    yield
    auto_store.stop_scheduler()
    asyncio.run(audit.clear())


# ---- 规则层：中文时刻 + 提醒正文抽取 ----


def test_reminder_time_phrases() -> None:
    today = datetime.now().astimezone().date()
    cases = {
        "今天下午5点": today,
        "明天上午9点半": today + timedelta(days=1),
        "后天晚上8点15分": today + timedelta(days=2),
        "明天中午12点": today + timedelta(days=1),
    }
    times = {"今天下午5点": "17:00", "明天上午9点半": "09:30", "后天晚上8点15分": "20:15", "明天中午12点": "12:00"}
    for msg, day in cases.items():
        got = rules.extract_reminder_time(f"帮我创建一个自动化任务，{msg}。提醒我测试")
        assert got == f"{day.isoformat()}T{times[msg]}:00", msg


def test_reminder_time_invalid() -> None:
    assert rules.extract_reminder_time("帮我创建一个自动化任务，提醒我开会") is None
    assert rules.extract_reminder_time("提醒我25点开会") is None


def test_reminder_content_phrases() -> None:
    assert (
        rules.extract_reminder_content("帮我创建一个自动化任务，今天下午5点。提醒我晚上联络活动")
        == "晚上联络活动"
    )
    assert rules.extract_reminder_content("明天上午9点半") is None


def test_reminder_short_keyword_variant() -> None:
    """「自动任务」（少「化」字）变体：路由与正文抽取均命中（线上反馈回归）。"""
    msg = "建一个自动任务，今天下午5点，有个吃饭的活动"
    skill = match_skill(msg)
    assert skill is not None and skill["name"] == "automation_task_create"
    assert rules.extract_reminder_content(msg) == "今天下午5点，有个吃饭的活动"


# ---- 图层：对话直建定时提醒 ----


async def test_chat_creates_once_reminder() -> None:
    """用户句式端到端：命中技能 → 抽取齐备 → 直建 once 任务（无确认卡）。"""
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {
            "user_id": _USER,
            "session_id": "s-auto-create",
            "message": "帮我创建一个自动化任务，明天下午5点。提醒我晚上联络活动",
        }
    )
    text = result["final"]["text"]
    assert "已创建定时提醒" in text and "晚上联络活动" in text
    tasks = auto_store.list_tasks(owner=_USER)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["skill"] == "send_reminder"
    assert task["schedule"]["type"] == "once"
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
    assert task["schedule"]["at"] == f"{tomorrow}T17:00:00"
    assert task["params"]["content"]["value"] == "晚上联络活动"


async def test_chat_creates_once_reminder_short_keyword() -> None:
    """端到端：「自动任务」说法直接创建（此前落到闲聊兜底的线上回归）。"""
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {
            "user_id": _USER,
            "session_id": "s-auto-create-short",
            "message": "建一个自动任务，明天上午9点，有个吃饭的活动",
        }
    )
    text = result["final"]["text"]
    assert "已创建定时提醒" in text and "有个吃饭的活动" in text
    tasks = auto_store.list_tasks(owner=_USER)
    assert len(tasks) == 1
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
    assert tasks[0]["schedule"]["at"] == f"{tomorrow}T09:00:00"
    assert tasks[0]["params"]["content"]["value"] == "明天上午9点，有个吃饭的活动"


async def test_chat_reask_missing_time() -> None:
    """缺时刻 → 补问；下一轮裸时刻回复经 pending 续收并创建。"""
    graph = build_graph().compile()
    session = "s-auto-reask"
    first = await graph.ainvoke(
        {
            "user_id": _USER,
            "session_id": session,
            "message": "帮我创建一个自动化任务，提醒我晚上联络活动",
        }
    )
    assert "什么时候提醒你" in first["final"]["text"]
    assert auto_store.list_tasks(owner=_USER) == []
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
    second = await graph.ainvoke(
        {"user_id": _USER, "session_id": session, "message": "明天上午9点半"}
    )
    assert "已创建定时提醒" in second["final"]["text"]
    tasks = auto_store.list_tasks(owner=_USER)
    assert len(tasks) == 1
    assert tasks[0]["schedule"]["at"] == f"{tomorrow}T09:30:00"


async def test_scheduler_delivers_reminder_to_inbox() -> None:
    """到点执行：调度器跑 send_reminder 子图，提醒正文入结果信箱。"""
    at = (datetime.now().astimezone() + timedelta(minutes=5)).isoformat(timespec="seconds")
    task = await automation.create(
        name="晚上联络活动",
        skill="send_reminder",
        params={"content": {"value": "晚上联络活动", "source": "ask"}},
        schedule={"type": "once", "at": at},
        owner=_USER,
        owner_auth={"user_id": _USER, "dept": "事业部A/销售科", "roles": ["employee"]},
    )
    await auto_store.run_once(task["id"])
    msgs = auto_store.inbox(_USER)
    assert len(msgs) == 1
    assert msgs[0]["kind"] == "result"
    assert "提醒：晚上联络活动" in msgs[0]["text"]
    after = auto_store.list_tasks(owner=_USER)[0]
    assert after["stats"]["success"] == 1
