"""跨系统编排测试（PLAN P2.6，PRD 3.2.2/3.3/6.3/14）。

单元层（workflow store）：steps 声明校验（契约池白名单/写步骤强制
确认/rw 推断）、创建防线（mode/scope、科室禁 craft）、上下文传递
（{{input.x}}/{{step_N.x}} 全匹配与内嵌插值）、三模式执行分形（plan
计划卡不落运行、ask 遇写步骤 partial 止步、craft/plan 批准后写步骤
挂确认卡、确认恢复续跑（幂等键注入）/拒绝取消）、步骤失败中止、
停用编排拒绝执行、终态发布 workflow.run.finished、启停与删除。
事件触发（automation store p2-6d）：event 调度校验、publish→轮询
消费、fire owner 限定与节流、push 渠道校验。
wecom 双通道（p2-6e）：未配置降级信箱、推送成功免落信箱、errcode≠0/
网络异常降级、编排通知共用通道。
API 层（TestClient + make_token）：恒需认证 401、编排 CRUD/启停/
手动执行权限分层（仅管理员）、plan 卡透传、runs actor 过滤、通知
信箱与已读、事件手动触发。
"""

import asyncio
import os
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from agent_core import audit, automation, wecom
from agent_core.api import auth as auth_mod
from agent_core.api.main import app
from agent_core.automation import store as auto_store
from agent_core.skills import store as skill_store
from agent_core.workflow import store as wf_store


def make_token(rsa_key: Any, **overrides: Any) -> str:
    """按 PRD 8.5.4 claims 结构签发测试 token（与 test_automation 同口径）。"""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": auth_mod.SSO_ISSUER,
        "sub": "E1001",
        "idp": "ad",
        "idp_sub": "zhangsan@corp.com",
        "dept": "事业部A/销售科",
        "roles": ["employee"],
        "perm_ver": 17,
        "aud": auth_mod.SSO_AUDIENCE,
        "sid": "sess-1",
        "iat": now,
        "exp": now + 1800,
        "jti": "jti-1",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return pyjwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-kid"})


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_workflow() -> Iterator[None]:
    """测试隔离：编排/自动化/市场域清空 + 调度器关闭 + 审计清空 + wecom 未配置。"""
    auth_mod.set_sso_required(False)
    auto_store.stop_scheduler()
    automation.reset()
    wf_store.reset()
    skill_store.reset()  # 市场状态重建内置基线（防跨文件注册残留）
    os.environ.pop("WECOM_WEBHOOK_URL", None)
    asyncio.run(audit.clear())
    yield
    auto_store.stop_scheduler()
    os.environ.pop("WECOM_WEBHOOK_URL", None)
    asyncio.run(audit.clear())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


READ_STEPS = [
    {"tool": "crm__search_customers", "desc": "CRM 搜客户", "args": {"kw": "{{input.kw}}"}},
    {
        "tool": "erp__query_inventory",
        "desc": "ERP 查库存",
        "args": {"sku": "{{step_1.customer_id}}"},
    },
]

_OWNER_AUTH = {"user_id": "E1001", "dept": "事业部A/销售科", "roles": ["employee"]}


async def _mk_wf(
    steps: list[dict[str, Any]] | None = None,
    *,
    mode: str = "plan",
    scope: str = "official",
    name: str = "订单编排",
) -> dict[str, Any]:
    """便捷创建编排定义（owner_auth 快照三键，与 API 层转换口径一致）。"""
    return await wf_store.create(
        name=name,
        steps=READ_STEPS if steps is None else steps,
        owner="E1001",
        owner_auth=dict(_OWNER_AUTH),
        mode=mode,
        scope=scope,
    )


