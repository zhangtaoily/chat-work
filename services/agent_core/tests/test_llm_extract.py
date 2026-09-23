"""LLM 槽位抽取测试（规则优先，LLM 兜底）。

单元层：未配置 LLM_BASE_URL 零行为变化、tool_calls 解析、非法技能名过滤、
HTTP 故障静默降级。
图层：LLM 兜底路由（规则关键词未命中 → 小模型选技能 + 补槽位 → 直建任务）、
LLM 补缺失字段（规则命中技能缺时刻）、LLM 故障时纯规则路径不受影响。
"""

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest

from agent_core import audit, automation
from agent_core.automation import store as auto_store
from agent_core.pipeline import llm
from agent_core.pipeline.graph import build_graph
from agent_core.skills import store as skill_store
from agent_core.skills.registry import match_skill

_USER = "E2001"


@pytest.fixture(autouse=True)
def _reset_and_disable_llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """测试隔离：任务域清空 + 调度器关闭 + 市场基线重建 + 审计清空 + LLM 关闭。"""
    auto_store.stop_scheduler()
    automation.reset()
    skill_store.reset()
    asyncio.run(audit.clear())
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    yield
    auto_store.stop_scheduler()
    asyncio.run(audit.clear())


def _tool_payload(skill: str, fields: dict[str, Any]) -> dict[str, Any]:
    """OpenAI 兼容 tool_calls 响应体。"""
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "extract_slots",
                                "arguments": json.dumps(
                                    {"skill": skill, "fields": fields},
                                    ensure_ascii=False,
                                ),
                            }
                        }
                    ]
                }
            }
        ]
    }


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> list[dict[str, Any]]:
    """替身 httpx 客户端：记录请求（url/body）并返回固定载荷或抛错。"""
    calls: list[dict[str, Any]] = []

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            assert payload is not None
            return payload

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> _Resp:
            calls.append({"url": url, **kwargs})
            if error is not None:
                raise error
            return _Resp()

    monkeypatch.setattr(llm.httpx, "AsyncClient", _Client)
    return calls


# ---- 单元层：llm.extract_slots ----


async def test_extract_slots_not_configured() -> None:
    """未配置 LLM_BASE_URL → 不调用、返回 None（纯规则，零行为变化）。"""
    assert llm.enabled() is False
    assert (
        await llm.extract_slots("帮我创建一个自动化任务，明天下午5点提醒我开会")
        is None
    )


async def test_extract_slots_parses_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    calls = _mock_http(
        monkeypatch,
        _tool_payload(
            "automation_task_create",
            {"remind_at": "2026-09-25T17:00:00", "content": "订会议室"},
        ),
    )
    result = await llm.extract_slots("周五下班前叫我记得订会议室")
    assert result == {
        "skill": "automation_task_create",
        "fields": {"remind_at": "2026-09-25T17:00:00", "content": "订会议室"},
    }
    assert len(calls) == 1
    assert calls[0]["url"] == "http://llm.test/v1/chat/completions"
    body = calls[0]["json"]
    assert body["model"] == "qwen2.5-32b-instruct"
    assert body["tools"][0]["function"]["name"] == "extract_slots"
    assert body["tool_choice"] == "auto"


async def test_extract_slots_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法技能名过滤 / 无函数调用 / HTTP 故障 → 维持规则结果（None）。"""
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    _mock_http(monkeypatch, _tool_payload("nonexistent_skill", {"content": "x"}))
    assert await llm.extract_slots("随便说点什么") == {
        "skill": None,
        "fields": {"content": "x"},
    }
    _mock_http(monkeypatch, {"choices": [{"message": {}}]})
    assert await llm.extract_slots("随便说点什么") is None
    _mock_http(monkeypatch, error=RuntimeError("boom"))
    assert await llm.extract_slots("随便说点什么") is None


# ---- 图层：LLM 兜底路由 + 补槽位 + 降级回归 ----


async def test_chat_llm_routes_and_creates_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """规则关键词未命中 → LLM 选技能 + 槽位随 intent 透传 → 直建 once 任务。

    复用校验：一轮仅一次 LLM 调用（intent 抽取结果 extract 节点不重复调用）。
    """
    message = "周五下班之前叫我记得给老王回电话"
    assert match_skill(message) is None  # 前置：规则层必不命中
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    at = f"{(datetime.now().astimezone() + timedelta(days=2)).date().isoformat()}T17:00:00"
    calls = _mock_http(
        monkeypatch,
        _tool_payload(
            "automation_task_create",
            {"remind_at": at, "content": "给老王回电话"},
        ),
    )
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {"user_id": _USER, "session_id": "s-llm-route", "message": message}
    )
    assert "已创建定时提醒" in result["final"]["text"]
    tasks = auto_store.list_tasks(owner=_USER)
    assert len(tasks) == 1
    assert tasks[0]["skill"] == "send_reminder"
    assert tasks[0]["schedule"]["type"] == "once"
    assert tasks[0]["schedule"]["at"] == at
    assert tasks[0]["params"]["content"]["value"] == "给老王回电话"
    assert len(calls) == 1  # intent 抽取结果透传复用，extract 不二次调用


async def test_chat_llm_fills_missing_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """规则命中技能抽到正文、缺时刻 → LLM 补缺失字段后直建任务。"""
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    at = f"{(datetime.now().astimezone() + timedelta(days=1)).date().isoformat()}T18:30:00"
    _mock_http(
        monkeypatch,
        _tool_payload("automation_task_create", {"remind_at": at}),
    )
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {
            "user_id": _USER,
            "session_id": "s-llm-fill",
            "message": "帮我创建一个自动化任务，提醒我晚上联络活动",
        }
    )
    assert "已创建定时提醒" in result["final"]["text"]
    tasks = auto_store.list_tasks(owner=_USER)
    assert len(tasks) == 1
    assert tasks[0]["schedule"]["at"] == at


async def test_chat_llm_failure_falls_back_to_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM 已配置但持续故障 → 规则路径完全不受影响（兜底回归）。"""
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    _mock_http(monkeypatch, error=RuntimeError("boom"))
    graph = build_graph().compile()
    result = await graph.ainvoke(
        {
            "user_id": _USER,
            "session_id": "s-llm-fallback",
            "message": "帮我创建一个自动化任务，明天下午5点。提醒我晚上联络活动",
        }
    )
    assert "已创建定时提醒" in result["final"]["text"]
    assert len(auto_store.list_tasks(owner=_USER)) == 1
