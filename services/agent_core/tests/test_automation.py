"""自动化任务测试（PLAN P2.4，PRD 3.6）。

单元层（store 纯函数/状态机）：调度规则校验（四类型合法 + 非法拒绝）、
next_run_time 逐日扫描（workday 跳周末/weekly/monthly/once 过期/
生效期外）、创建防护栏（write 技能拒绝/未上架拒绝/params 草稿形态/
单用户 active ≤3/once 过期）、生命周期迁移（pause/resume/delete +
非法迁移 ValueError）、record_run 回写（历史/信箱/统计/连续 3 败
自动暂停 + 通知 + 审计）。
执行管线：MCP 桩跑真实只读子图（erp_inventory_query，以创建者身份）；
dept_scope 技能以无权限科室创建者执行 → 权限拒绝计失败（权限随人走）；
mock 持续失败 ×3 → 自动暂停联动。
API 层（TestClient + make_token）：恒需认证 401、创建/列表 owner 过滤、
write 技能 400、归属门禁 403（非创建者禁操作，管理员可查）、
暂停/恢复/删除全流程、执行历史与结果信箱（含已读标记）。
"""

import asyncio
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from agent_core import audit, automation
from agent_core.api import auth as auth_mod
from agent_core.api.main import app
from agent_core.automation import store as auto_store
from agent_core.skills import registry as skill_registry
from agent_core.skills import store as skill_store

ISSUER = auth_mod.SSO_ISSUER
AUDIENCE = auth_mod.SSO_AUDIENCE
TZ = ZoneInfo("Asia/Shanghai")


def make_token(rsa_key: Any, **overrides: Any) -> str:
    """按 PRD 8.5.4 claims 结构签发测试 token（与 test_skill_store 同口径）。"""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": "E1001",
        "idp": "ad",
        "idp_sub": "zhangsan@corp.com",
        "dept": "事业部A/销售科",
        "roles": ["employee"],
        "perm_ver": 17,
        "aud": AUDIENCE,
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
def _reset_automation() -> Iterator[None]:
    """测试隔离：任务域清空 + 调度器关闭 + 审计清空 + 关闭强制鉴权。"""
    auth_mod.set_sso_required(False)
    auto_store.stop_scheduler()
    automation.reset()
    skill_store.reset()  # 市场状态重建内置基线（防跨文件注册残留）
    asyncio.run(audit.clear())
    yield
    auto_store.stop_scheduler()
    asyncio.run(audit.clear())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


DRAFT = {"sku": {"value": "SKU-001", "source": "extract"}}
DAILY = {"type": "daily", "time": "09:00"}

_FAKE_DEF = {  # 市场注册需配套的运行时技能定义（registry 无内置 write 可上架技能）
    "name": "bulk_write",
    "title": "批量写入",
    "rw": "write",
    "required_roles": [],
    "intent_patterns": [],
    "read_tools": [],
    "write_tool": None,
    "ask_messages": {},
    "required_fields": [],
}


