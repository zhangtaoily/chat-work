"""Automation Store：自动化任务域模型与调度执行（PLAN P2.4，PRD 3.6）。

定位：定时/触发式执行的任务域模型 + apscheduler 调度封装 + 执行管线
（「先跑通再自动化」的自动化半边，PRD 3.6.2 上线流程）。ARCHITECTURE
4.8 automation 包的任务存储/调度半边；试运行 HITL/模板库留后续。

MVP 范围（用户确认）：
- 后端自动化闭环：任务 CRUD + 进程内调度 + 以创建者身份执行（复用
  graph 权限管线）+ 执行历史 + 结果通知双通道（PLAN P2.6 p2-6e，
  PRD 14 章：wecom_bot 企微群机器人 + 进程内信箱降级）
- 仅只读技能可自动化：绑定 write 技能创建任务直接拒绝——无人值守
  不写入（PRD 3.6.2 禁 Craft 的延伸），HITL 确认卡无人处理会挂起
- 事件触发（PLAN P2.6 p2-6d，PRD 3.6.1）：订阅系统事件名（如
  workflow.run.finished），事件经手动 fire API 或系统内部 publish 入队，
  轮询检查器消费并执行匹配任务；节流窗口 interval_minutes 兜底
  （默认 15 分钟，同 PRD 3.6.2 防护栏），手动 fire 仅命中本人任务

任务结构（PRD 3.6.2，MVP 简化为单调度规则）：
    {id: auto_{seq:06d}, name, skill, params(draft 形态，直跳 validate),
     schedule{type, days, time/at, effective_from, effective_to},
     channel{push, format}, perm_mode, owner, owner_auth 快照,
     scope, status, failure_count, next_run_at, stats, 时间戳}

调度规则（PRD 3.6.2 调度表）：daily（days=["workday"] 仅工作日）/
weekly（days 0-6，0=周一）/ monthly（days 1-31）/ once（at ISO
datetime）/ event（event_name 订阅事件 + interval_minutes 节流窗口，
PLAN P2.6 p2-6d）。

防护栏（PRD 3.6.2）：单任务最小触发间隔 15 分钟（显式 interval_minutes
校验，四种调度类型天然 ≥1 天）；单用户 active 任务 ≤3；单次执行超时
30 分钟终止；失败不自动重试，连续 3 次失败自动暂停并通知创建者。

执行身份：以创建者本人身份执行（权限随人走，PRD 3.6.2）——创建时
快照 owner 的 auth 子集（user_id/dept/roles），执行时注入 ChatState
跑 validate→permission→hitl→execute→format 子图（跳过 intent/route/
extract：params 已是 draft 形态；只读技能 hitl 直通，permission 复用
角色/区域/科室校验管线）。

存储：进程内 dict/list + 快照（automation:snapshot：REDIS_URL 配置走
Redis，未配置落本地文件 data/snapshots/ 兜底；写事件镜像、restore 恢复，
历史/信箱截尾保留最近 500 条）——与 skills/store 同风格。
"""

import asyncio
import json
import re
import time
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agent_core import audit, persist, wecom

_SNAPSHOT_KEY = "automation:snapshot"

_TZ = ZoneInfo("Asia/Shanghai")  # 调度语义为北京时间（每日 09:00 等）
_STATUSES = ("active", "paused")
_SCHEDULE_TYPES = ("daily", "weekly", "monthly", "once", "event")
_POLL_INTERVAL_S = 60  # event 轮询检查器周期（PLAN P2.6 p2-6d）
_MIN_INTERVAL_MIN = 15  # 防护栏：单任务最小触发间隔
_MAX_ACTIVE_PER_USER = 3  # 防护栏：单用户同时活跃任务上限
_MAX_FAILURES = 3  # 防护栏：连续失败自动暂停阈值
_EXEC_TIMEOUT_S = 30 * 60  # 防护栏：单次执行超时终止
_HISTORY_KEEP = 500  # 快照截尾：历史/信箱保留条数
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_WEEKDAYS = frozenset(range(7))  # 0=周一 … 6=周日
_MONTH_DAYS = frozenset(range(1, 32))

