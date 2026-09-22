"""HITL 确认存储：Redis confirm:{token} TTL 10 分钟（PRD 8.7 / ARCHITECTURE 4.1）。

- 生产：REDIS_URL 配置 → Redis（跨 Worker 共享，TTL 自动过期）
- 冒烟：未配置 → 进程内字典 + 过期检查（零外部依赖）
接口一致（set/get/pop），业务代码无感切换。
"""

import json
import time
from typing import Any

from agent_core.config import settings

_CONFIRM_PREFIX = "confirm:"

_memory_store: dict[str, tuple[float, dict[str, Any]]] = {}
_redis_client: Any = None


async def _get_redis() -> Any:
    """惰性初始化 Redis 客户端（进程内单例）。"""
    global _redis_client
    if _redis_client is None and settings.redis_url:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _now() -> float:
    return time.time()


async def set_token(token: str, payload: dict[str, Any]) -> None:
    """写入确认令牌 → 挂起状态快照（TTL 10 分钟）。"""
    redis = await _get_redis()
    if redis is not None:
        await redis.set(
            _CONFIRM_PREFIX + token,
            json.dumps(payload, ensure_ascii=False),
            ex=settings.confirm_ttl_seconds,
        )
        return
    _memory_store[token] = (
        _now() + settings.confirm_ttl_seconds,
        payload,
    )


async def get_token(token: str) -> dict[str, Any] | None:
    """读取挂起状态（过期/不存在返回 None）。"""
    redis = await _get_redis()
    if redis is not None:
        raw = await redis.get(_CONFIRM_PREFIX + token)
        if raw is None:
            return None
        payload: dict[str, Any] = json.loads(raw)
        return payload
    entry = _memory_store.get(token)
    if entry is None:
        return None
    expires_at, payload = entry
    if _now() > expires_at:
        del _memory_store[token]
        return None
    return payload


async def pop_token(token: str) -> dict[str, Any] | None:
    """读取并删除（确认卡一次性：重复确认返回 404）。"""
    payload = await get_token(token)
    if payload is None:
        return None
    redis = await _get_redis()
    if redis is not None:
        await redis.delete(_CONFIRM_PREFIX + token)
        return payload
    _memory_store.pop(token, None)
    return payload