def _mk_event_task(event: str = "stock.low", owner: str = "E1001") -> dict[str, Any]:
    """event 任务便捷创建（绑定内置只读技能；执行由桩接管不触 MCP）。"""
    return asyncio.run(
        automation.create(
            name=f"订阅{event}",
            skill="erp_inventory_query",
            params={"sku": {"value": "SKU-001", "source": "extract"}},
            schedule={"type": "event", "event_name": event},
            owner=owner,
            owner_auth={"user_id": owner, "dept": _OWNER_AUTH["dept"], "roles": ["employee"]},
        )
    )


def _stub_tools(
    monkeypatch: pytest.MonkeyPatch, *, fail_tools: set[str] | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """MCP 桩：记录 (tool, args) 调用序；fail_tools 内的工具抛异常。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(tool: str, args: dict[str, Any]) -> Any:
        calls.append((tool, dict(args)))
        if fail_tools and tool in fail_tools:
            raise RuntimeError(f"{tool} 下游不可用")
        return {"customer_id": "C001", "qty": 5}

    monkeypatch.setattr(wf_store, "_call_tool", fake_call)
    return calls


def _stub_run_once(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """automation.run_once 桩：记录执行的任务 id（事件触发用例不触子图）。"""
    ran: list[str] = []

    async def fake_run_once(task_id: str) -> None:
        ran.append(task_id)

    monkeypatch.setattr(auto_store, "run_once", fake_run_once)
    return ran


# ---- steps 声明校验与创建防线（PRD 3.2.2/3.3）----


def test_validate_steps_normalizes() -> None:
    """rw 从契约池推断 + 写工具强制 requires_confirm + seq 按序重排。"""
    norm = wf_store.validate_steps(
        [
            {"tool": "crm__submit_sales_order", "args": {"x": 1}},
            {"tool": "erp__query_inventory", "args": {}, "requires_confirm": True},
        ]
    )
    assert [s["seq"] for s in norm] == [1, 2]
    assert norm[0]["rw"] == "write" and norm[0]["requires_confirm"] is True  # 写强制确认
    assert norm[1]["rw"] == "read" and norm[1]["requires_confirm"] is True  # 读可显式开启


@pytest.mark.parametrize(
    "steps",
    [
        [],
        "not-a-list",
        [{"args": {}}],  # 缺 tool
        [{"tool": "hr__fire_someone", "args": {}}],  # 不在契约池白名单
        [{"tool": "erp__query_inventory", "args": "x"}],  # args 非对象
    ],
)
def test_validate_steps_rejects(steps: Any) -> None:
    """非法 steps 一律 ValueError（白名单外工具/形态不符）。"""
    with pytest.raises(ValueError):
        wf_store.validate_steps(steps)


async def test_create_list_detail() -> None:
    """创建后 list（新在前 + status 过滤）与 detail 可查。"""
    wf = await _mk_wf()
    assert wf["id"].startswith("wf_") and wf["status"] == "active"
    assert len(wf_store.list_workflows()) == 1
    assert wf_store.list_workflows(status="disabled") == []
    assert wf_store.detail(wf["id"])["name"] == "订单编排"
    assert wf_store.detail("wf_999999") is None


@pytest.mark.parametrize(
    "name,mode,scope,match",
    [
        ("  ", "plan", "official", "名称不能为空"),
        ("t", "auto", "official", "mode 仅支持"),
        ("t", "plan", "personal", "scope 仅支持"),
        ("t", "craft", "dept", "科室编排"),
    ],
)
def test_create_rejects(name: str, mode: str, scope: str, match: str) -> None:
    """名称/mode/scope 非法与科室 craft 模式一律拒绝（PRD 3.3 规则3）。"""
    with pytest.raises(ValueError, match=match):
        asyncio.run(
            wf_store.create(
                name=name,
                steps=READ_STEPS,
                owner="E1001",
                owner_auth=dict(_OWNER_AUTH),
                mode=mode,
                scope=scope,
            )
        )


def test_resolve_value_context() -> None:
    """上下文传递：全匹配返回对象本身、内嵌做字符串插值、点路径取值、缺失报错。"""
    ctx = {"input": {"kw": "手机"}, "step_1": {"customer_id": "C001", "items": ["a", "b"]}}
    assert wf_store.resolve_value("{{input.kw}}", ctx) == "手机"  # 标量全匹配
    assert wf_store.resolve_value("{{step_1.items}}", ctx) == ["a", "b"]  # 对象全匹配
    assert wf_store.resolve_value("订单-{{step_1.customer_id}}", ctx) == "订单-C001"
    assert wf_store.resolve_value("{{step_1.customer_id}}-{{input.kw}}", ctx) == "C001-手机"
    with pytest.raises(ValueError, match="无法解析"):
        wf_store.resolve_value("{{step_9.missing}}", ctx)


# ---- 三模式执行分形（PRD 3.3）----


async def test_execute_plan_card_no_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """plan 模式未批准：返回计划卡（含系统/参数预览），不触 MCP 不落运行。"""
    wf = await _mk_wf()
    out = await wf_store.execute(wf["id"], {"kw": "手机"})
    assert out["status"] == "plan"
    assert out["plan"]["steps"][0]["system"] == "CRM"
    assert out["plan"]["steps"][1]["args"] is None  # 前步输出占位执行期才可解析
    assert wf_store.runs(wf["id"]) == []


async def test_execute_ask_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """ask 模式：读步骤照跑，遇写步骤止步 partial（未执行写）。"""
    calls = _stub_tools(monkeypatch)
    wf = await _mk_wf(
        [
            {"tool": "erp__query_inventory", "args": {"sku": "S1"}},
            {"tool": "crm__submit_sales_order", "args": {"x": 1}},
        ],
        mode="ask",
    )
    out = await wf_store.execute(wf["id"], {}, plan_approved=True)
    assert out["status"] == "partial" and out["run"]["ok"] is True
    assert [s["ok"] for s in out["run"]["steps"]] == [True, True]  # 第二步止步未执行
    assert "止步" in out["run"]["steps"][1]["detail"]
    assert len(calls) == 1  # 写步骤未触达 MCP
    assert wf_store.detail(wf["id"])["stats"]["success"] == 1  # partial 计成功


async def test_execute_ok_context_passing(monkeypatch: pytest.MonkeyPatch) -> None:
    """plan 批准后读编排全 ok：{{step_1.x}} 传递前步输出 + 通知落信箱 + 终态事件发布。"""
    calls = _stub_tools(monkeypatch)
    wf = await _mk_wf()
    out = await wf_store.execute(wf["id"], {"kw": "手机"}, plan_approved=True)
    assert out["status"] == "ok" and out["run"]["ok"] is True
    assert calls[1][1]["sku"] == "C001"  # step_1 输出传入 step_2（上下文传递）
    assert out["run"]["outputs"] == {"customer_id": "C001", "qty": 5}
    # 通知（wecom 未配置降级信箱）+ 已读标记
    msgs = wf_store.notifications("E1001")
    assert len(msgs) == 1 and msgs[0]["ok"] is True and msgs[0]["read"] is False
    assert wf_store.mark_notifications_read("E1001") == 1
    # 终态旁路事件（p2-6d）：订阅 workflow.run.finished 的自动化任务可命中
    assert [e["event"] for e in auto_store._pending_events] == ["workflow.run.finished"]
    assert auto_store._pending_events[0]["payload"]["run_id"] == out["run"]["run_id"]


async def test_execute_craft_pending_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """craft 模式遇写步骤：挂 confirm_card（写步骤无论什么模式强制 HITL）。"""
    calls = _stub_tools(monkeypatch)
    wf = await _mk_wf(
        [
            {"tool": "erp__query_inventory", "args": {"sku": "S1"}},
            {"tool": "crm__submit_sales_order", "args": {"order_type": "standard"}},
        ],
        mode="craft",
    )
    out = await wf_store.execute(wf["id"], {})
    assert out["status"] == "pending_confirm" and out["confirm_token"]
    assert len(calls) == 1  # 确认前写步骤未执行
    run = out["run"]
    assert run["status"] == "pending_confirm" and run["pending"]["step_index"] == 1


async def test_resume_confirm_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认恢复：从挂起步骤续跑至 ok；写工具注入确定性幂等键（PRD 8.7）。"""
    calls = _stub_tools(monkeypatch)
    wf = await _mk_wf(
        [{"tool": "crm__submit_sales_order", "args": {"order_type": "standard"}}],
        mode="craft",
    )
    out = await wf_store.execute(wf["id"], {}, user_id="E1001")
    resumed = await wf_store.resume_confirm(out["confirm_token"], approved=True)
    assert resumed is not None and resumed["status"] == "ok"
    args = calls[0][1]
    assert args["user_id"] == "E1001"  # 写工具自动注入操作者
    assert args["idempotency_key"] == f"{out['run']['run_id']}_1"  # 确定性幂等键


async def test_resume_confirm_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    """拒绝写步骤：run 终态 cancelled，MCP 未触达。"""
    calls = _stub_tools(monkeypatch)
    wf = await _mk_wf([{"tool": "crm__submit_sales_order", "args": {}}], mode="craft")
    out = await wf_store.execute(wf["id"], {})
    resumed = await wf_store.resume_confirm(out["confirm_token"], approved=False)
    assert resumed is not None and resumed["status"] == "cancelled"
    assert resumed["run"]["ok"] is False and calls == []


async def test_resume_confirm_bad_token() -> None:
    """未知/已消费 token：返回 None（一次性令牌双重消费防护）。"""
    assert await wf_store.resume_confirm("no-such-token", approved=True) is None


async def test_execute_disabled_rejected() -> None:
    """停用编排不可执行（技能/自动化/事件触发同此门禁）。"""
    wf = await _mk_wf()
    await wf_store.set_status(wf["id"], status="disabled", by="E1001")
    with pytest.raises(ValueError, match="已停用"):
        await wf_store.execute(wf["id"], {}, plan_approved=True)


async def test_execute_step_failure_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """任一步失败即中止（不做回滚，PRD 8.7 幂等键兜底）：failed + 后步未跑。"""
    _stub_tools(monkeypatch, fail_tools={"erp__query_inventory"})
    wf = await _mk_wf()
    out = await wf_store.execute(wf["id"], {"kw": "x"}, plan_approved=True)
    assert out["status"] == "failed" and out["run"]["ok"] is False
    assert len(out["run"]["steps"]) == 2 and out["run"]["steps"][1]["ok"] is False
    assert wf_store.detail(wf["id"])["stats"]["failed"] == 1


async def test_set_status_and_delete_keeps_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """启停迁移 + 删除后运行记录仍可查（PRD 10 审计覆盖口径）。"""
    _stub_tools(monkeypatch)
    wf = await _mk_wf()
    await wf_store.execute(wf["id"], {}, plan_approved=True)
    await wf_store.set_status(wf["id"], status="disabled", by="E1001")
    assert wf_store.detail(wf["id"])["status"] == "disabled"
    await wf_store.delete(wf["id"], by="E1001")
    assert wf_store.detail(wf["id"]) is None
    assert len(wf_store.runs(wf["id"])) == 1  # 删除后运行历史保留


# ---- 事件触发（p2-6d，PRD 3.6.1）----


def test_event_schedule_validation() -> None:
    """event 调度：event_name 必填 + 节流窗口校验 + 无固定触发时间。"""
    automation.validate_schedule({"type": "event", "event_name": "stock.low"})
    automation.validate_schedule(
        {"type": "event", "event_name": "stock.low", "interval_minutes": 30}
    )
    with pytest.raises(ValueError, match="event_name"):
        automation.validate_schedule({"type": "event"})
    assert automation.next_run_time({"type": "event", "event_name": "x"}) is None


def test_publish_then_poll_consumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """publish_event 入队 → 轮询检查器原子取出消费（事件名命中任务执行）。"""
    ran = _stub_run_once(monkeypatch)
    task = _mk_event_task()
    auto_store.publish_event("workflow.run.finished")  # 未订阅事件不命中任务
    auto_store.publish_event("stock.low", payload={"sku": "SKU-001"})
    asyncio.run(auto_store._poll_events())
    assert ran == [task["id"]]
    assert auto_store._pending_events == []  # 队列已清空


def test_fire_event_owner_scope_and_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """fire_event：owner 限定命中（普通用户仅本人任务）+ 节流窗口跳过。"""
    _stub_run_once(monkeypatch)
    mine = _mk_event_task("stock.low", owner="E1001")
    _mk_event_task("stock.low", owner="E2002")
    # 普通用户 owner 限定：仅命中本人任务
    out = asyncio.run(auto_store.fire_event("stock.low", by="E1001", owner="E1001"))
    assert [m["id"] for m in out["matched"]] == [mine["id"]]
    # 管理员 owner=None：命中全部同事件任务
    out = asyncio.run(auto_store.fire_event("stock.low", by="SEC001", owner=None))
    assert len(out["matched"]) == 2
    # 节流：距上次执行不足窗口 → 跳过（PRD 3.6.2 防护栏）；桩未更新
    # last_run_at，显式为全部 event 任务盖上「刚执行过」时间戳
    for t in auto_store._tasks.values():
        t["last_run_at"] = auto_store._now()
    out = asyncio.run(auto_store.fire_event("stock.low", by="E1001", owner=None))
    assert out["matched"] == []


def test_channel_validation_and_wecom_task() -> None:
    """push 渠道校验（p2-6e）：仅 inbox/wecom_bot；wecom_bot 合法创建。"""
    with pytest.raises(ValueError, match="push 渠道"):
        asyncio.run(
            automation.create(
                name="渠道非法",
                skill="erp_inventory_query",
                params={"sku": {"value": "SKU-001", "source": "extract"}},
                schedule={"type": "daily", "time": "09:00"},
                channel={"push": "chatwork_session", "format": "markdown"},
                owner="E1001",
                owner_auth=dict(_OWNER_AUTH),
            )
        )
    asyncio.run(
        automation.create(
            name="企微播报",
            skill="erp_inventory_query",
            params={"sku": {"value": "SKU-001", "source": "extract"}},
            schedule={"type": "daily", "time": "09:00"},
            channel={"push": "wecom_bot", "format": "markdown"},
            owner="E1001",
            owner_auth=dict(_OWNER_AUTH),
        )
    )
    assert auto_store.list_tasks()[0]["channel"]["push"] == "wecom_bot"


# ---- wecom 双通道（p2-6e，PRD 14）----


async def test_wecom_unconfigured_falls_back() -> None:
    """未配置 WECOM_WEBHOOK_URL：push 返回 False，通知落进程内信箱（零依赖默认值）。"""
    assert await wecom.push_markdown("t") is False
    task = {
        "id": "auto_000001",
        "name": "n",
        "skill": "s",
        "owner": "E1001",
        "channel": {"push": "wecom_bot"},
    }
    n0 = len(auto_store.inbox("E1001"))
    await auto_store._deliver(task, kind="result", ok=True, text="x")
    assert len(auto_store.inbox("E1001")) == n0 + 1  # wecom_bot 渠道未配置 → 降级信箱


def _mock_wecom(
    monkeypatch: pytest.MonkeyPatch, *, errcode: int = 0, raises: bool = False
) -> AsyncMock:
    """替换 httpx.AsyncClient 为桩（记录 url/json 载荷，可注入错误码/异常）。

    注意用 return_value 而非 side_effect=resp：MagicMock 可调用，会作为
    side_effect 被调用返回未配置的 return_value，errcode 断言恒假。
    """
    resp = MagicMock()
    resp.json.return_value = {"errcode": errcode, "errmsg": "x"}
    post = (
        AsyncMock(side_effect=RuntimeError("net down"))
        if raises
        else AsyncMock(return_value=resp)
    )
    client = MagicMock()
    client.return_value.__aenter__.return_value.post = post
    monkeypatch.setattr(wecom.httpx, "AsyncClient", client)
    return post


async def test_wecom_push_success_skips_inbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置 + errcode=0：推送成功返回 True，wecom_bot 渠道通知免落信箱。"""
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://qy.example/hook")
    post = _mock_wecom(monkeypatch)
    assert await wecom.push_markdown("hi") is True
    assert post.call_args.args[0] == "https://qy.example/hook"
    body = post.call_args.kwargs["json"]
    assert body["msgtype"] == "markdown" and body["markdown"]["content"] == "hi"
    task = {
        "id": "auto_000001",
        "name": "n",
        "skill": "s",
        "owner": "E1001",
        "channel": {"push": "wecom_bot"},
    }
    n0 = len(auto_store.inbox("E1001"))
    await auto_store._deliver(task, kind="result", ok=True, text="y")
    assert len(auto_store.inbox("E1001")) == n0  # 推送成功不重复落信箱


@pytest.mark.parametrize("errcode,raises", [(93000, False), (0, True)])
async def test_wecom_push_degrades_on_failure(
    monkeypatch: pytest.MonkeyPatch, errcode: int, raises: bool
) -> None:
    """errcode≠0 / 网络异常：返回 False 降级信箱，不抛异常不阻断主流程。"""
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://qy.example/hook")
    _mock_wecom(monkeypatch, errcode=errcode, raises=raises)
    assert await wecom.push_markdown("z") is False
    task = {
        "id": "auto_000001",
        "name": "n",
        "skill": "s",
        "owner": "E1001",
        "channel": {"push": "wecom_bot"},
    }
    n0 = len(auto_store.inbox("E1001"))
    await auto_store._deliver(task, kind="result", ok=True, text="w")
    assert len(auto_store.inbox("E1001")) == n0 + 1  # 推送失败降级落信箱（通知不丢）


async def test_notify_wecom_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """编排通知双通道（workflow._notify）：推送成功免落信箱，与 automation 共用通道。"""
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://qy.example/hook")
    _mock_wecom(monkeypatch)
    wf = await _mk_wf()
    run = {"run_id": "wfr_000001", "actor": "E1001", "ok": True}
    n0 = len(wf_store.notifications("E1001"))
    await wf_store._notify(wf, run, text="编排完成")
    assert len(wf_store.notifications("E1001")) == n0  # 推送成功不落信箱
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "")  # 取消配置 → 降级
    await wf_store._notify(wf, run, text="编排完成")
    assert len(wf_store.notifications("E1001")) == n0 + 1


# ---- API 层（p2-6f）----


def test_api_requires_auth(client: TestClient) -> None:
    """编排/事件端点恒需认证（无 token → 401）。"""
    assert client.get("/workflows").status_code == 401
    assert client.post("/automations/events/fire", json={"event": "x"}).status_code == 401


def test_api_workflow_crud_admin(client: TestClient, rsa_key: Any) -> None:
    """管理员全流程：注册→列表→详情→停用→删除；普通用户管理 403。"""
    admin = auth(make_token(rsa_key, sub="SEC001", roles=["security_reviewer"], jti="j-a"))
    body = {"name": "订单编排", "steps": READ_STEPS, "mode": "plan", "scope": "official"}
    assert (
        client.post("/workflows", json=body, headers=auth(make_token(rsa_key))).status_code
        == 403
    )
    resp = client.post("/workflows", json=body, headers=admin)
    assert resp.status_code == 200
    wf_id = resp.json()["id"]
    assert client.get("/workflows", headers=admin).json()["count"] == 1
    assert client.get(f"/workflows/{wf_id}", headers=admin).json()["name"] == "订单编排"
    assert client.get("/workflows/none", headers=admin).status_code == 404
    assert (
        client.post(
            f"/workflows/{wf_id}/status", json={"status": "disabled"}, headers=admin
        ).json()["status"]
        == "disabled"
    )
    assert client.delete(f"/workflows/{wf_id}", headers=admin).json()["deleted"] is True


def test_api_execute_plan_and_runs_filter(
    client: TestClient, rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """手动执行分形透传（plan 卡）+ runs 权限分层（普通用户仅本人触发）。"""
    _stub_tools(monkeypatch)
    admin = auth(make_token(rsa_key, sub="SEC001", roles=["dept_manager"], jti="j-a"))
    wf = asyncio.run(_mk_wf())
    # 管理员手动执行：plan 未批准 → 计划卡（不落运行）
    resp = client.post(
        f"/workflows/{wf['id']}/execute", json={"inputs": {"kw": "手机"}}, headers=admin
    )
    assert resp.json()["status"] == "plan"
    assert wf_store.runs(wf["id"]) == []
    # 普通用户 API 执行 403（API 直接执行绕过 graph 权限管线故收紧）
    assert (
        client.post(
            f"/workflows/{wf['id']}/execute",
            json={"inputs": {}},
            headers=auth(make_token(rsa_key)),
        ).status_code
        == 403
    )
    # runs：管理员全量；普通用户仅本人触发的运行（actor 过滤）
    asyncio.run(
        wf_store.execute(wf["id"], {}, trigger="manual", user_id="SEC001", plan_approved=True)
    )
    runs_admin = client.get(f"/workflows/{wf['id']}/runs", headers=admin).json()
    assert runs_admin["count"] == 1
    runs_user = client.get(
        f"/workflows/{wf['id']}/runs", headers=auth(make_token(rsa_key))
    ).json()
    assert runs_user["count"] == 0  # actor=SEC001，非本人触发


def test_api_notifications_and_read(
    client: TestClient, rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """编排通知信箱（恒取当前用户）+ 全部已读标记。"""
    _stub_tools(monkeypatch)
    token = auth(make_token(rsa_key, jti="j-n"))
    wf = asyncio.run(_mk_wf())
    run = asyncio.run(
        wf_store.execute(
            wf["id"], {"kw": "x"}, trigger="manual", user_id="E1001", plan_approved=True
        )
    )
    assert run["status"] == "ok"
    items = client.get("/workflow-notifications", headers=token).json()
    assert items["count"] == 1 and items["items"][0]["read"] is False
    assert (
        client.get(
            "/workflow-notifications", headers=token, params={"unread_only": "true"}
        ).json()["count"]
        == 1
    )
    marked = client.post("/workflow-notifications/read", headers=token).json()["marked"]
    assert marked == 1
    assert (
        client.get(
            "/workflow-notifications", headers=token, params={"unread_only": "true"}
        ).json()["count"]
        == 0
    )


def test_api_events_fire_permission(
    client: TestClient, rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事件手动触发 API：普通用户 owner 限定本人任务；管理员触发全部。"""
    _stub_run_once(monkeypatch)
    mine = _mk_event_task("stock.low", owner="E1001")
    resp = client.post(
        "/automations/events/fire",
        json={"event": "stock.low"},
        headers=auth(make_token(rsa_key)),
    )
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["matched"]] == [mine["id"]]
    admin = auth(make_token(rsa_key, sub="SEC001", roles=["dept_manager"], jti="j-f"))
    _mk_event_task("stock.low", owner="E2002")
    resp = client.post(
        "/automations/events/fire", json={"event": "stock.low"}, headers=admin
    )
    assert len(resp.json()["matched"]) == 2
