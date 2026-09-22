"""系统配置域：功能开关 / MCP 注册与健康探测 / 模型路由只读（PLAN P2.7，PRD 5.6）。

三块能力（系统配置工作台后端）：
1. 功能开关（syscfg_toggle 审计）：已知键白名单；automation.scheduler_enabled
   实接线——关闭时 lifespan 不启动调度器、新建任务不挂 job，set_toggle 即时
   启停（懒导入 automation 防循环依赖）。
2. MCP 服务：env 五个 MCP 地址构成内置注册表，register_mcp 允许管理员
   追加/改址（builtin 标记保留）；probe_mcp 并发 GET {url}/health 探活
   （超时 3s，上报时延与健康状态）。
3. 模型路由：llm/embedding 配置只读视图（api_key 打码），改模型走
   env/配置中心重启生效，不提供网页写路径（PRD 5.6 只读陈列）。

存储：进程内 dict + 可选 Redis 快照（syscfg:snapshot，写事件时镜像、
restore 恢复）——与 skills/audit 同风格；开关变更与 MCP 注册均落审计
（"审计者也被审计"，PRD 5.6.4）。
"""

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any

from agent_core import audit

_SNAPSHOT_KEY = "syscfg:snapshot"

# 功能开关白名单（key → 说明；新增开关需同步接实线）
_KNOWN_TOGGLES: dict[str, str] = {
    "automation.scheduler_enabled": "自动化任务调度器总开关（关闭后新建任务不调度、已有任务暂停触发）",
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

_toggles: dict[str, bool] = {k: True for k in _KNOWN_TOGGLES}
_mcp_servers: dict[str, dict[str, Any]] = {}
_redis_client: Any = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _get_redis() -> Any:
    """惰性初始化 Redis（与 audit/skills 同一单例约定；无 REDIS_URL 返回 None）。"""
    global _redis_client
    if _redis_client is None:
        from agent_core.config import settings

        if settings.redis_url:
            import redis.asyncio as aioredis

            _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _ensure_mcp() -> None:
    """MCP 注册表惰性基线：env 五个内置地址（PRD 2.2 跨系统服务面）。"""
    if _mcp_servers:
        return
    from agent_core.config import settings

    for name, url in (
        ("oa", settings.mcp_oa_url),
        ("bi", settings.mcp_bi_url),
        ("crm", settings.mcp_crm_url),
        ("erp", settings.mcp_erp_url),
        ("wms", settings.mcp_wms_url),
    ):
        _mcp_servers[name] = {"name": name, "url": url, "builtin": True, "registered_by": None, "registered_at": None}


async def _snapshot() -> None:
    """写事件后镜像快照（无 Redis 时跳过）。"""
    r = _get_redis()
    if r is None:
        return
    payload = {"toggles": _toggles, "mcp_servers": _mcp_servers}
    await r.set(_SNAPSHOT_KEY, json.dumps(payload, ensure_ascii=False))


async def restore() -> None:
    """启动恢复：Redis 快照优先，否则默认基线（API lifespan 调用）。"""
    _ensure_mcp()
    r = _get_redis()
    if r is not None:
        raw = await r.get(_SNAPSHOT_KEY)
        if raw:
            snap = json.loads(raw)
            for k, v in snap.get("toggles", {}).items():
                if k in _KNOWN_TOGGLES:
                    _toggles[k] = bool(v)
            for name, entry in snap.get("mcp_servers", {}).items():
                if isinstance(entry, dict) and _NAME_RE.match(name):
                    entry.setdefault("builtin", False)
                    _mcp_servers[name] = entry


def reset() -> None:
    """清空配置状态并重建默认基线（测试隔离用）。"""
    _toggles.clear()
    _toggles.update({k: True for k in _KNOWN_TOGGLES})
    _mcp_servers.clear()
    _ensure_mcp()


# ---------- 1. 功能开关 ----------


def list_toggles() -> list[dict[str, Any]]:
    """全部开关及说明（工作台陈列）。"""
    return [{"key": k, "value": _toggles[k], "description": desc} for k, desc in _KNOWN_TOGGLES.items()]


def get_toggle(key: str) -> bool:
    """读开关值（未知键报错，避免静默按开处理）。"""
    if key not in _KNOWN_TOGGLES:
        raise ValueError(f"未知功能开关 {key}（可用：{'、'.join(_KNOWN_TOGGLES)}）")
    return _toggles[key]


async def set_toggle(key: str, value: bool, *, by: str, note: str = "") -> dict[str, Any]:
    """改开关并接实线（scheduler_enabled 即时启停调度器），变更落审计。"""
    if key not in _KNOWN_TOGGLES:
        raise ValueError(f"未知功能开关 {key}（可用：{'、'.join(_KNOWN_TOGGLES)}）")
    if not isinstance(value, bool):
        raise ValueError("开关值必须是布尔")  # noqa: TRY004 - 统一 ValueError→API 400
    if _toggles[key] == value:  # 幂等：重复设置同值不落审计不动作
        return {"key": key, "value": value, "changed": False}
    _toggles[key] = value
    await _snapshot()
    await audit.record(
        "syscfg_toggle",
        tool="syscfg",
        params={"key": key, "value": value},
        user_id=by or None,
        result="success",
        detail=f"功能开关 {key} → {value}（{_KNOWN_TOGGLES[key]}）{('：' + note) if note else ''}",
    )
    if key == "automation.scheduler_enabled":  # 实接线：懒导入防循环依赖
        from agent_core.automation import store as automation

        if value:
            automation.start_scheduler()
        else:
            automation.stop_scheduler()
    return {"key": key, "value": value, "changed": True}


# ---------- 2. MCP 注册与健康探测 ----------


def list_mcp() -> list[dict[str, Any]]:
    """全部 MCP 注册项（按名排序，工作台陈列）。"""
    _ensure_mcp()
    return [dict(_mcp_servers[name]) for name in sorted(_mcp_servers)]


async def register_mcp(name: str, url: str, *, by: str) -> dict[str, Any]:
    """追加/改址 MCP 服务（内置五项可改址不可更名），注册动作落审计。"""
    _ensure_mcp()
    if not _NAME_RE.match(name):
        raise ValueError("服务名须为小写字母开头的 1-32 位 [a-z0-9_-]")
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL 必须以 http:// 或 https:// 开头")
    existed = name in _mcp_servers
    _mcp_servers[name] = {
        "name": name,
        "url": url.rstrip("/"),
        "builtin": _mcp_servers.get(name, {}).get("builtin", False),
        "registered_by": by,
        "registered_at": _now(),
    }
    await _snapshot()
    await audit.record(
        "syscfg_mcp_register",
        tool="syscfg",
        params={"name": name, "url": url},
        user_id=by or None,
        result="success",
        detail=f"{'更新' if existed else '注册'} MCP 服务 {name} → {url}",
    )
    return dict(_mcp_servers[name])


async def probe_mcp(*names: str) -> dict[str, Any]:
    """并发探活 GET {url}/health（超时 3s）；不指定则探测全部。

    只读健康检查，不落审计（避免轮询噪音）；结果含时延与错误摘要。
    """
    import httpx

    _ensure_mcp()
    targets = [n for n in (names or sorted(_mcp_servers)) if n in _mcp_servers]
    unknown = [n for n in (names or []) if n not in _mcp_servers]
    if unknown:
        raise ValueError(f"未知 MCP 服务：{'、'.join(unknown)}")

    async def _probe(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
        url = _mcp_servers[name]["url"]
        started = datetime.now(UTC)
        try:
            resp = await client.get(f"{url}/health", timeout=3.0)
            latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
            return {"name": name, "url": url, "healthy": resp.status_code == 200, "status": resp.status_code, "latency_ms": latency, "error": None}
        except Exception as exc:  # noqa: BLE001 — 探活对任意网络异常统一降级为不健康
            return {"name": name, "url": url, "healthy": False, "status": None, "latency_ms": None, "error": str(exc) or type(exc).__name__}

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(_probe(client, n) for n in targets))
    return {"checked_at": _now(), "results": list(results)}


# ---------- 3. 模型路由（只读视图） ----------


def model_view() -> dict[str, Any]:
    """llm/embedding 当前路由只读陈列（api_key 打码；改配置走 env 重启生效）。"""
    from agent_core.config import settings

    def _route(base_url: str | None, model: str) -> dict[str, Any]:
        return {
            "base_url": base_url,
            "model": model,
            "api_key": "已配置（打码）" if base_url else "未配置（降级本地）",
        }

    return {
        "llm": _route(settings.llm_base_url, settings.llm_model),
        "embedding": _route(settings.embedding_base_url, settings.embedding_model),
        "note": "模型路由只读：修改走环境变量/配置中心并重启 agent-core 生效",
    }