_tasks: dict[str, dict[str, Any]] = {}
_history: list[dict[str, Any]] = []
_inbox: list[dict[str, Any]] = []
_pending_events: list[dict[str, Any]] = []  # 待消费事件队列（瞬时，不入快照）
_event_lock = asyncio.Lock()  # 事件消费互斥（手动 fire 与轮询检查器并发）
_seq = 0  # 任务号自增（auto-*）
_msg_seq = 0  # 信箱消息号自增
_redis_client: Any = None
_scheduler: Any = None
_graph: Any = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _get_redis() -> Any:
    """惰性初始化 Redis（与 audit/slots/skill store 同一单例约定）。"""
    global _redis_client
    if _redis_client is None:
        from agent_core.config import settings

        if settings.redis_url:
            import redis.asyncio as aioredis

            _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


async def _snapshot() -> None:
    """写事件后镜像快照（无 Redis 落本地文件兜底；历史/信箱截尾防膨胀）。"""
    payload = {
        "tasks": _tasks,
        "history": _history[-_HISTORY_KEEP:],
        "inbox": _inbox[-_HISTORY_KEEP:],
        "seq": _seq,
        "msg_seq": _msg_seq,
    }
    r = _get_redis()
    if r is None:
        await persist.write_json(_SNAPSHOT_KEY, payload)
        return
    await r.set(_SNAPSHOT_KEY, json.dumps(payload, ensure_ascii=False))


async def restore() -> None:
    """启动恢复：Redis 快照优先，无 Redis 读本地文件兜底（API lifespan 调用）。"""
    global _seq, _msg_seq
    snap: dict[str, Any] | None = None
    r = _get_redis()
    if r is not None:
        raw = await r.get(_SNAPSHOT_KEY)
        if raw:
            snap = json.loads(raw)
    if snap is None:
        snap = await persist.read_json(_SNAPSHOT_KEY)
    if snap:
        _tasks.update(snap.get("tasks", {}))
        _history.extend(snap.get("history", []))
        _inbox.extend(snap.get("inbox", []))
        _seq = int(snap.get("seq", 0))
        _msg_seq = int(snap.get("msg_seq", 0))


def reset() -> None:
    """清空全部任务/历史/信箱/待消费事件（测试隔离用）。"""
    global _seq, _msg_seq
    _tasks.clear()
    _history.clear()
    _inbox.clear()
    _pending_events.clear()
    _seq = 0
    _msg_seq = 0


# ---- 调度规则：校验与下次触发计算（纯函数，可单测）----


def validate_schedule(schedule: dict[str, Any]) -> None:
    """调度规则合法性（PRD 3.6.2 调度表 + 防护栏），非法 raise ValueError。"""
    if not isinstance(schedule, dict):
        raise ValueError("schedule 必须是对象")  # noqa: TRY004 - 统一 ValueError→API 400
    stype = schedule.get("type")
    if stype not in _SCHEDULE_TYPES:
        raise ValueError(f"schedule.type 仅支持 {'/'.join(_SCHEDULE_TYPES)}")
    if stype == "once":
        at = schedule.get("at")
        if not at:
            raise ValueError("once 调度必须提供 at（ISO 时间）")
        try:
            datetime.fromisoformat(str(at))
        except ValueError as exc:
            raise ValueError(f"at 不是合法时间：{at}") from exc
    elif stype == "event":
        if not str(schedule.get("event_name") or "").strip():
            raise ValueError("event 调度必须提供 event_name（订阅的系统事件名）")
    else:
        t = schedule.get("time")
        if not t or not _TIME_RE.match(str(t)):
            raise ValueError("time 必须是 HH:MM 格式")
        raw_days = schedule.get("days") or []
        if stype == "daily":
            if raw_days and list(raw_days) != ["workday"]:
                raise ValueError('daily 的 days 仅支持空（每天）或 ["workday"]（仅工作日）')
        else:
            try:
                days = {int(d) for d in raw_days}
            except (TypeError, ValueError) as exc:
                raise ValueError("days 必须是数字列表") from exc
            if stype == "weekly":
                if not days or not days <= _WEEKDAYS:
                    raise ValueError("weekly 的 days 必须是 0-6（0=周一）的非空子集")
            else:  # monthly
                if not days or not days <= _MONTH_DAYS:
                    raise ValueError("monthly 的 days 必须是 1-31 的非空子集")
    interval = schedule.get("interval_minutes")
    if interval is not None and int(interval) < _MIN_INTERVAL_MIN:
        raise ValueError(f"触发间隔不得小于 {_MIN_INTERVAL_MIN} 分钟（PRD 3.6.2 防护栏）")
    for label in ("effective_from", "effective_to"):
        v = schedule.get(label)
        if v:
            try:
                date.fromisoformat(str(v))
            except ValueError as exc:
                raise ValueError(f"{label} 必须是 YYYY-MM-DD") from exc
    eff_from = schedule.get("effective_from")
    eff_to = schedule.get("effective_to")
    if eff_from and eff_to and str(eff_from) > str(eff_to):
        raise ValueError("effective_from 不得晚于 effective_to")


