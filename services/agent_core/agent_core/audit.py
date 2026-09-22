"""审计日志：Agent 侧敏感操作统一流水（PRD 10 安全合规，PLAN P1.2）。

覆盖范围：
- MCP tools/call 全量（读/写均记，mcp_client 单点收口埋点）
- HITL 确认卡生命周期：签发 / 确认通过 / 用户拒绝 / 确认人校验失败
- 权限拒绝（技能级角色矩阵 / BI 区域白名单，permission 节点落点）
- 鉴权成功事件（登录后会话活动代理；登出发生在桌面端/IdP 侧）

字段（PRD 10）：user_id / session_id / time / action / tool /
params_digest（参数摘要，截断防全量泄漏）/ result / duration_ms / detail。

存储：MVP 进程内环形缓冲（5000 条）+ 可选 Redis（REDIS_URL → 热窗
audit:events 截断 5000 供查询 + 全量归档 audit:archive，EXPIRE 180 天
满足 PRD 10 留存要求；生产可再对接日志平台冷存）。

身份注入：API 入口 set_actor 写入 contextvar，任务上下文透传到
mcp_client 埋点自动携带操作者，避免逐层传参污染工具签名。
"""

import json
from collections import deque
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from agent_core.config import settings

_AUDIT_KEY = "audit:events"
_AUDIT_ARCHIVE_KEY = "audit:archive"
_AUDIT_RETENTION_SECONDS = 180 * 24 * 3600  # PRD 10：留存 ≥180 天
_MAX_ENTRIES = 5000
_DIGEST_MAX = 300

_memory: deque[dict[str, Any]] = deque(maxlen=_MAX_ENTRIES)
_redis_client: Any = None

_actor: ContextVar[dict[str, Any] | None] = ContextVar("audit_actor", default=None)


def set_actor(actor: dict[str, Any]) -> Any:
    """标记当前任务的操作者（API 入口调用，随 await 链透传到 MCP 埋点）。

    返回 contextvar Token，请求结束时传给 reset_actor 恢复。
    """
    return _actor.set(actor)


def reset_actor(token: Any) -> None:
    _actor.reset(token)


def current_actor() -> dict[str, Any] | None:
    return _actor.get()


async def _get_redis() -> Any:
    """惰性初始化 Redis 客户端（与 confirm_store/slots 同一单例约定）。"""
    global _redis_client
    if _redis_client is None and settings.redis_url:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _digest(params: Any) -> str:
    """参数摘要：JSON 序列化后截断（审计可追溯 vs 全量参数防泄漏）。"""
    try:
        text = json.dumps(params, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(params)
    return text[:_DIGEST_MAX]


async def record(
    action: str,
    *,
    tool: str | None = None,
    params: Any = None,
    user_id: str | None = None,
    session_id: str | None = None,
    result: str = "ok",
    duration_ms: int | None = None,
    detail: str = "",
) -> None:
    """落一条审计；user_id/session_id 缺省取当前 actor（API 层注入）。"""
    actor = _actor.get() or {}
    entry: dict[str, Any] = {
        "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "user_id": user_id if user_id is not None else actor.get("user_id"),
        "session_id": session_id
        if session_id is not None
        else actor.get("session_id"),
        "action": action,
        "tool": tool,
        "params_digest": _digest(params) if params is not None else "",
        "result": result,
        "duration_ms": duration_ms,
        "detail": detail[:_DIGEST_MAX],
    }
    redis = await _get_redis()
    if redis is not None:
        payload = json.dumps(entry, ensure_ascii=False)
        await redis.rpush(_AUDIT_KEY, payload)
        await redis.ltrim(_AUDIT_KEY, -_MAX_ENTRIES, -1)
        # 全量归档（不截断，滑动 180 天 TTL），满足留存审计要求
        await redis.rpush(_AUDIT_ARCHIVE_KEY, payload)
        await redis.expire(_AUDIT_ARCHIVE_KEY, _AUDIT_RETENTION_SECONDS)
        return
    _memory.append(entry)


async def recent(
    limit: int = 100,
    user_id: str | None = None,
    action: str | None = None,
) -> list[dict[str, Any]]:
    """查询审计流水（最新在前）；user_id / action 可选过滤。"""
    redis = await _get_redis()
    if redis is not None:
        raw = await redis.lrange(_AUDIT_KEY, 0, -1)
        items = [json.loads(x) for x in raw]
    else:
        items = list(_memory)
    if user_id is not None:
        items = [e for e in items if e.get("user_id") == user_id]
    if action is not None:
        items = [e for e in items if e.get("action") == action]
    items.reverse()
    return items[:limit]


async def clear() -> None:
    """清空内存审计（测试隔离用；Redis 模式不清理）。"""
    _memory.clear()