def _inject_registry(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """向运行时注册表注入测试技能定义（monkeypatch 自动清理）。"""
    monkeypatch.setitem(skill_registry.SKILLS, name, {**_FAKE_DEF, "name": name})


async def _mk(
    owner: str = "E1001",
    skill: str = "erp_inventory_query",
    params: dict[str, Any] | None = None,
    schedule: dict[str, Any] | None = None,
    name: str = "每日库存播报",
    roles: list[str] | None = None,
) -> dict[str, Any]:
    """便捷创建：owner_auth 快照三键（与 API 层转换口径一致）。"""
    return await automation.create(
        name=name,
        skill=skill,
        params=DRAFT if params is None else params,
        schedule=DAILY if schedule is None else schedule,
        owner=owner,
        owner_auth={
            "user_id": owner,
            "dept": "事业部A/生产科",
            "roles": roles or ["employee"],
        },
    )


# ---- 调度规则：校验与下次触发（纯函数）----


def test_schedule_validation_ok() -> None:
    """四种调度类型合法形态 + workday/生效期/interval 均通过。"""
    automation.validate_schedule({"type": "daily", "time": "09:00"})
    automation.validate_schedule({"type": "daily", "time": "09:00", "days": ["workday"]})
    automation.validate_schedule({"type": "weekly", "time": "08:30", "days": [1, 3, 5]})
    automation.validate_schedule({"type": "monthly", "time": "07:00", "days": [1, 15]})
    automation.validate_schedule({"type": "once", "at": "2030-01-01T09:00:00"})
    automation.validate_schedule(
        {
            "type": "daily",
            "time": "09:00",
            "interval_minutes": 15,
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
        }
    )


@pytest.mark.parametrize(
    "schedule",
    [
        {"type": "event"},  # event 缺 event_name（P2.6 后 event_name 必填）
        {"type": "hourly", "time": "09:00"},
        {"type": "daily", "time": "9:00"},  # 非 HH:MM
        {"type": "daily", "time": "25:00"},
        {"type": "daily", "time": "09:00", "days": ["mon"]},  # daily 仅支持 workday
        {"type": "weekly", "time": "09:00", "days": []},  # weekly 需非空
        {"type": "weekly", "time": "09:00", "days": [7]},
        {"type": "monthly", "time": "09:00", "days": [0]},
        {"type": "monthly", "time": "09:00", "days": [32]},
        {"type": "daily", "time": "09:00", "interval_minutes": 5},  # < 15 分钟
        {"type": "once"},  # 缺 at
        {"type": "once", "at": "不是时间"},
        {"type": "daily", "time": "09:00", "effective_from": "2026-12-31",
         "effective_to": "2026-01-01"},  # 生效期倒挂
    ],
)
def test_schedule_validation_rejects(schedule: dict[str, Any]) -> None:
    """非法调度规则一律 ValueError（PRD 3.6.2 调度表 + 防护栏）。"""
    with pytest.raises(ValueError):
        automation.validate_schedule(schedule)


def test_next_run_time_scenarios() -> None:
    """逐日扫描：workday 跳周末、weekly 周内取次日、monthly 取下月 1 日。"""
    # 基准周六 10:00：workday 任务跳过周末 → 下周一 09:00
    sat = datetime(2026, 9, 19, 10, 0, tzinfo=TZ)
    nxt = automation.next_run_time(
        {"type": "daily", "time": "09:00", "days": ["workday"]}, after=sat
    )
    assert nxt == datetime(2026, 9, 21, 9, 0, tzinfo=TZ)
    # 基准周一 12:00：weekly [0,2,4]（0=周一；当天 08:30 已过）→ 周三 08:30
    mon = datetime(2026, 9, 21, 12, 0, tzinfo=TZ)
    nxt = automation.next_run_time(
        {"type": "weekly", "time": "08:30", "days": [0, 2, 4]}, after=mon
    )
    assert nxt == datetime(2026, 9, 23, 8, 30, tzinfo=TZ)
    # 基准 9-16：monthly [1,15]（9-15 已过）→ 10-01 07:00
    nxt = automation.next_run_time(
        {"type": "monthly", "time": "07:00", "days": [1, 15]},
        after=datetime(2026, 9, 16, 0, 0, tzinfo=TZ),
    )
    assert nxt == datetime(2026, 10, 1, 7, 0, tzinfo=TZ)
    # once 未过期 → 返回该时间；已过期 → None
    future = automation.next_run_time(
        {"type": "once", "at": "2030-01-01T09:00:00"}, after=sat
    )
    assert future == datetime(2030, 1, 1, 9, 0, tzinfo=TZ)
    assert (
        automation.next_run_time({"type": "once", "at": "2020-01-01T09:00:00"}, after=sat)
        is None
    )
    # 生效期结束前无触发日 → None（effective_to 之后不再触发）
    assert (
        automation.next_run_time(
            {"type": "monthly", "time": "07:00", "days": [15], "effective_to": "2026-09-30"},
            after=datetime(2026, 10, 5, 0, 0, tzinfo=TZ),
        )
        is None
    )


# ---- 创建防护栏（PRD 3.6.2）----


async def test_create_rejects_write_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    """write 技能不可自动化：无人值守不写入（PRD 3.6.2 禁 Craft 延伸）。"""
    _inject_registry(monkeypatch, "bulk_write")
    await skill_store.register(name="bulk_write", title="批量写入", rw="write", by="E1001")
    await skill_store.submit("bulk_write", by="E2002")
    await skill_store.review("bulk_write", approve=True, by="SEC001")  # 第一核
    await skill_store.review("bulk_write", approve=True, by="SEC002")  # 双人复核后已上架
    with pytest.raises(ValueError, match="仅只读技能可自动化"):
        await _mk(skill="bulk_write")


async def test_create_rejects_missing_or_unpublished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """技能不存在 / 未上架（draft）均拒绝。"""
    with pytest.raises(ValueError, match="不存在"):
        await _mk(skill="no_such_skill")
    _inject_registry(monkeypatch, "wip_read")  # 有运行时定义，但市场仅 draft
    await skill_store.register(name="wip_read", title="开发中", rw="read", by="E1001")
    with pytest.raises(ValueError, match="未上架"):
        await _mk(skill="wip_read")


async def test_create_rejects_raw_params() -> None:
    """params 必须是 {field: {value, source}} 草稿形态（直跳 validate）。"""
    with pytest.raises(ValueError, match="草稿形态"):
        await _mk(params={"sku": "SKU-001"})


async def test_create_once_expired_rejected() -> None:
    """once 执行时间已过期 → 拒绝（防创建即永不触发的僵尸任务）。"""
    with pytest.raises(ValueError, match="过期"):
        await _mk(schedule={"type": "once", "at": "2020-01-01T09:00:00"})


async def test_create_max_active_per_user() -> None:
    """单用户 active 任务 ≤3；暂停后腾出额度；他人不受影响。"""
    for i in range(3):
        await _mk(name=f"任务{i}")
    with pytest.raises(ValueError, match="最多 3 个活跃任务"):
        await _mk(name="第四个")
    # 他人可继续创建
    await _mk(owner="E2002", name="他人任务")
    # 暂停一个后可再建
    tasks = automation.list_tasks(owner="E1001")
    await automation.pause(tasks[0]["id"], by="E1001")
    await _mk(name="第四个")


# ---- 生命周期 ----


async def test_pause_resume_delete_lifecycle() -> None:
    """pause → paused；resume 重置失败计数；delete 移除；非法迁移 ValueError。"""
    task = await _mk()
    tid = task["id"]
    assert task["status"] == "active"
    paused = await automation.pause(tid, by="E1001")
    assert paused["status"] == "paused" and paused["next_run_at"] is None
    with pytest.raises(ValueError, match="无需暂停"):
        await automation.pause(tid, by="E1001")
    resumed = await automation.resume(tid, by="E1001")
    assert resumed["status"] == "active" and resumed["failure_count"] == 0
    with pytest.raises(ValueError, match="无需恢复"):
        await automation.resume(tid, by="E1001")
    await automation.delete(tid, by="E1001")
    assert automation.detail(tid) is None
    with pytest.raises(ValueError, match="不存在"):
        await automation.delete(tid, by="E1001")


# ---- 执行管线：子图复用 + record_run 回写 ----


async def test_run_once_real_read_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP 桩 + 真实子图：以创建者身份执行只读技能并回写历史/信箱。"""
    from agent_core.pipeline import graph as chat_graph

    async def fake_erp(tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"sku": args.get("sku", "SKU-001"), "qty": 5}]

    monkeypatch.setattr(chat_graph, "call_erp_tool", fake_erp)
    task = await _mk()
    result = await automation.run_once(task["id"])
    assert result is not None and result["ok"] is True
    detail = automation.detail(task["id"])
    assert detail is not None
    assert detail["stats"] == {"runs": 1, "success": 1, "failed": 0}
    assert detail["next_run_at"] is not None  # daily 回写下次触发
    runs = automation.history(task["id"])
    assert len(runs) == 1 and runs[0]["ok"] is True
    assert runs[0]["session_id"] == f"auto_{task['id']}_001"
    msgs = automation.inbox("E1001")
    assert len(msgs) == 1 and msgs[0]["kind"] == "result" and msgs[0]["read"] is False


async def test_run_once_permission_follows_creator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """权限随创建者走：无权限科室的创建者执行 dept_scope 技能 → 计失败。"""
    from agent_core.pipeline import graph as chat_graph

    async def fake_wms(tool: str, args: dict[str, Any]) -> Any:  # pragma: no cover
        raise AssertionError("权限拒绝后不应触达 MCP")

    monkeypatch.setattr(chat_graph, "call_wms_tool", fake_wms)
    # wh_stock_overview 内置基线已上架，dept_scope=仓库物流；创建者科室=生产科
    task = await _mk(skill="wh_stock_overview", params={})
    result = await automation.run_once(task["id"])
    assert result is not None and result["ok"] is False
    detail = automation.detail(task["id"])
    assert detail is not None and detail["stats"]["failed"] == 1


async def test_three_failures_auto_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """连续 3 次失败自动暂停 + auto_paused 信箱通知 + 审计（PRD 3.6.2 防护栏）。"""

    async def always_fail(task: dict[str, Any], session_id: str) -> tuple[bool, str]:
        return False, "下游服务不可用"

    monkeypatch.setattr(auto_store, "_execute_task", always_fail)
    task = await _mk()
    for _ in range(3):
        await automation.run_once(task["id"])
    detail = automation.detail(task["id"])
    assert detail is not None
    assert detail["status"] == "paused" and detail["failure_count"] == 3
    assert detail["stats"]["failed"] == 3 and detail["next_run_at"] is None
    kinds = [m["kind"] for m in automation.inbox("E1001")]
    # inbox 新的在前：第 3 次失败的 result 与 auto_paused 排最前
    assert kinds == ["auto_paused", "result", "result", "result"]
    paused_logs = await audit.recent(action="automation_paused")
    assert len(paused_logs) == 1 and paused_logs[0]["user_id"] == "E1001"
    run_logs = await audit.recent(action="automation_run", user_id="E1001")
    assert len(run_logs) == 3 and all(e["result"] == "failed" for e in run_logs)
    # 暂停后调度触发被跳过（run_once 对非 active 直接返回 None）
    assert await automation.run_once(task["id"]) is None
    assert automation.detail(task["id"])["stats"]["runs"] == 3


async def test_record_run_success_resets_failure_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功一次即清零连续失败计数（失败 2 次 + 成功 → 不触发自动暂停）。"""

    async def fail_then_ok(task: dict[str, Any], session_id: str) -> tuple[bool, str]:
        return (False, "boom") if automation.detail(task["id"])["failure_count"] < 2 else (
            True,
            "恢复",
        )

    monkeypatch.setattr(auto_store, "_execute_task", fail_then_ok)
    task = await _mk()
    await automation.run_once(task["id"])
    await automation.run_once(task["id"])
    result = await automation.run_once(task["id"])
    assert result is not None and result["ok"] is True
    detail = automation.detail(task["id"])
    assert detail is not None
    assert detail["status"] == "active" and detail["failure_count"] == 0


# ---- API 层（TestClient）----


def test_api_requires_auth(client: TestClient) -> None:
    """自动化端点恒需认证（口径对齐市场端点；合法 body 免 422 抢先）。"""
    assert client.get("/automations").status_code == 401
    body = {"name": "x", "skill": "erp_inventory_query", "params": DRAFT,
            "schedule": DAILY}
    assert client.post("/automations", json=body).status_code == 401
    assert client.post("/automations/inbox/read").status_code == 401


def test_api_create_and_list_owner_filter(
    client: TestClient, rsa_key: Any
) -> None:
    """创建任务 → 我的列表；owner 过滤互不可见。"""
    alice = make_token(rsa_key, sub="E1001")
    bob = make_token(rsa_key, sub="E2002")
    body = {"name": "库存播报", "skill": "erp_inventory_query",
            "params": DRAFT, "schedule": DAILY}
    resp = client.post("/automations", json=body, headers=auth(alice))
    assert resp.status_code == 200
    task = resp.json()
    assert task["id"].startswith("auto_")
    assert task["owner"] == "E1001" and task["status"] == "active"
    assert task["owner_auth"] == {"user_id": "E1001", "dept": "事业部A/销售科",
                                  "roles": ["employee"]}
    client.post("/automations", json=body, headers=auth(alice))
    client.post("/automations", json=body, headers=auth(bob))
    mine = client.get("/automations", headers=auth(alice)).json()
    assert mine["count"] == 2 and all(t["owner"] == "E1001" for t in mine["items"])
    bobs = client.get("/automations", headers=auth(bob)).json()
    assert bobs["count"] == 1


def test_api_create_guards_400(
    client: TestClient, rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write 技能 / 未上架技能 / 非法调度 → 400（store 防护栏透出）。"""
    token = make_token(rsa_key)
    # 未注册技能
    resp = client.post(
        "/automations",
        json={"name": "x", "skill": "no_such_skill", "params": DRAFT,
              "schedule": DAILY},
        headers=auth(token),
    )
    assert resp.status_code == 400 and "不存在" in resp.json()["detail"]
    # write 技能（运行时定义注入 + 注册→提交→双人复核发布）
    _inject_registry(monkeypatch, "bulk_write")
    asyncio.run(skill_store.register(name="bulk_write", title="批量写入", rw="write", by="E1001"))
    asyncio.run(skill_store.submit("bulk_write", by="E2002"))
    asyncio.run(skill_store.review("bulk_write", approve=True, by="SEC001"))
    asyncio.run(skill_store.review("bulk_write", approve=True, by="SEC002"))
    resp = client.post(
        "/automations",
        json={"name": "x", "skill": "bulk_write", "params": {}, "schedule": DAILY},
        headers=auth(token),
    )
    assert resp.status_code == 400 and "仅只读技能可自动化" in resp.json()["detail"]
    # 非法调度
    resp = client.post(
        "/automations",
        json={"name": "x", "skill": "erp_inventory_query", "params": DRAFT,
              "schedule": {"type": "event"}},
        headers=auth(token),
    )
    assert resp.status_code == 400


def test_api_ownership_gate(client: TestClient, rsa_key: Any) -> None:
    """非创建者 403（detail/pause/history）；管理员可查；不存在 404。"""
    owner_tok = make_token(rsa_key, sub="E1001")
    other_tok = make_token(rsa_key, sub="E2002")
    mgr_tok = make_token(rsa_key, sub="E3001", roles=["dept_manager"])
    task = client.post(
        "/automations",
        json={"name": "播报", "skill": "erp_inventory_query", "params": DRAFT,
              "schedule": DAILY},
        headers=auth(owner_tok),
    ).json()
    tid = task["id"]
    for path in (f"/automations/{tid}", f"/automations/{tid}/history"):
        assert client.get(path, headers=auth(other_tok)).status_code == 403
    assert client.post(f"/automations/{tid}/pause", headers=auth(other_tok)).status_code == 403
    assert client.get(f"/automations/{tid}", headers=auth(mgr_tok)).status_code == 200
    assert client.get("/automations/auto_999999", headers=auth(owner_tok)).status_code == 404


def test_api_pause_resume_delete_flow(client: TestClient, rsa_key: Any) -> None:
    """暂停 → 恢复 → 删除全流程（API 归属校验 + 状态透出）。"""
    token = make_token(rsa_key)
    task = client.post(
        "/automations",
        json={"name": "播报", "skill": "erp_inventory_query", "params": DRAFT,
              "schedule": DAILY},
        headers=auth(token),
    ).json()
    tid = task["id"]
    paused = client.post(f"/automations/{tid}/pause", headers=auth(token)).json()
    assert paused["status"] == "paused"
    # 暂停态再暂停 → 400（store ValueError 透出）
    assert client.post(f"/automations/{tid}/pause", headers=auth(token)).status_code == 400
    resumed = client.post(f"/automations/{tid}/resume", headers=auth(token)).json()
    assert resumed["status"] == "active" and resumed["failure_count"] == 0
    assert client.delete(f"/automations/{tid}", headers=auth(token)).status_code == 200
    assert client.get(f"/automations/{tid}", headers=auth(token)).status_code == 404
    assert client.get("/automations", headers=auth(token)).json()["count"] == 0


def test_api_history_and_inbox(
    client: TestClient, rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """执行后历史/信箱端点联动 + 已读标记（unread_only 过滤）。"""

    async def fake_ok(task: dict[str, Any], session_id: str) -> tuple[bool, str]:
        return True, "每日库存播报：正常"

    monkeypatch.setattr(auto_store, "_execute_task", fake_ok)
    token = make_token(rsa_key)
    task = client.post(
        "/automations",
        json={"name": "播报", "skill": "erp_inventory_query", "params": DRAFT,
              "schedule": DAILY},
        headers=auth(token),
    ).json()
    tid = task["id"]
    asyncio.run(automation.run_once(tid))
    history = client.get(f"/automations/{tid}/history", headers=auth(token)).json()
    assert history["count"] == 1
    assert history["items"][0]["ok"] is True and history["items"][0]["text"] == "每日库存播报：正常"
    inbox = client.get("/automations/inbox", headers=auth(token)).json()
    assert inbox["count"] == 1 and inbox["items"][0]["task_id"] == tid
    # 他人信箱恒空（信箱按当前用户过滤）
    other = client.get("/automations/inbox",
                       headers=auth(make_token(rsa_key, sub="E2002"))).json()
    assert other["count"] == 0
    # 已读标记：marked=1 → unread_only 为空
    marked = client.post("/automations/inbox/read", headers=auth(token)).json()
    assert marked["marked"] == 1
    unread = client.get("/automations/inbox?unread_only=true", headers=auth(token)).json()
    assert unread["count"] == 0
