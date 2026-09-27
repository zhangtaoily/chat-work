"""assignment 任务单 Store（P1-2 @分身委托会话）。

数字分身任务载体：领导 @ 员工数字分身布置工作 → 生成 assignment 任务单；
员工（分身本体）查询并推进自己的任务。v1 形态 = 委托会话 + 任务单
（不做群聊协议，P2 再议 mention 路由）。

准入防线在 store 层（与 automation create 防线同风格，进程内直调不走 MCP）：
- 布置人必须是接收人的直属上级（accounts.manager_of，防越权布置）
- 接收人数字分身已开启（accounts.is_twin_enabled）且未冻结（accounts.is_frozen）

存储：进程内 dict + 快照（assignment_store:snapshot：REDIS_URL 配置走
Redis，未配置落本地文件兜底；与 accounts/skills 同风格）。
"""

import json
from datetime import datetime
from typing import Any

from agent_core import audit, persist
from agent_core.accounts import store as accounts

_SNAPSHOT_KEY = "assignment_store:snapshot"

# 状态机：pending → in_progress → done；未终态可 cancelled（仅布置人可取消）
_TRANSITIONS = {
    ("pending", "in_progress"),
    ("pending", "done"),
    ("in_progress", "done"),
    ("pending", "cancelled"),
    ("in_progress", "cancelled"),
}

_seq = 0
_tasks: dict[str, dict[str, Any]] = {}
_redis_client: Any = None


def _get_redis() -> Any:
    """惰性初始化 Redis（与 accounts/skills store 同一单例约定）。"""
    global _redis_client
    if _redis_client is None:
        from agent_core.config import settings

        if settings.redis_url:
            import redis.asyncio as aioredis

            _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def reset() -> None:
    """清空任务域（测试隔离用）。"""
    global _seq
    _seq = 0
    _tasks.clear()


async def _snapshot() -> None:
    """写事件后镜像快照（无 Redis 落本地文件兜底）。"""
    payload = {"seq": _seq, "tasks": _tasks}
    r = _get_redis()
    if r is None:
        await persist.write_json(_SNAPSHOT_KEY, payload)
        return
    await r.set(_SNAPSHOT_KEY, json.dumps(payload, ensure_ascii=False))


async def restore() -> None:
    """启动恢复（API lifespan 调用）：Redis 快照优先，无 Redis 读本地文件。"""
    global _seq
    snap: dict[str, Any] | None = None
    r = _get_redis()
    if r is not None:
        raw = await r.get(_SNAPSHOT_KEY)
        if raw:
            snap = json.loads(raw)
    if snap is None:
        snap = await persist.read_json(_SNAPSHOT_KEY)
    if snap:
        _seq = int(snap.get("seq", 0))
        _tasks.update(snap.get("tasks", {}))


# ---- 查询（同步无 IO）----


def get(task_id: str) -> dict[str, Any] | None:
    """任务单详情（副本）。"""
    task = _tasks.get(task_id)
    return dict(task) if task else None


def list_for_assignee(emp_no: str, status: str | None = None) -> list[dict[str, Any]]:
    """布置给某人的任务（新任务在前；status 可选过滤）。

    排序键 (created_at, id)：秒级时间戳同秒内按单号倒序，保证新单在前。
    """
    items = [t for t in _tasks.values() if t["assignee"] == emp_no]
    if status:
        items = [t for t in items if t["status"] == status]
    return [dict(t) for t in sorted(items, key=lambda t: (t["created_at"], t["id"]), reverse=True)]


def list_by_assigner(emp_no: str, status: str | None = None) -> list[dict[str, Any]]:
    """某人布置出去的任务（新任务在前；status 可选过滤；排序同上）。"""
    items = [t for t in _tasks.values() if t["assigner"] == emp_no]
    if status:
        items = [t for t in items if t["status"] == status]
    return [dict(t) for t in sorted(items, key=lambda t: (t["created_at"], t["id"]), reverse=True)]


def overview(emp_no: str) -> dict[str, Any]:
    """双视角总览（任务进度技能渲染数据源）：布置给我的 + 我布置的。"""
    return {
        "assigned_to_me": list_for_assignee(emp_no),
        "assigned_by_me": list_by_assigner(emp_no),
    }


def list_all(status: str | None = None) -> list[dict[str, Any]]:
    """全量任务单（管理后台治理视角；新任务在前；status 可选过滤）。"""
    items = list(_tasks.values())
    if status:
        items = [t for t in items if t["status"] == status]
    return [dict(t) for t in sorted(items, key=lambda t: (t["created_at"], t["id"]), reverse=True)]


# ---- 写路径（async 以落快照与审计）----


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


async def create(
    assigner: str,
    assignee: str,
    title: str,
    *,
    detail: str = "",
    deadline: str | None = None,
) -> dict[str, Any]:
    """布置任务（领导 → 员工数字分身）：准入校验失败抛 ValueError。"""
    global _seq
    title = (title or "").strip()
    if not title:
        raise ValueError("任务内容不能为空")
    entry = accounts.get(assignee)
    if entry is None:
        raise ValueError(f"账号 {assignee} 不存在")
    if not accounts.is_twin_enabled(assignee):
        raise ValueError(f"「{entry['name']}」的数字分身未开启，无法接收任务")
    if accounts.is_frozen(assignee):
        raise ValueError(f"「{entry['name']}」的账号已冻结，无法接收任务")
    if accounts.manager_of(assignee) != assigner:
        raise ValueError(
            f"仅直属上级可以布置任务：你需是「{entry['name']}」的直属上级"
        )
    _seq += 1
    task = {
        "id": f"ASSIGN-{_seq:04d}",
        "assigner": assigner,
        "assigner_name": accounts.name_of(assigner) or assigner,
        "assignee": assignee,
        "assignee_name": entry["name"],
        "title": title,
        "detail": detail,
        "deadline": deadline,
        "status": "pending",
        "created_at": _now(),
        "updated_at": _now(),
        "done_note": "",
    }
    _tasks[task["id"]] = task
    await audit.record(
        "assignment_create",
        tool="assignments_store",
        params={"task_id": task["id"], "assignee": assignee, "title": title},
        user_id=assigner or None,
        result="ok",
        detail=f"{task['assigner_name']} 向 {entry['name']} 的数字分身布置任务",
    )
    await _snapshot()
    return dict(task)


async def update_status(
    task_id: str, status: str, *, by: str, note: str = "", as_admin: bool = False
) -> dict[str, Any]:
    """推进任务状态：接收人可 in_progress/done，布置人可 cancelled；
    as_admin=True 为管理后台治理动作（取消任意未终态任务单，仍受状态机
    约束，归属校验放宽为管理角色，PRD 5.6.2 同 automation 治理口径）。"""
    task = _tasks.get(task_id)
    if task is None:
        raise ValueError(f"任务单 {task_id} 不存在")
    if (task["status"], status) not in _TRANSITIONS:
        raise ValueError(f"任务状态不允许从 {task['status']} 变更为 {status}")
    if not as_admin:
        if status == "cancelled":
            if task["assigner"] != by:
                raise ValueError("仅布置人可以取消任务")
        elif task["assignee"] != by:
            raise ValueError("仅任务接收人可以推进任务状态")
    task["status"] = status
    if note:
        task["done_note"] = note
    task["updated_at"] = _now()
    await audit.record(
        "assignment_status",
        tool="assignments_store",
        params={"task_id": task_id, "status": status, "note": note},
        user_id=by or None,
        result="ok",
        detail=f"任务 {task_id} 状态更新为 {status}",
    )
    await _snapshot()
    return dict(task)
