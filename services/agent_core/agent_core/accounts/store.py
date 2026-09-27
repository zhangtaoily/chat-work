"""账号 Store：员工目录 + 数字分身开关 + 冻结状态（P0-3）。

定位：agent-core 侧的账号视图（工号 → 姓名/部门/角色/直属上级），
与 mock_idp HR_USERS 同源同种子；生产 P2 切 HR 主数据同步，本模块
只保留运行时开关态（twins/frozen）。

分身模型（P0 设计共识）：分身不是独立账号，而是「账号的 agent 化
执行面」——twin 身份即员工本人身份；关闭分身仅影响 @数字人可达性
（P1 委托会话），不影响本人正常使用。

冻结（管理后台触发，三合一中的 agent 侧）：本模块 frozen 标记 +
api/auth user denylist（token 立即 401）；IdP 侧停用由 admin 路由
调 mock_idp /internal 端点完成。

存储：进程内 dict + 快照（accounts:snapshot：REDIS_URL 配置走
Redis，未配置落本地文件兜底；与 skills/audit 同风格）。
"""

import json
from typing import Any

from agent_core import audit, persist

_SNAPSHOT_KEY = "accounts:snapshot"

# 员工目录（与 mock_idp/users.py 种子一致；manager 为直属上级工号，
# P1 委托会话「领导 @ 分身」的准入依据；E2002-E2005 上级为 dev 占位，
# P2 切 HR 主数据后以同步为准）
_DIRECTORY: dict[str, dict[str, Any]] = {
    "E1001": {"name": "张三", "dept": "事业部A/销售科", "roles": ["employee"], "manager": "E1003"},
    "E1002": {"name": "李四", "dept": "事业部A/销售科", "roles": ["employee"], "manager": "E1003"},
    "E1003": {"name": "王五", "dept": "事业部A/销售科", "roles": ["employee", "dept_manager"], "manager": "E8001"},
    "E2001": {"name": "郑拓", "dept": "事业部A/信息科", "roles": ["employee"], "manager": "E8001"},
    "E2002": {"name": "冯刚", "dept": "事业部A/生产科", "roles": ["employee"], "manager": "E8001"},
    "E2003": {"name": "许芹", "dept": "事业部A/计划科", "roles": ["employee"], "manager": "E8001"},
    "E2004": {"name": "贺平", "dept": "事业部A/品管科", "roles": ["employee"], "manager": "E8001"},
    "E2005": {"name": "鲁仓", "dept": "事业部A/仓库物流", "roles": ["employee"], "manager": "E8001"},
    "E8001": {"name": "系统管理员", "dept": "信息科", "roles": ["system_admin", "org_admin"], "manager": None},
    "E8002": {"name": "安全评审员甲", "dept": "信息科", "roles": ["security_reviewer"], "manager": "E8001"},
    "E8003": {"name": "知识管理员", "dept": "信息科", "roles": ["knowledge_manager"], "manager": "E8001"},
    "E8004": {"name": "审计员", "dept": "信息科", "roles": ["auditor"], "manager": "E8001"},
    "E8005": {"name": "安全评审员乙", "dept": "信息科", "roles": ["security_reviewer"], "manager": "E8001"},
}

_twins: dict[str, bool] = {}  # 缺省 True（分身默认开启）
_frozen: dict[str, bool] = {}  # 缺省 False
# 档案补录（管理后台手工维护；P2 切 hr_sync 后由 HR 主数据权威覆盖）。
# 白名单见 _PROFILE_FIELDS，后续扩字段只加键即可
_profiles: dict[str, dict[str, str]] = {}
_redis_client: Any = None

# 账号档案字段白名单（籍贯/学历/专业/岗位；仅 admin 来源可写）
_PROFILE_FIELDS: tuple[str, ...] = ("native_place", "education", "major", "position")


def _get_redis() -> Any:
    """惰性初始化 Redis（与 skills store 同一单例约定；无 REDIS_URL 返回 None）。"""
    global _redis_client
    if _redis_client is None:
        from agent_core.config import settings

        if settings.redis_url:
            import redis.asyncio as aioredis

            _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def reset() -> None:
    """清空运行时开关态与档案补录（测试隔离用；目录为静态种子不动）。"""
    _twins.clear()
    _frozen.clear()
    _profiles.clear()


async def _snapshot() -> None:
    """写事件后镜像快照（无 Redis 落本地文件兜底）。"""
    payload = {"twins": _twins, "frozen": _frozen, "profiles": _profiles}
    r = _get_redis()
    if r is None:
        await persist.write_json(_SNAPSHOT_KEY, payload)
        return
    await r.set(_SNAPSHOT_KEY, json.dumps(payload, ensure_ascii=False))