def next_run_time(
    schedule: dict[str, Any], *, after: datetime | None = None
) -> datetime | None:
    """下次触发时间（北京时间；once 已过期/超出生效期返回 None）。"""
    stype = schedule["type"]
    base = after or datetime.now(_TZ)
    if base.tzinfo is None:
        base = base.replace(tzinfo=_TZ)
    else:
        base = base.astimezone(_TZ)
    if stype == "event":
        return None  # 事件驱动无固定触发时间（next_run_at 恒为 None）
    if stype == "once":
        at = datetime.fromisoformat(str(schedule["at"]))
        if at.tzinfo is None:
            at = at.replace(tzinfo=_TZ)
        return at.astimezone(_TZ) if at > base else None
    hour, minute = (int(p) for p in str(schedule["time"]).split(":"))
    days = schedule.get("days") or []
    workday_only = stype == "daily" and list(days) == ["workday"]
    weekday_set = {int(d) for d in days} if stype == "weekly" else None
    month_set = {int(d) for d in days} if stype == "monthly" else None
    start_date = (
        date.fromisoformat(str(schedule["effective_from"]))
        if schedule.get("effective_from")
        else None
    )
    end_date = (
        date.fromisoformat(str(schedule["effective_to"]))
        if schedule.get("effective_to")
        else None
    )
    day0 = base.date()
    for offset in range(367):  # 逐日扫描上限一年（含闰年余量）
        d = day0 + timedelta(days=offset)
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            return None
        if workday_only and d.weekday() >= 5:
            continue
        if weekday_set is not None and d.weekday() not in weekday_set:
            continue
        if month_set is not None and d.day not in month_set:
            continue
        cand = datetime(d.year, d.month, d.day, hour, minute, tzinfo=_TZ)
        if cand > base:
            return cand
    return None


def _in_effective_range(schedule: dict[str, Any]) -> bool:
    """执行日是否在生效期内（job 触发后的防御性校验，PRD 3.6.2 effectiveRange）。"""
    today = datetime.now(_TZ).date()
    start = schedule.get("effective_from")
    end = schedule.get("effective_to")
    if start and today < date.fromisoformat(str(start)):
        return False
    return not (end and today > date.fromisoformat(str(end)))


# ---- 查询（API 消费，同步无 IO）----


