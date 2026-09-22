"""补问槽位存储：会话级 pending 草稿（分段收集多轮补问，PRD 5.2）。

写入类技能缺必填参数时，extract 节点把已合并草稿挂到 (user_id, session_id)
槽位；下一轮消息若未命中任何技能且槽位存在，则沿用槽位技能并合并新字段，
直至必填齐备进入校验/HITL。槽位生命周期：
- extract 节点：合并后始终写入（补问/校验失败/取消确认后均可继续改参重试）
- route 节点：新消息命中其他技能 → 清除（切换意图）
- format 节点：写入成功（doc_no）→ 清除

- 生产：REDIS_URL 配置 → Redis（跨 Worker 共享，TTL 与确认卡一致）
- 冒烟：未配置 → 进程内字典（零外部依赖），接口一致。
"""

import json
import time
from typing import Any

from agent_core.config import settings

_PENDING_PREFIX = "pending:"
_QUERY_PREFIX = "query:"
_PENDING_TTL_SECONDS = 30 * 60  # 补问挂起上限（用户超时未答自然放弃）

_memory_store: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
# 最近只读查询结果（按 kind 命名空间隔离：bi=BI 结果 dict / approval=审批条目 list，
# 与 pending 草稿独立生命周期；混用同键会跨技能污染——E2E 联调发现的回归）
_query_memory: dict[tuple[str, str, str], tuple[float, Any]] = {}
_redis_client: Any = None


async def _get_redis() -> Any:
    """惰性初始化 Redis 客户端（与 confirm_store 同一单例约定）。"""
    global _redis_client
    if _redis_client is None and settings.redis_url:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _now() -> float:
    return time.time()


async def get_pending(user_id: str, session_id: str) -> dict[str, Any] | None:
    """读取会话挂起草稿（{"skill_name", "draft"}；过期/不存在返回 None）。"""
    redis = await _get_redis()
    if redis is not None:
        raw = await redis.get(_PENDING_PREFIX + f"{user_id}:{session_id}")
        return json.loads(raw) if raw else None
    entry = _memory_store.get((user_id, session_id))
    if entry is None:
        return None
    expires_at, payload = entry
    if _now() > expires_at:
        del _memory_store[(user_id, session_id)]
        return None
    return payload


async def set_pending(
    user_id: str, session_id: str, skill_name: str, draft: dict[str, Any]
) -> None:
    """写入/覆盖会话挂起草稿。"""
    payload = {"skill_name": skill_name, "draft": draft}
    redis = await _get_redis()
    if redis is not None:
        await redis.set(
            _PENDING_PREFIX + f"{user_id}:{session_id}",
            json.dumps(payload, ensure_ascii=False),
            ex=_PENDING_TTL_SECONDS,
        )
        return
    _memory_store[(user_id, session_id)] = (_now() + _PENDING_TTL_SECONDS, payload)


async def clear_pending(user_id: str, session_id: str) -> None:
    """清除会话挂起草稿（意图切换/写入成功）。"""
    redis = await _get_redis()
    if redis is not None:
        await redis.delete(_PENDING_PREFIX + f"{user_id}:{session_id}")
        return
    _memory_store.pop((user_id, session_id), None)


async def set_query_result(user_id: str, session_id: str, result: Any, kind: str) -> None:
    """存储最近一次只读查询结果（kind 隔离："bi" / "approval"，防跨技能互相污染）。"""
    redis = await _get_redis()
    if redis is not None:
        await redis.set(
            _QUERY_PREFIX + f"{user_id}:{session_id}:{kind}",
            json.dumps({"items": result}, ensure_ascii=False),
            ex=_PENDING_TTL_SECONDS,
        )
        return
    _query_memory[(user_id, session_id, kind)] = (_now() + _PENDING_TTL_SECONDS, result)


async def get_query_result(user_id: str, session_id: str, kind: str) -> Any:
    """读取最近查询结果（过期/不存在返回 None）。"""
    redis = await _get_redis()
    if redis is not None:
        raw = await redis.get(_QUERY_PREFIX + f"{user_id}:{session_id}:{kind}")
        return json.loads(raw)["items"] if raw else None
    entry = _query_memory.get((user_id, session_id, kind))
    if entry is None:
        return None
    expires_at, result = entry
    if _now() > expires_at:
        del _query_memory[(user_id, session_id, kind)]
        return None
    return result