async def restore() -> None:
    """启动恢复（API lifespan 调用）：Redis 快照优先，无 Redis 读本地文件。"""
    snap: dict[str, Any] | None = None
    r = _get_redis()
    if r is not None:
        raw = await r.get(_SNAPSHOT_KEY)
        if raw:
            snap = json.loads(raw)
    if snap is None:
        snap = await persist.read_json(_SNAPSHOT_KEY)
    if snap:
        _twins.update({u: bool(v) for u, v in snap.get("twins", {}).items()})
        _frozen.update({u: bool(v) for u, v in snap.get("frozen", {}).items()})
        _profiles.update(
            {u: {k: str(v) for k, v in p.items() if k in _PROFILE_FIELDS} for u, p in snap.get("profiles", {}).items()}
        )


# ---- 查询（同步无 IO）----


def get(emp_no: str) -> dict[str, Any] | None:
    """账号目录条目（副本）。"""
    entry = _DIRECTORY.get(emp_no.strip().upper())
    return dict(entry) if entry else None


def name_of(emp_no: str) -> str | None:
    entry = _DIRECTORY.get(emp_no)
    return entry["name"] if entry else None


def manager_of(emp_no: str) -> str | None:
    """直属上级工号（P1 委托准入依据；无上级/未知账号返回 None）。"""
    entry = _DIRECTORY.get(emp_no)
    return entry.get("manager") if entry else None


def resolve_mention(token: str) -> str | None:
    """@提及解析（P1-2 委托会话）：工号直查 / 姓名反查（目录种子无重名）。"""
    token = (token or "").strip()
    if not token:
        return None
    if token.upper() in _DIRECTORY:
        return token.upper()
    for emp_no, entry in _DIRECTORY.items():
        if entry["name"] == token:
            return emp_no
    return None


def is_twin_enabled(emp_no: str) -> bool:
    """数字分身开关（缺省开启；关闭仅影响 P1 @可达性，不影响本人使用）。"""
    return _twins.get(emp_no, True)


def is_frozen(emp_no: str) -> bool:
    """账号冻结态（管理后台「冻结」动作的 agent 侧标记）。"""
    return _frozen.get(emp_no, False)


def list_accounts() -> list[dict[str, Any]]:
    """账号目录视图（按工号排序，附运行时态与上级姓名）。"""
    items = []
    for emp_no, entry in sorted(_DIRECTORY.items()):
        mgr = entry.get("manager")
        items.append(
            {
                "emp_no": emp_no,
                "name": entry["name"],
                "dept": entry["dept"],
                "roles": list(entry["roles"]),
                "manager": mgr,
                "manager_name": name_of(mgr) if mgr else None,
                "profile": dict(_profiles.get(emp_no, {})),
                "twin_enabled": is_twin_enabled(emp_no),
                "frozen": is_frozen(emp_no),
            }
        )
    return items


# ---- 管理动作（写路径，async 以落快照与审计）----


async def set_twin(emp_no: str, enabled: bool, *, by: str) -> None:
    """数字分身开关（管理后台「数字分身账号」工作台）。"""
    if emp_no not in _DIRECTORY:
        raise ValueError(f"账号 {emp_no} 不存在")
    _twins[emp_no] = enabled
    await audit.record(
        "account_twin",
        tool="accounts_store",
        params={"emp_no": emp_no, "enabled": enabled},
        user_id=by or None,
        result="ok",
        detail=f"{'开启' if enabled else '关闭'} {emp_no} 的数字分身",
    )
    await _snapshot()


async def set_frozen(emp_no: str, frozen: bool, *, by: str) -> None:
    """冻结/解冻（agent 侧标记；token denylist 与 IdP 停用由 admin 路由联动）。"""
    if emp_no not in _DIRECTORY:
        raise ValueError(f"账号 {emp_no} 不存在")
    _frozen[emp_no] = frozen
    await audit.record(
        "account_freeze",
        tool="accounts_store",
        params={"emp_no": emp_no, "frozen": frozen},
        user_id=by or None,
        result="ok",
        detail=f"{'冻结' if frozen else '解冻'}账号 {emp_no}",
    )
    await _snapshot()


def profile_of(emp_no: str) -> dict[str, str]:
    """账号档案（已补录字段副本；未补录返回空 dict）。"""
    return dict(_profiles.get(emp_no, {}))


async def update_profile(emp_no: str, fields: dict[str, str], *, by: str) -> dict[str, str]:
    """档案补录（管理后台「数字分身账号」工作台）：白名单字段整体提交，
    空串=清除该字段；P2 切 hr_sync 后由 HR 主数据权威覆盖本模块补录值。"""
    if emp_no not in _DIRECTORY:
        raise ValueError(f"账号 {emp_no} 不存在")
    clean = {
        k: v.strip()
        for k in _PROFILE_FIELDS
        if (v := fields.get(k, "")) and v.strip()
    }
    if clean:
        _profiles[emp_no] = clean
    else:
        _profiles.pop(emp_no, None)
    await audit.record(
        "account_profile",
        tool="accounts_store",
        params={"emp_no": emp_no, "fields": sorted(clean)},
        user_id=by or None,
        result="ok",
        detail=f"补录 {emp_no} 档案：{'、'.join(sorted(clean)) or '（已清空）'}",
    )
    await _snapshot()
    return dict(clean)