def list_tasks(*, owner: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    """任务列表（新任务在前；owner/status 可选过滤）。"""
    items = [
        dict(t)
        for t in _tasks.values()
        if (owner is None or t["owner"] == owner) and (status is None or t["status"] == status)
    ]
    return sorted(items, key=lambda t: t["id"], reverse=True)


def detail(task_id: str) -> dict[str, Any] | None:
    """任务详情（不存在返回 None）。"""
    task = _tasks.get(task_id)
    return dict(task) if task else None


def history(task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """执行历史（新的在前；任务删除后仍可查，PRD 10 审计覆盖口径）。"""
    rows = [dict(h) for h in _history if h["task_id"] == task_id]
    return list(reversed(rows))[:limit]


def inbox(user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """结果信箱（新的在前；含执行结果与自动暂停通知）。"""
    rows = [dict(m) for m in _inbox if m["user_id"] == user_id]
    return list(reversed(rows))[:limit]


def mark_inbox_read(user_id: str) -> int:
    """全部已读（返回标记条数）。"""
    n = 0
    for m in _inbox:
        if m["user_id"] == user_id and not m["read"]:
            m["read"] = True
            n += 1
    return n


# ---- 任务生命周期（写路径，async 以落 Redis 快照 + 审计）----


def _require(task_id: str) -> dict[str, Any]:
    task = _tasks.get(task_id)
    if task is None:
        raise ValueError(f"任务 {task_id} 不存在")
    return task


async def create(
    *,
    name: str,
    skill: str,
    params: dict[str, Any],
    schedule: dict[str, Any],
    owner: str,
    owner_auth: dict[str, Any],
    channel: dict[str, Any] | None = None,
    scope: str = "personal",
) -> dict[str, Any]:
    """创建自动化任务（PRD 3.6.2）：防线校验 → 注册 job → 审计/快照。

    防线：技能存在且已上架；write 技能直接拒绝（无人值守不写入）；
    调度规则合法；推送渠道合法（inbox/wecom_bot）；once 时间不得已过期；
    单用户 active 任务 ≤3。
    """
    if not str(name).strip():
        raise ValueError("任务名称不能为空")
    from agent_core.skills import store as skill_store
    from agent_core.skills.registry import get_skill

    meta = skill_store.detail(skill)
    if meta is None or get_skill(skill) is None:
        raise ValueError(f"技能 {skill} 不存在")
    if meta["status"] != "published":
        raise ValueError(f"技能 {skill} 未上架（当前 {meta['status']}），不可自动化")
    if meta["rw"] == "write":
        raise ValueError("仅只读技能可自动化（无人值守不写入，PRD 3.6.2）")
    if not isinstance(params, dict) or any(
        not isinstance(v, dict) or "value" not in v for v in params.values()
    ):
        raise ValueError("params 必须是 {field: {value, source}} 草稿形态")
    validate_schedule(schedule)
    push = (channel or {}).get("push", "inbox")
    if push not in ("inbox", "wecom_bot"):
        raise ValueError(f"push 渠道仅支持 inbox/wecom_bot（当前 {push}）")
    if schedule["type"] == "once" and next_run_time(schedule) is None:
        raise ValueError("once 执行时间已过期")
    active = [t for t in _tasks.values() if t["owner"] == owner and t["status"] == "active"]
    if len(active) >= _MAX_ACTIVE_PER_USER:
        raise ValueError(f"每人同时最多 {_MAX_ACTIVE_PER_USER} 个活跃任务（PRD 3.6.2 防护栏）")
    global _seq
    _seq += 1
    task: dict[str, Any] = {
        "id": f"auto_{_seq:06d}",
        "name": str(name).strip(),
        "skill": skill,
        "params": deepcopy(params),
        "schedule": deepcopy(schedule),
        # push 渠道（PLAN P2.6 p2-6e，PRD 14 章）：inbox 信箱 / wecom_bot
        # 企微群机器人（WECOM_WEBHOOK_URL 未配置时 _deliver 自动降级信箱）
        "channel": channel or {"push": "inbox", "format": "markdown"},
        "perm_mode": "ask",
        "owner": owner,
        "owner_auth": {k: owner_auth.get(k) for k in ("user_id", "dept", "roles")},
        "scope": scope,
        "status": "active",
        "failure_count": 0,
        "created_at": _now(),
        "last_run_at": None,
        "next_run_at": None,
        "stats": {"runs": 0, "success": 0, "failed": 0},
    }
    _tasks[task["id"]] = task
    _sync_job(task)
    await audit.record(
        "automation_create",
        tool="automation",
        params={"task_id": task["id"], "skill": skill, "schedule_type": schedule["type"]},
        user_id=owner,
        result="ok",
        detail=f"创建自动化任务「{task['name']}」",
    )
    await _snapshot()
    return dict(task)


async def pause(task_id: str, *, by: str) -> dict[str, Any]:
    """暂停任务（owner 本人或管理员，归属校验在 API 层）。"""
    task = _require(task_id)
    if task["status"] != "active":
        raise ValueError(f"任务当前状态 {task['status']}，无需暂停")
    task["status"] = "paused"
    task["next_run_at"] = None
    _remove_job(task_id)
    await audit.record(
        "automation_pause",
        tool="automation",
        params={"task_id": task_id, "skill": task["skill"]},
        user_id=by,
        result="ok",
        detail=f"暂停自动化任务「{task['name']}」",
    )
    await _snapshot()
    return dict(task)


async def resume(task_id: str, *, by: str) -> dict[str, Any]:
    """恢复任务：重置失败计数并重新注册调度。"""
    task = _require(task_id)
    if task["status"] != "paused":
        raise ValueError(f"任务当前状态 {task['status']}，无需恢复")
    task["status"] = "active"
    task["failure_count"] = 0
    _sync_job(task)
    await audit.record(
        "automation_resume",
        tool="automation",
        params={"task_id": task_id, "skill": task["skill"]},
        user_id=by,
        result="ok",
        detail=f"恢复自动化任务「{task['name']}」",
    )
    await _snapshot()
    return dict(task)


async def delete(task_id: str, *, by: str) -> None:
    """删除任务（执行历史保留，PRD 10 审计覆盖口径）。"""
    task = _require(task_id)
    _remove_job(task_id)
    del _tasks[task_id]
    await audit.record(
        "automation_delete",
        tool="automation",
        params={"task_id": task_id, "skill": task["skill"]},
        user_id=by,
        result="ok",
        detail=f"删除自动化任务「{task['name']}」",
    )
    await _snapshot()


# ---- 执行管线：子图复用 + 调度 job ----


def _automation_graph() -> Any:
    """只读执行子图：validate → permission → hitl → execute → format。

    自动化跳过 intent/route/extract（params 已是 draft 形态，等价
    build_resume_graph 从中间起跑的先例）；只读技能 hitl 直通，
    permission 复用角色/区域/科室校验（权限随创建者走）。
    """
    global _graph
    if _graph is None:
        from langgraph.graph import END, START, StateGraph

        from agent_core.pipeline import graph as chat_graph

        g = StateGraph(chat_graph.ChatState)
        g.add_node("validate", chat_graph.validate_node)
        g.add_node("permission", chat_graph.permission_node)
        g.add_node("hitl", chat_graph.hitl_node)
        g.add_node("execute", chat_graph.execute_node)
        g.add_node("format", chat_graph.format_node)
        g.add_edge(START, "validate")
        g.add_edge("validate", "permission")
        g.add_edge("permission", "hitl")
        g.add_edge("hitl", "execute")
        g.add_edge("execute", "format")
        g.add_edge("format", END)
        _graph = g.compile()
    return _graph


async def _execute_task(task: dict[str, Any], session_id: str) -> tuple[bool, str]:
    """以创建者身份跑一次只读技能，返回 (是否成功, 结果文本)。"""
    from agent_core.skills.registry import get_skill

    skill = get_skill(task["skill"])
    if skill is None:
        return False, f"技能 {task['skill']} 已不存在"
    state: dict[str, Any] = {
        "auth": dict(task.get("owner_auth") or {}),
        "user_id": task["owner"],
        "session_id": session_id,
        "message": f"自动化任务「{task['name']}」",
        "skill": skill,
        "draft": deepcopy(task["params"]),
    }
    final_state = await _automation_graph().ainvoke(state)
    validation = final_state.get("validation") or {}
    result = final_state.get("tool_result")
    final = final_state.get("final") or {}
    text = str(final.get("text") or "（无输出）")
    if not validation.get("passed", True):
        return False, text  # 参数校验/权限拒绝（permission 拒绝置 passed=False）
    if isinstance(result, dict) and "error" in result:
        return False, text  # MCP 报错（与 format 节点使用统计口径一致）
    return True, text


async def run_once(task_id: str) -> dict[str, Any] | None:
    """执行一次任务（调度 job 与手动触发/测试的共同入口）。

    防护栏：非 active 或不在生效期跳过；单次执行 30 分钟超时终止；
    结果经 record_run 回写历史/信箱/失败计数。
    """
    task = _tasks.get(task_id)
    if task is None or task["status"] != "active":
        return None
    if not _in_effective_range(task["schedule"]):
        return None
    started = time.monotonic()
    run_seq = int(task["stats"]["runs"]) + 1
    session_id = f"auto_{task_id}_{run_seq:03d}"
    ok, text = False, "执行异常"
    try:
        ok, text = await asyncio.wait_for(_execute_task(task, session_id), _EXEC_TIMEOUT_S)
    except TimeoutError:
        ok, text = False, "执行超时（30 分钟上限，已终止）"
    except Exception as exc:  # noqa: BLE001 - 执行器兜底：任何异常都计一次失败
        ok, text = False, f"执行异常：{exc}"
    duration_ms = int((time.monotonic() - started) * 1000)
    await record_run(task_id, ok=ok, text=text, duration_ms=duration_ms, session_id=session_id)
    return {"ok": ok, "text": text, "duration_ms": duration_ms}


async def record_run(
    task_id: str, *, ok: bool, text: str, duration_ms: int = 0, session_id: str = ""
) -> dict[str, Any] | None:
    """回写一次执行结果：历史 + 信箱 + 失败计数 + 审计 + 快照。

    连续失败达 _MAX_FAILURES → 自动暂停 + 信箱通知创建者（PRD 3.6.2
    防护栏；失败不自动重试）。
    """
    task = _tasks.get(task_id)
    if task is None:
        return None
    now = _now()
    task["last_run_at"] = now
    task["stats"]["runs"] += 1
    task["stats"]["success" if ok else "failed"] += 1
    _history.append(
        {
            "task_id": task_id,
            "run_at": now,
            "ok": ok,
            "text": (text or "")[:2000],
            "duration_ms": duration_ms,
            "session_id": session_id,
        }
    )
    await _deliver(task, kind="result", ok=ok, text=text)
    paused = False
    if ok:
        task["failure_count"] = 0
    else:
        task["failure_count"] += 1
        if task["failure_count"] >= _MAX_FAILURES and task["status"] == "active":
            task["status"] = "paused"
            task["next_run_at"] = None
            _remove_job(task_id)
            paused = True
            await _deliver(
                task,
                kind="auto_paused",
                ok=False,
                text=(
                    f"自动化任务「{task['name']}」连续 {_MAX_FAILURES} 次失败，"
                    "已自动暂停，请排查后手动恢复。"
                ),
            )
            await audit.record(
                "automation_paused",
                tool="automation",
                params={
                    "task_id": task_id,
                    "skill": task["skill"],
                    "failures": task["failure_count"],
                },
                user_id=task["owner"],
                result="ok",
                detail=f"任务连续 {task['failure_count']} 次失败，自动暂停",
            )
    await audit.record(
        "automation_run",
        tool="automation",
        params={"task_id": task_id, "skill": task["skill"], "ok": ok},
        user_id=task["owner"],
        session_id=session_id or None,
        result="ok" if ok else "failed",
        detail=f"自动化任务「{task['name']}」{'执行成功' if ok else '执行失败'}",
    )
    if task["status"] == "active" and task["schedule"]["type"] != "once":
        nxt = next_run_time(task["schedule"])
        task["next_run_at"] = nxt.isoformat(timespec="seconds") if nxt else None
    else:
        task["next_run_at"] = None
    await _snapshot()
    return {"ok": ok, "paused": paused}


async def _deliver(task: dict[str, Any], *, kind: str, ok: bool, text: str) -> None:
    """投递通知（创建者收件）。

    双通道（PLAN P2.6 p2-6e，PRD 14 章）：channel.push 为 wecom_bot 且
    WECOM_WEBHOOK_URL 已配置时推企微群机器人；未配置或推送失败降级
    进程内信箱（零依赖默认值，通知不丢）。
    """
    if (task.get("channel") or {}).get("push") == "wecom_bot" and await wecom.push_markdown(text):
        return
    global _msg_seq
    _msg_seq += 1
    _inbox.append(
        {
            "id": f"auto-msg-{_msg_seq:06d}",
            "kind": kind,
            "task_id": task["id"],
            "task_name": task["name"],
            "skill": task["skill"],
            "user_id": task["owner"],
            "ok": ok,
            "text": (text or "")[:2000],
            "run_at": _now(),
            "read": False,
        }
    )


# ---- 事件触发（PLAN P2.6 p2-6d，PRD 3.6.1：订阅系统事件 → 触发技能）----


def publish_event(event_name: str, *, payload: dict[str, Any] | None = None) -> None:
    """系统内部发布事件（workflow 终态等）：入队由轮询检查器消费。

    同步无 IO（仅 append），事件源调用方不应被阻塞；队列瞬时
    （不入快照，进程重启丢失可接受，与调度器进程内语义一致）。
    """
    name = str(event_name).strip()
    if name:
        _pending_events.append(
            {"event": name, "payload": dict(payload or {}), "fired_at": _now(), "by": None}
        )


async def fire_event(
    event_name: str,
    *,
    payload: dict[str, Any] | None = None,
    by: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """手动触发事件（API fire 入口）：立即消费并执行匹配任务。

    owner 限定命中范围（普通用户仅自己的任务，管理员传 None 触发
    全部）——事件广播语义下防止普通用户触发他人任务；系统内部事件
    （publish_event 入队）不受此限。
    """
    name = str(event_name).strip()
    if not name:
        raise ValueError("event 不能为空")
    evt = {"event": name, "payload": dict(payload or {}), "fired_at": _now(), "by": by}
    matched = await _consume_events([evt], owner=owner)
    await audit.record(
        "automation_event_fired",
        tool="automation",
        params={"event": name, "matched": [t["id"] for t in matched]},
        user_id=by,
        result="ok",
        detail=f"手动触发事件「{name}」，命中 {len(matched)} 个任务",
    )
    return {
        "event": name,
        "matched": [{"id": t["id"], "name": t["name"], "skill": t["skill"]} for t in matched],
    }


async def _poll_events() -> None:
    """轮询检查器（apscheduler interval job）：消费待处理事件队列。"""
    if not _pending_events:
        return
    async with _event_lock:
        events, _pending_events[:] = _pending_events[:], []  # 原子取出
    if events:
        await _consume_events(events, owner=None)


async def _consume_events(
    events: list[dict[str, Any]], *, owner: str | None
) -> list[dict[str, Any]]:
    """事件匹配 → 节流检查 → 执行（fire 与轮询检查器的共同入口）。

    节流（PRD 3.6.2 防护栏）：距上次执行不足 interval_minutes（默认
    15 分钟）跳过并审计；同一事件批量只执行一次（去重）。
    """
    matched: list[dict[str, Any]] = []
    for task in _tasks.values():
        if task["status"] != "active" or task["schedule"]["type"] != "event":
            continue
        if owner is not None and task["owner"] != owner:
            continue
        if task["schedule"]["event_name"] not in {str(e.get("event")) for e in events}:
            continue
        if not _in_effective_range(task["schedule"]):
            continue
        window = int(task["schedule"].get("interval_minutes") or _MIN_INTERVAL_MIN)
        last = task.get("last_run_at")
        if last:
            try:
                elapsed_min = (
                    datetime.now(UTC) - datetime.fromisoformat(str(last))
                ).total_seconds() / 60
            except ValueError:
                elapsed_min = None
            if elapsed_min is not None and elapsed_min < window:
                await audit.record(
                    "automation_event_throttled",
                    tool="automation",
                    params={"task_id": task["id"], "event": task["schedule"]["event_name"]},
                    user_id=task["owner"],
                    result="skipped",
                    detail=f"距上次执行 {elapsed_min:.0f} 分钟，不足节流窗口 {window} 分钟，跳过",
                )
                continue
        matched.append(task)
        await run_once(task["id"])
    return matched


# ---- apscheduler 封装（AsyncIOScheduler 由 API lifespan 启停）----


def _job_trigger(schedule: dict[str, Any]) -> Any:
    """调度规则 → apscheduler trigger（daily/weekly/monthly 用 cron）。"""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger

    if schedule["type"] == "once":
        at = datetime.fromisoformat(str(schedule["at"]))
        if at.tzinfo is None:
            at = at.replace(tzinfo=_TZ)
        return DateTrigger(run_date=at.astimezone(_TZ))
    hour, minute = (int(p) for p in str(schedule["time"]).split(":"))
    kwargs: dict[str, Any] = {"hour": hour, "minute": minute, "timezone": str(_TZ)}
    days = schedule.get("days") or []
    if schedule["type"] == "daily":
        if list(days) == ["workday"]:
            kwargs["day_of_week"] = "mon-fri"
    elif schedule["type"] == "weekly":
        kwargs["day_of_week"] = ",".join(str(int(d)) for d in days)  # 0=周一，aps 口径一致
    else:  # monthly
        kwargs["day"] = ",".join(str(int(d)) for d in days)
    return CronTrigger(**kwargs)


def _sync_job(task: dict[str, Any]) -> None:
    """按任务当前状态同步 job：active 且有下次触发 → 注册，否则移除。"""
    if _scheduler is None:
        # 调度器未启动（syscfg 功能开关 automation.scheduler_enabled 关闭，
        # PLAN P2.7）：不挂 job，重开开关时 start_scheduler 统一补挂
        return
    _remove_job(task["id"])
    if task["status"] != "active":
        return
    nxt = next_run_time(task["schedule"])
    if nxt is None:
        return
    _scheduler.add_job(
        run_once,
        _job_trigger(task["schedule"]),
        args=[task["id"]],
        id=task["id"],
        name=f"automation:{task['name']}",
        replace_existing=True,
        max_instances=1,  # 同一任务不并发执行
        coalesce=True,  # 积压多次触发合并为一次
        misfire_grace_time=300,
    )
    task["next_run_at"] = nxt.isoformat(timespec="seconds")


def _remove_job(task_id: str) -> None:
    if _scheduler is not None and _scheduler.get_job(task_id) is not None:
        _scheduler.remove_job(task_id)


def start_scheduler() -> None:
    """API lifespan 启动调度器并按当前任务集注册 job（restore 之后调用）。

    受 syscfg 功能开关 automation.scheduler_enabled 控制（PLAN P2.7）：
    关闭时不启动，syscfg.set_toggle 重开时由此入口补启。
    """
    global _scheduler
    if _scheduler is not None:
        return
    from agent_core import syscfg

    if not syscfg.get_toggle("automation.scheduler_enabled"):
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    sched = AsyncIOScheduler(timezone=str(_TZ))
    sched.start()
    # event 轮询检查器（PLAN P2.6 p2-6d）：周期消费 publish 入队的系统事件
    sched.add_job(
        _poll_events,
        IntervalTrigger(seconds=_POLL_INTERVAL_S),
        id="__event_poll__",
        name="automation:event_poll",
        max_instances=1,
        coalesce=True,
    )
    _scheduler = sched
    for task in _tasks.values():
        _sync_job(task)


def stop_scheduler() -> None:
    """API lifespan 关闭调度器（测试隔离用）。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
